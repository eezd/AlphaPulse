import { listSources } from "@/apis/crawler"

const names = ref<Record<string, string>>({})

/** 来源 code → 中文名，全局只请求一次 */
export function useSourceNames() {
  if (Object.keys(names.value).length === 0) {
    listSources().then((info) => {
      names.value = Object.fromEntries(info.sources.map(s => [s.code, s.name]))
    })
  }
  return names
}
