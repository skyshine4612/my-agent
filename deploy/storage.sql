-- storage.objects 的策略：与业务库 schema.sql 分开执行
--
-- 单独成文件：本文件依赖 storage-api 首启后自建的 storage schema 与 storage.foldername()，
-- 这两个在 schema.sql 的执行时点还不存在。
--
-- 执行方式（二选一）：
--   1. Studio → SQL Editor → 整段贴入 → Run
--   2. psql "$SUPABASE_DATABASE_URL" -f deploy/storage.sql
--
-- 幂等：策略先 drop 再建，可反复执行。
--
-- storage schema 每张表都开着 RLS 却没有任何内置策略，所以「不加策略」不等于「不限制」，
-- 而是除 service_role 外全拒——包括后端自己（实测 authenticated 上传 403，service_role 200）。
--
-- 依赖 HS256 对称密钥：启用非对称密钥后 auth.uid() 拿不到 sub，本文件两条策略会全拒。

drop policy if exists storage_files_own on storage.objects;
create policy storage_files_own on storage.objects
    for all to authenticated
    using      (bucket_id = 'files' and (storage.foldername(name))[1] = auth.uid()::text)
    with check (bucket_id = 'files' and (storage.foldername(name))[1] = auth.uid()::text);

-- knowledge 桶是共享语料（与 documents 表的定位一致），但必须含 update：
-- 重抓同一 URL 时 source_key 与序号不变、撞同一个 key 才会冲突，
-- 而冲突被「上传失败只丢图」静默吞掉 —— 图全没了、正文里的「【图片：…】」还在。
drop policy if exists storage_knowledge_all on storage.objects;
create policy storage_knowledge_all on storage.objects
    for all to authenticated
    using      (bucket_id = 'knowledge')
    with check (bucket_id = 'knowledge');
