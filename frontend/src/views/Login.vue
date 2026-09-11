<!-- views/Login.vue —— 登录 / 注册页：直连 Supabase GoTrue 换取 JWT -->
<script setup lang="ts">
import {ref} from 'vue'
import {useRouter} from 'vue-router'
import {useAuthStore} from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const username = ref('')
const password = ref('')
const isSignUp = ref(false)
const error = ref('')
const busy = ref(false)
// 注册后若 GoTrue 要求邮箱确认，则没有会话，需要提示用户去收信
const notice = ref('')

async function submit() {
    error.value = ''
    notice.value = ''
    busy.value = true
    try {
        if (isSignUp.value) {
            await auth.signUp(username.value, password.value)
            if (!auth.session) {
                notice.value = '注册成功，请直接登录'
                isSignUp.value = false
                return
            }
        } else {
            await auth.signIn(username.value, password.value)
        }
        await router.replace({name: 'chat'})
    } catch (e) {
        error.value = e instanceof Error ? e.message : '操作失败，请重试'
    } finally {
        busy.value = false
    }
}
</script>

<template>
    <div class="login">
        <form class="login__card" @submit.prevent="submit">
            <h1 class="login__brand">日常助手</h1>
            <p class="login__hint">{{ isSignUp ? '创建一个账号' : '登录后继续你的对话' }}</p>

            <label class="login__field">
                <span class="login__label">用户名</span>
                <input v-model="username" class="login__input" type="text" autocomplete="username"
                       placeholder="字母、数字、_ 或 -" minlength="3" maxlength="32"
                       pattern="[A-Za-z0-9_-]+" required>
            </label>
            <label class="login__field">
                <span class="login__label">密码</span>
                <input v-model="password" class="login__input" type="password"
                       :autocomplete="isSignUp ? 'new-password' : 'current-password'"
                       placeholder="至少 6 位" minlength="6" required>
            </label>

            <p v-if="error" class="login__error">{{ error }}</p>
            <p v-if="notice" class="login__notice">{{ notice }}</p>

            <button class="login__submit" type="submit" :disabled="busy">
                {{ busy ? '处理中…' : (isSignUp ? '注册' : '登录') }}
            </button>

            <button class="login__switch" type="button" @click="isSignUp = !isSignUp; error = ''">
                {{ isSignUp ? '已有账号，去登录' : '没有账号？注册一个' }}
            </button>
        </form>
    </div>
</template>

<style scoped>
.login {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100vh;
    background: var(--bg);
}

.login__card {
    display: flex;
    flex-direction: column;
    gap: 16px;
    width: 320px;
    padding: 32px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
}

.login__brand {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    color: var(--text);
    text-align: center;
}

.login__hint {
    margin: 0 0 8px;
    font-size: 13px;
    color: var(--text-secondary);
    text-align: center;
}

.login__field {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.login__label {
    font-size: 13px;
    color: var(--text-secondary);
}

.login__input {
    padding: 9px 12px;
    font-size: 14px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    outline: none;
}

.login__input:focus {
    border-color: var(--accent);
}

.login__error {
    margin: 0;
    font-size: 13px;
    color: #dc2626;
}

.login__notice {
    margin: 0;
    font-size: 13px;
    color: var(--accent-strong);
}

.login__submit {
    padding: 10px;
    font-size: 14px;
    color: #fff;
    background: var(--accent);
    border: none;
    border-radius: 8px;
    cursor: pointer;
}

.login__submit:hover:not(:disabled) {
    background: var(--accent-strong);
}

.login__submit:disabled {
    opacity: 0.6;
    cursor: default;
}

.login__switch {
    font-size: 13px;
    color: var(--text-secondary);
    background: none;
    border: none;
    cursor: pointer;
}

.login__switch:hover {
    color: var(--accent);
}
</style>
