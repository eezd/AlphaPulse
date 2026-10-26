export interface OpenApiDocument {
  openapi: string
  info: { title: string, version: string }
  paths: Record<string, unknown>
  components?: Record<string, unknown>
}
