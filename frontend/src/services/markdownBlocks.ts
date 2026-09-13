// services/markdownBlocks.ts —— 把回答正文切成「HTML 段」与「知识库图片」两类块。
//
// 为什么在送进 marked **之前**就切：图片标记 【图13-2】 不是 markdown 语法，切走之后
// 剩下的文本里根本不含它，既不会被 DOMPurify 当成可疑 URI 把 src 洗掉，也不必在渲染完的
// DOM 上做字符串替换（那会让 <img> 随每个 token 的重渲染被销毁重建，blob 反复加载）。
//
// 切分只依赖正文，**不依赖图片有没有取到**：blob URL 是异步到达的，混进来会让块数组
// 反复重建。取不到图的那一块由渲染方跳过，效果就是「悄悄不显示」。
import { marked } from 'marked'
import DOMPurify from 'dompurify'

export type Block =
    | { kind: 'html'; html: string; key: string }
    | { kind: 'img'; ref: string; key: string }

// 引用号的形状：chunk 行 id - 图在块内下标，与后端 number_hit_images 发出来的一致
const MARKER = /【图(\d+-\d+)】/g
// 流式中途未写完的尾巴（【图13-2 半个）：先裁掉，否则会当裸文本露出去
const PARTIAL_TAIL = /【图[\d-]*$/
// 代码围栏：模型偶尔在答案里贴代码块，围栏内的 【图N-M】 只是普通文本
const FENCE_LINE = /^\s*(```|~~~)/gm

/** 位置 pos 是否落在未闭合的代码围栏里 —— 数它前面有几个围栏行，奇数即在内。 */
function inFence(text: string, pos: number): boolean {
    const before = text.slice(0, pos).match(FENCE_LINE)
    return !!before && before.length % 2 === 1
}

function renderHtml(seg: string): string {
    // breaks: true 让单换行也渲染成 <br>，与原来 ChatPanel 的渲染保持一致
    const raw = marked.parse(seg, { async: false, breaks: true }) as string
    return DOMPurify.sanitize(raw)
}

/**
 * 按引用号切块。块 key 用**起始偏移**而不是数组下标：内容只追加，起始偏移不变，
 * 尾部那段重渲染时前面的块才能被 Vue 复用（用下标会让图片块整体错位重建）。
 */
export function buildBlocks(md: string): Block[] {
    const text = md.replace(PARTIAL_TAIL, '')
    const out: Block[] = []
    let last = 0
    MARKER.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = MARKER.exec(text)) !== null) {
        if (inFence(text, m.index)) continue
        const seg = text.slice(last, m.index)
        if (seg.trim()) out.push({ kind: 'html', html: renderHtml(seg), key: `h:${last}` })
        out.push({ kind: 'img', ref: m[1]!, key: `i:${m.index}` })
        last = m.index + m[0].length
    }
    const tail = text.slice(last)
    if (tail.trim()) out.push({ kind: 'html', html: renderHtml(tail), key: `h:${last}` })
    return out
}
