// services/imageUrls.ts —— 知识库图片的 blob URL 缓存，按引用计数回收。
//
// 为什么要缓存：ChatPanel 随 token 事件逐块重渲染 messages，URL 一旦在渲染路径 /
// computed 里创建，每个 token 都会 mint 一个新的 blob URL，而旧的永不回收（内存泄漏
// + 图片反复闪烁）。所以创建只发生在这里的 hold() 里，一个对象 key 全应用只对应一个 URL。
import {onScopeDispose, ref, type Ref} from 'vue'
import {fetchKnowledgeImage} from '@/services/api'

const cache = new Map<string, { url: string, refs: number }>()

export interface ImageUrls {
    urls: Ref<Record<string, string>>
    hold: (key: string) => Promise<void>
    release: (key: string) => void
}

export function useImageUrls(): ImageUrls {
    // key → blob URL，模板按 key 取
    const urls = ref<Record<string, string>>({})
    const held = new Set<string>()

    async function hold(key: string) {
        if (held.has(key)) return                     // 已经持有，不再 mint
        held.add(key)
        const hit = cache.get(key)
        if (hit) {
            hit.refs++
            urls.value[key] = hit.url
            return
        }
        try {
            const url = URL.createObjectURL(await fetchKnowledgeImage(key))
            cache.set(key, {url, refs: 1})
            urls.value[key] = url
        } catch {
            // 文档已删 / 图片上传失败 / 未登录：不显示缩略图，别让它冒泡成未捕获的 rejection
            held.delete(key)
        }
    }

    function release(key: string) {
        if (!held.delete(key)) return
        const e = cache.get(key)
        if (!e) return
        e.refs--
        if (e.refs <= 0) {
            URL.revokeObjectURL(e.url)
            cache.delete(key)
        }
    }

    // 组件卸载时把本组件持有的全放掉：不放就是长期泄漏
    onScopeDispose(() => { for (const k of [...held]) release(k) })

    return {urls, hold, release}
}
