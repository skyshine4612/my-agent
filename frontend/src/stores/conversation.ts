// stores/conversation.ts —— 会话列表/当前会话
import {defineStore} from 'pinia'
import {ref} from 'vue'

export const useConversationStore = defineStore('conversation', () => {
    // 会话列表
    const conversations = ref<any[]>([])
    // 当前选中会话 id
    const currentId = ref<string | null>(null)
    return {conversations, currentId}
})
