import { CanceledError } from "axios"
import { onBeforeUnmount } from "vue"
import { useUserStore } from "@/pinia/stores/user"

/** 确认框等异步交互结束后，不得用新账号执行旧页面的操作。 */
export function useSessionScope() {
  const user = useUserStore()
  const version = user.sessionVersion
  let disposed = false
  onBeforeUnmount(() => {
    disposed = true
  })
  const isCurrent = () => !disposed && version === user.sessionVersion
  function assertCurrent() {
    if (!isCurrent()) throw new CanceledError("页面或会话已切换")
  }
  return { isCurrent, assertCurrent }
}
