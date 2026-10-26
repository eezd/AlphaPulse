export interface Identity {
  username: string
  is_admin: boolean
}

export type GetIdentityResponseData = Identity

export type RevokeSessionResponseData = null

export type Captcha = { enabled: false } | {
  enabled: true
  captcha_id: string
  image: string
  expires_in: number
}

export interface LoginForm {
  username: string
  password: string
  captcha_id?: string
  code?: string
}

export interface LoginResult {
  token: string
  token_type: "bearer"
  expires_at: string
}

export type GetCaptchaResponseData = Captcha

export type LoginResponseData = LoginResult

export interface WebSocketTicket {
  ticket: string
  expires_in: number
}
