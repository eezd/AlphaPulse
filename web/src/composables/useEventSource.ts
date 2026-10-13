/** 订阅 SSE 事件；组件卸载时关闭。断线由浏览器自动重连 */
export function useEventSource<T>(url: string, event: string, onMessage: (data: T) => void) {
  const connected = ref(false)
  const source = new EventSource(url)
  source.addEventListener("open", () => connected.value = true)
  source.addEventListener("error", () => connected.value = false)
  source.addEventListener(event, e => onMessage(JSON.parse((e as MessageEvent).data)))
  onBeforeUnmount(() => source.close())
  return { connected }
}
