import { describe, expect, it } from 'vitest'
import { adminPageFromPath, adminPathForPage, adminPages } from './routes'

describe('admin routes', () => {
  it('round-trips every workspace at its stable nested route', () => {
    for (const page of adminPages) {
      expect(adminPageFromPath(adminPathForPage(page))).toBe(page)
    }
  })

  it('accepts a trailing slash and rejects unknown paths', () => {
    expect(adminPageFromPath('/admin/merchant/reviews/')).toBe('reviews')
    expect(adminPageFromPath('/admin/merchant/unknown')).toBe('not-found')
  })
})
