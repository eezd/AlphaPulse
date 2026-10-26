import { storeToRefs } from "pinia"
import { useSourcesStore } from "@/pinia/stores/sources"

export function useSourceNames() {
  const store = useSourcesStore()
  onMounted(() => store.load())
  return storeToRefs(store).names
}
