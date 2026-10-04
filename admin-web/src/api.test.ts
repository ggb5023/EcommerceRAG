import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, get, post } from './api'

afterEach(() => vi.unstubAllGlobals())

describe('admin API boundary', () => {
  it('uses same-origin credentials for authorized reads', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"items":[]}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await get('/admin/v1/merchant/shops')

    expect(fetchMock).toHaveBeenCalledWith('/admin/v1/merchant/shops', {
      credentials: 'same-origin',
    })
  })

  it('does not hide server authorization failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '{"error":{"code":"admin_scope_denied"}}', { status: 403 },
    )))

    await expect(get('/admin/v1/merchant/reviews')).rejects.toMatchObject({
      status: 403,
      message: expect.stringContaining('没有该操作的授权范围'),
    } satisfies Partial<ApiError>)
  })

  it('sends scoped version confirmation as a JSON command', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"status":"active"}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const body = { confirm: true, shop_id: 'shop-a', expected_fencing_epoch: 4 }

    await post('/admin/v1/merchant/versions/12/publish', body)

    expect(fetchMock).toHaveBeenCalledWith('/admin/v1/merchant/versions/12/publish', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      body: JSON.stringify(body),
    }))
  })
})
