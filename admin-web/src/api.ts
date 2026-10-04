export type AdminSession = {
  tenant_id: string
  user_id: string
  runtime_role: string
  shop_id: string
  allowed_shop_ids: string[]
  permission_revision: string
  admin_roles: string[]
  admin_role_catalog: AdminRoleCatalogEntry[]
  is_mock: boolean
}

export type AdminRoleCatalogEntry = {
  role_key: string
  storage_role: string | null
  display_name: string
  catalog_version: string
  enabled: boolean
  requestable: boolean
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

const messages: Record<number, string> = {
  401: '登录状态无效，请重新建立受限会话。',
  403: '当前账号没有该操作的授权范围。',
  404: '请求的记录不存在或已不在授权范围内。',
  409: '数据或权限版本已变化，请刷新后重新确认。',
  413: '上传文件超过允许大小。',
  503: '服务暂不可用，数据未确认成功。',
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init })
  if (!response.ok) {
    let detail = ''
    try {
      const payload = await response.json() as { error?: { code?: string } }
      detail = payload.error?.code ? ` (${payload.error.code})` : ''
    } catch {
      detail = ''
    }
    throw new ApiError(response.status, `${messages[response.status] ?? '请求失败。'}${detail}`)
  }
  return response.json() as Promise<T>
}

export const get = <T>(path: string) => request<T>(path)
export const post = <T>(path: string, body: unknown) => request<T>(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})
