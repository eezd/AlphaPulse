import { http } from "@/http"

export const useUserStore = defineStore("user", () => {
  const username = ref<string>()

  async function fetchMe() {
    const me: { username: string } = await http.get("/auth/me")
    username.value = me.username
  }

  async function login(name: string, password: string) {
    const me: { username: string } = await http.post("/auth/login", { username: name, password })
    username.value = me.username
  }

  async function logout() {
    await http.post("/auth/logout")
    username.value = undefined
  }

  return { username, fetchMe, login, logout }
})
