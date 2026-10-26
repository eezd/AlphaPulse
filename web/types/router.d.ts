import "vue-router"

declare module "vue-router" {
  interface RouteMeta {
    title?: string
    admin?: boolean
    public?: boolean
    status?: number
  }
}
