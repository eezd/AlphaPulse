import type { Ref } from "vue"

/** 持久化到 localStorage 的 ref；值需可 JSON 序列化 */
export function usePersisted<T>(key: string, initial: T): Ref<T> {
  const fullKey = `alphapulse.${key}`
  const raw = localStorage.getItem(fullKey)
  const state = ref(raw === null ? initial : JSON.parse(raw)) as Ref<T>
  watch(state, value => localStorage.setItem(fullKey, JSON.stringify(value)), { deep: true })
  return state
}
