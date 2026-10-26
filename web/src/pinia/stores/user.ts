import type { Identity } from "@@/apis/auth/type"
import { getIdentityApi, revokeSessionApi } from "@@/apis/auth"
import { CanceledError } from "axios"
import { defineStore } from "pinia"
import { computed, ref } from "vue"

export const TOKEN_KEY = "alphapulse.auth.token"

export const useUserStore = defineStore("user", () => {
  const token = ref(localStorage.getItem(TOKEN_KEY) ?? "")
  const sessionVersion = ref(0)
  const identity = ref<Identity>()
  const username = computed(() => identity.value?.username)
  let restoring: Promise<void> | undefined

  function setToken(value: string, persist = true) {
    sessionVersion.value++
    token.value = value
    identity.value = undefined
    restoring = undefined
    if (persist) {
      if (value)
        localStorage.setItem(TOKEN_KEY, value)
      else
        localStorage.removeItem(TOKEN_KEY)
    }
  }

  async function fetchMe() {
    if (!restoring) {
      const version = sessionVersion.value
      restoring = getIdentityApi().then((me) => {
        if (version !== sessionVersion.value)
          throw new CanceledError("会话已切换")
        identity.value = me
      }).finally(() => {
        if (version === sessionVersion.value)
          restoring = undefined
      })
    }
    return restoring
  }

  async function logout() {
    const version = sessionVersion.value
    await revokeSessionApi()
    if (version !== sessionVersion.value)
      throw new CanceledError("会话已切换")
    setToken("")
  }

  return { token, sessionVersion, identity, username, setToken, fetchMe, logout }
})
