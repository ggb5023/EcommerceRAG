import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const baseURL = (process.env.ADMIN_WEB_BASE_URL ?? 'http://127.0.0.1:5174').replace(/\/$/, '')
const routes = [
  ['/admin/overview', '运行状态'],
  ['/admin/access/accounts', '账号与权限'],
  ['/admin/access/requests', '权限申请'],
  ['/admin/access/audit', '授权审计'],
  ['/admin/merchant/shops', '店铺范围'],
  ['/admin/merchant/sources', '资料来源'],
  ['/admin/merchant/ingestion', '摄取任务'],
  ['/admin/merchant/reviews', '版本审核'],
]
const viewports = [
  { width: 1440, height: 900 },
  { width: 1280, height: 800 },
  { width: 1024, height: 768 },
  { width: 390, height: 844 },
]

const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: viewports[0] })
const failures = []
let expected503 = false
page.on('pageerror', error => failures.push(`pageerror:${error.message}`))
page.on('response', response => {
  if (response.url().includes('/admin/v1/') && response.status() >= 500 && !expected503) {
    failures.push(`api:${response.status()}:${new URL(response.url()).pathname}`)
  }
})

try {
  for (const [path, title] of routes) {
    await page.goto(`${baseURL}${path}`, { waitUntil: 'networkidle' })
    await page.getByRole('heading', { name: title, level: 1 }).waitFor()
    assert.equal(await page.locator('h1').innerText(), title, `${path} title`)
  }

  for (const viewport of viewports) {
    await page.setViewportSize(viewport)
    for (const [path, title] of routes) {
      await page.goto(`${baseURL}${path}`, { waitUntil: 'networkidle' })
      await page.getByRole('heading', { name: title, level: 1 }).waitFor()
      const layout = await page.evaluate(() => ({
        viewport: innerWidth,
        document: document.documentElement.scrollWidth,
        main: Math.round(document.querySelector('.main-area')?.getBoundingClientRect().width ?? 0),
      }))
      assert.equal(layout.document, viewport.width, `${path} horizontal overflow at ${viewport.width}px`)
      assert.ok(layout.main > 0, `${path} main area missing at ${viewport.width}px`)
    }
  }

  await page.setViewportSize(viewports[0])
  await page.goto(`${baseURL}/admin/merchant/reviews`, { waitUntil: 'networkidle' })
  const versionRows = page.locator('.version-row')
  let reviewRow = null
  for (let index = 0; index < await versionRows.count(); index += 1) {
    const detailResponse = page.waitForResponse(response =>
      response.url().match(/\/admin\/v1\/merchant\/reviews\/\d+$/) !== null,
    )
    await versionRows.nth(index).click()
    await detailResponse
    const reviewAction = page.getByRole('button', { name: '记录审核结论' })
    if (await reviewAction.count() && await reviewAction.isVisible()) {
      reviewRow = versionRows.nth(index)
      await reviewAction.click()
      break
    }
  }
  assert.ok(reviewRow, 'no reviewable version available for confirmation preview')

  async function verifyConfirmation(dialogName, requiredFields) {
    const dialog = page.getByRole('dialog', { name: dialogName })
    await dialog.waitFor()
    const dialogText = await dialog.innerText()
    for (const field of requiredFields) {
      assert.ok(dialogText.includes(field), `${dialogName} confirmation is missing ${field}`)
    }
    const confirm = dialog.getByRole('button', { name: '确认并记录审计' })
    assert.equal(await confirm.isDisabled(), true, 'confirmation must start disabled')
    await dialog.locator('textarea').fill('Browser review only; no operation submitted.')
    await dialog.getByRole('checkbox').check()
    assert.equal(await confirm.isEnabled(), true, 'confirmation requires reason and scope acknowledgement')
    await dialog.getByRole('button', { name: '关闭' }).click()
  }

  await verifyConfirmation('确认资料审核', [
    'Tenant / Shop', 'Document / Version', 'Source hash', 'Permission revision', 'Fencing epoch',
  ])
  const versionRowResponse = page.waitForResponse(response =>
    response.url().match(/\/admin\/v1\/merchant\/reviews\/\d+$/) !== null,
  )
  await reviewRow.click()
  await versionRowResponse
  await page.getByRole('button', { name: '撤销版本' }).click()
  await verifyConfirmation('确认版本操作', [
    'Tenant', 'Shop', 'Document / Version', 'Version hash', 'Permission revision', 'Fencing epoch',
  ])

  await reviewRow.click()
  await page.getByRole('button', { name: '撤销版本' }).click()
  await page.route('**/admin/v1/merchant/versions/*/revoke', async route => {
    if (route.request().method() !== 'POST') return route.continue()
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'index_sync_unavailable' } }),
    })
  })
  const unavailableDialog = page.getByRole('dialog', { name: '确认版本操作' })
  expected503 = true
  await unavailableDialog.locator('textarea').fill('Browser fail-closed service check.')
  await unavailableDialog.getByRole('checkbox').check()
  await unavailableDialog.getByRole('button', { name: '确认并记录审计' }).click()
  await page.getByText('管理服务暂不可用').waitFor()
  assert.equal(await page.locator('.content-area').count(), 0, '503 must clear restricted page content')
  expected503 = false
  await page.unroute('**/admin/v1/merchant/versions/*/revoke')
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: '版本审核', level: 1 }).waitFor()

  const restoredDetailResponse = page.waitForResponse(response =>
    response.url().match(/\/admin\/v1\/merchant\/reviews\/\d+$/) !== null,
  )
  await reviewRow.click()
  await restoredDetailResponse
  await page.getByRole('button', { name: '撤销版本' }).click()
  await page.route('**/admin/v1/merchant/versions/*/revoke', async route => {
    if (route.request().method() !== 'POST') return route.continue()
    await route.fulfill({
      status: 403,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'admin_scope_denied' } }),
    })
  })
  const forbiddenDialog = page.getByRole('dialog', { name: '确认版本操作' })
  await forbiddenDialog.locator('textarea').fill('Browser fail-closed authorization check.')
  await forbiddenDialog.getByRole('checkbox').check()
  await forbiddenDialog.getByRole('button', { name: '确认并记录审计' }).click()
  await page.getByText('没有可用的管理会话').waitFor()
  assert.equal(await page.locator('.content-area').count(), 0, '403 must clear restricted page content')
  await page.unroute('**/admin/v1/merchant/versions/*/revoke')

  assert.deepEqual(failures, [], 'browser and management API errors')
  console.log(JSON.stringify({ status: 'PASS', route_count: routes.length, viewports,
    review_confirmation: 'PASS', version_confirmation: 'PASS', fail_closed_403: 'PASS',
    fail_closed_503: 'PASS', mutations_submitted: false }))
} finally {
  await browser.close()
}
