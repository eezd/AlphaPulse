export interface Jin10Status {
  connected: boolean
  connected_at: string | null
  error: string | null
  sync_state: "awaiting_validation" | "disconnected" | "connecting" | "syncing" | "ready" | "error"
  last_success_at: string | null
  history_next: string | null
}
export interface Jin10Qr {
  task_id: string
  image: string
  expires_at: string
}
export interface Jin10Poll {
  state: "pending" | "connected" | "expired"
}
