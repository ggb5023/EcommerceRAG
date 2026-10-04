export const adminPages = [
  'overview', 'accounts', 'requests', 'audit', 'shops', 'sources', 'ingestion', 'reviews',
] as const

export type AdminPage = typeof adminPages[number]
export type AdminRoutePage = AdminPage | 'not-found'

const paths: Record<AdminPage, string> = {
  overview: '/admin/overview',
  accounts: '/admin/access/accounts',
  requests: '/admin/access/requests',
  audit: '/admin/access/audit',
  shops: '/admin/merchant/shops',
  sources: '/admin/merchant/sources',
  ingestion: '/admin/merchant/ingestion',
  reviews: '/admin/merchant/reviews',
}

const pagesByPath = new Map(Object.entries(paths).map(([page, path]) => [path, page as AdminPage]))

export function adminPageFromPath(path: string): AdminRoutePage {
  return pagesByPath.get(path.replace(/\/$/, '') || '/') ?? 'not-found'
}

export function adminPathForPage(page: AdminPage): string {
  return paths[page]
}
