import { listSourcesApi } from "@@/apis/crawler"
import { defineStore } from "pinia"
import { ref, watch } from "vue"
import { useUserStore } from "@/pinia/stores/user"

export const useSourcesStore = defineStore("sources", () => {
  const names = ref<Record<string, string>>({})
  const user = useUserStore()
  let loading: Promise<void> | undefined
  watch(() => user.sessionVersion, () => {
    names.value = {}
    loading = undefined
  }, { flush: "sync" })

  function load() {
    if (Object.keys(names.value).length)
      return Promise.resolve()
    if (!loading) {
      const version = user.sessionVersion
      loading = listSourcesApi().then((info) => {
        if (version === user.sessionVersion)
          names.value = Object.fromEntries(info.sources.map(s => [s.code, s.name]))
      }).finally(() => {
        if (version === user.sessionVersion)
          loading = undefined
      })
    }
    return loading
  }
  return { names, load }
})
