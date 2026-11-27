/**
 * token 与少量偏好的唯一存取口。
 *
 * 为什么单独一个文件：手机化（Capacitor）之后 token 必须放原生 Preferences，
 * 放 localStorage 会被系统清缓存直接弄丢（用户莫名掉登录）。到时候**只改这一个文件**，
 * 业务代码和 api/client.ts 一行都不动 —— 这就是它存在的唯一理由。
 */

const TOKEN_KEY = 'kb.access_token'
const USER_KEY = 'kb.last_username'

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    // 隐私模式 / WebView 禁用 storage 时不能抛：抛了就等于整个应用白屏
    return ''
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* 存不下就算了，本次会话内存里那份仍然可用 */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 同上 */
  }
}

export function getLastUsername(): string {
  try {
    return localStorage.getItem(USER_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setLastUsername(name: string): void {
  try {
    localStorage.setItem(USER_KEY, name)
  } catch {
    /* 忽略 */
  }
}
