# app/core/knowledge.py
# 知识库存储：documents 表 + 切块 + 向量化入库 + 语义检索 + 原图进 Storage。
# 全局共享（不像长期记忆那样按 user 隔离），但读写仍走 rls_conn：
# 1A 的规矩是放进这套栈的表全开 RLS，业务查询一律不裸池直连。
import hashlib
import logging
import time
import uuid

from app.core.chunking import chunk_units
from app.core.db import rls_conn
from app.core.embedding import embed_batched, get_embedding, to_vector_literal
from app.core.ingest import assign_images, scrub_marks
from app.core.storage import BUCKET_KNOWLEDGE
from app.core.vision import ALLOWED_MIME

logger = logging.getLogger(__name__)


def _source_key(source_url: str) -> str:
    """source_url 的短哈希，图片目录的第一段。

    用哈希而不是 URL 本身：key 里塞完整 URL 要处理编码、长度与非法字符，
    而这个段只用来分目录，可读性由 documents.source_url 补足。
    """
    return hashlib.sha256(source_url.encode()).hexdigest()[:16]


def _ingest_ids(images) -> set[str]:
    """从一批 images 里取出用过的 ingest_id（key 的第二段）。"""
    if not isinstance(images, list):
        return set()
    out = set()
    for it in images:
        key = (it or {}).get("key")
        if not key:
            continue
        parts = key.split("/")
        if len(parts) >= 3:
            out.add(parts[1])
    return out


class KnowledgeStore:
    """知识库：全局共享的攻略语料，按余弦相似度检索；原图存 knowledge 桶。"""

    def __init__(self, storage, embedder=None, chunk_size=600, overlap=100):
        # 零 IO：进程级单例在 import 时构造 store，连接池要到 lifespan 才建
        self.storage = storage
        self._embedder = embedder
        self.chunk_size = chunk_size
        self.overlap = overlap

    @property
    def embedder(self):
        return self._embedder or get_embedding()

    async def ingest(self, user_id, text, title=None, source_url=None, images=None) -> int:
        """切块 → 向量化 → 传图 → 入库，返回写入的块数。

        重抓同一 URL 是**替换**不是叠加（今天的行为是叠加：检索会同时召回两版）。
        四步的顺序保证每一步失败都安全：

            1. embed 新正文            失败 → 旧版完好，什么都没动
            2. 上传新图到 <source_key>/<ingest_id>/   失败 → 旧版完好，只丢新图
            3. 一个事务：delete ... returning images  失败 → 新旧图都在，旧行仍旧
                         where source_url = $1
                       → insert 新行
            4. 删掉第 3 步实际替换掉的旧目录          失败 → 孤儿，可扫

        embed 失败直接抛出，不静默降级：知识库没有向量就检索不到，
        属于必须让调用方知道的情形（与长期记忆允许 embedding=NULL 相反）。
        """
        started = time.monotonic()
        parts = chunk_units(text, self.chunk_size, self.overlap)
        chunks = [c for c, _, _ in parts]
        if not chunks:
            return 0
        spans = [(s, e) for _, s, e in parts]

        # 偏移法：图片按起始偏移挂到唯一一块（跨边界归第一块），占位符换成描述
        buckets = assign_images(text, images or [], spans)
        contents = []
        for chunk, imgs in zip(chunks, buckets):
            for img in imgs:
                chunk = chunk.replace(f"\x00IMG{img['n']}\x00", f"【图片：{img['desc']}】")
            contents.append(scrub_marks(chunk))

        name = title or source_url or "未命名"
        logger.info("[知识库] 切块 %d 块，开始向量化：%s", len(chunks), name)
        vectors = await embed_batched(self.embedder, chunks)                      # 1
        per_chunk = await self._upload_images(user_id, source_url, buckets)       # 2
        source_key = _source_key(source_url) if source_url else None
        replaced = await self._replace(user_id, source_url, title,
                                       contents, vectors, per_chunk)              # 3
        if source_key and replaced:                                               # 4
            await self._drop_objects(user_id, source_key, replaced)
        logger.info("[知识库] 入库：%s（%d 块，耗时 %.1fs）", name, len(chunks),
                    time.monotonic() - started)
        return len(chunks)

    async def _upload_images(self, user_id, source_url, buckets) -> list[list[dict]]:
        """上传本次抓到的图，返回与块等长的 images 数组。

        降级：单张失败只记成 {"key": null, "desc": ...}，正文与其余图照常入库 ——
        爬虫图是增强项，因一张图把整次抓取搞成 502 远不划算（上传件相反，必须报错）。
        key 里带 source_key 与 ingest_id 两段：**版本化是为了让旧块永远指不到新图**，
        同时把「行删了才覆盖字节」这个会破坏好数据的顺序约束彻底解掉。

        **没有 source_url 就一条都不写**（粘贴文本 / 上传文件走的就是这条路）：
        key 恒为 null 的条目什么都指不到，而这时的描述早已在正文的
        `【图片：…】` 里了，前端按它渲染即可。写进去只会多出一个拿不到图的占位符 ——
        那也正是「本来就没有图片通道」与「本该有图、传丢了」的区别。
        """
        if not source_url:
            return [[] for _ in buckets]
        source_key = _source_key(source_url)
        ingest_id = uuid.uuid4().hex
        out: list[list[dict]] = []
        for imgs in buckets:
            row = []
            for img in imgs:
                key = f"{source_key}/{ingest_id}/{img['n']}.{ALLOWED_MIME[img['mime']]}"
                try:
                    await self.storage.put(user_id, BUCKET_KNOWLEDGE, key, img["data"],
                                           img["mime"])
                except Exception:
                    logger.warning("[知识库] 图片上传失败，只丢这张 key=%s", key, exc_info=True)
                    key = None
                row.append({"key": key, "desc": img["desc"]})
            out.append(row)
        return out

    async def _replace(self, user_id, source_url, title, contents, vectors, per_chunk) -> set[str]:
        """一个事务：先删同 source_url 的旧行、再插新行；返回被替换掉的旧 ingest_id 集合。

        `delete ... returning images` 把「这次到底替换掉了哪些目录」精确捕获下来 ——
        第 4 步只能删这批。写成「删 <source_key>/ 下除我之外的全部」，
        两人同时重抓同一 URL 时会把对方的目录删掉。
        source_url 为空时整段 delete 跳过（`where source_url = null` 匹配不到任何行），
        别让它变成「删除条件失效就删全表」的口子。

        images 列**直接传 Python 对象**：init_pool 注册过 jsonb encoder，
        再手写 json.dumps 会被编码两次，读回来是个「长得像 JSON 的 str」。
        """
        replaced: set[str] = set()
        async with rls_conn(user_id) as conn:
            if source_url:
                rows = await conn.fetch(
                    "delete from documents where source_url = $1 returning images", source_url)
                replaced = {i for r in rows for i in _ingest_ids(r["images"])}
            await conn.executemany(
                "insert into documents(title, content, chunk_index, embedding, source_url, images) "
                "values($1, $2, $3, $4::vector, $5, $6)",
                [(title, c, i, to_vector_literal(v), source_url, imgs)
                 for i, (c, v, imgs) in enumerate(zip(contents, vectors, per_chunk))])
        return replaced

    async def _drop_objects(self, user_id, source_key, ingest_ids) -> None:
        """删掉被替换掉的旧版本目录。失败只记日志：正文已经入库，这里再抛只会让整次抓取 502。"""
        for iid in ingest_ids:
            prefix = f"{source_key}/{iid}/"
            try:
                await self.storage.delete_prefix(user_id, BUCKET_KNOWLEDGE, prefix)
            except Exception:
                logger.warning("[知识库] 清理旧图失败，留作孤儿 prefix=%s", prefix, exc_info=True)

    async def search(self, user_id, query, top_k=3) -> list[dict]:
        """语义检索 top-k，返回 [{id, title, content, source_url, similarity, images}]。

        **不动 match_documents**：命中后再补一条 images 查询即可，同一个事务里拿到同一份快照。
        改函数要 drop 再建，而且 hits 形状一变，模型语料与 critic 语料会跟着一起变 ——
        那不该混进一次存储改造。
        """
        vec = (await self.embedder.embed([query]))[0]
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select id, title, content, source_url, similarity "
                "from match_documents($1::vector, $2)",
                to_vector_literal(vec), top_k)
            hits = [dict(r) for r in rows]
            imgs = await conn.fetch(
                "select id, images from documents where id = any($1::bigint[])",
                [h["id"] for h in hits])
        by_id = {r["id"]: (r["images"] or []) for r in imgs}
        for h in hits:
            h["images"] = by_id.get(h["id"], [])
        return hits

    async def list_documents(self, user_id) -> list[dict]:
        """按来源聚合成文档列表（供前端知识库弹窗展示），不含向量列，带图片 key。"""
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select coalesce(d.title, d.source_url, '未命名') as title, d.source_url, "
                # count(*) 会连 lateral join 出来的图片行一起数，必须是 count(distinct)
                "       count(distinct d.id) as chunks, max(d.created_at) as created_at, "
                # 同一张图会被相邻两块同时挂上（chunking 的 overlap），distinct 去重
                "       coalesce(jsonb_agg(distinct img.value -> 'key') "
                "                filter (where img.value -> 'key' is not null), '[]'::jsonb) "
                "                as image_keys "
                "from documents d "
                "left join lateral jsonb_array_elements(d.images) img on true "
                "group by d.title, d.source_url order by count(distinct d.id) desc")
        return [dict(r) for r in rows]

    async def has_source_key(self, user_id, source_key: str) -> bool:
        """该哈希是否对应某个已入库文档的 source_url。

        图片代理端点靠它避免退化成「按 key 直读整个桶」的通用读口。
        文档数量是「几十篇」这个量级，一次全取回来在 Python 侧算比在库里建函数简单。
        """
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select distinct source_url from documents where source_url is not null")
        return any(_source_key(r["source_url"]) == source_key for r in rows)

    async def delete_by_source(self, user_id, source_url) -> int:
        """按来源删掉整篇文档的所有块与其图片对象，返回删除块数。

        **顺序写死：先删对象再删行**，且对象删失败**不吞异常**。反过来的话行先没了，
        前缀删一旦失败就是「永远查不到的孤儿图」；正序最坏只是「行还在、图没了」
        （界面可见、重抓可恢复）。按前缀删会一次清光所有历史版本 —— 重抓留下的旧目录一并回收。
        """
        if source_url:
            await self.storage.delete_prefix(
                user_id, BUCKET_KNOWLEDGE, f"{_source_key(source_url)}/")
        async with rls_conn(user_id) as conn:
            res = await conn.execute("delete from documents where source_url = $1", source_url)
        return int(res.split()[-1])
