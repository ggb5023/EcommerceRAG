// Run against actual isolated Go/Python services. The replay scenario alters
// only transport delivery of genuine persisted frames, never business data.
import { chromium } from 'playwright'
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
const base = process.env.WORKSPACE_URL ?? 'http://127.0.0.1:5173'
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'isolated local URL required')
const output = path.resolve(fileURLToPath(new URL('../../.local/synthetic-workspace/browser/', import.meta.url)))
await mkdir(output, { recursive: true, mode: 0o700 })
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH ?? '/root/.cache/ms-playwright/chromium-1187/chrome-linux/chrome', headless: true })
const checks = []
const problems = []
let page
async function test(name, run) {
  try { await run(); checks.push({ name, status: 'PASS' }) }
  catch (error) {
    checks.push({ name, status: 'FAIL' })
    await page?.screenshot({ path: path.join(output, name.replace(/[^a-z0-9-]/gi, '-') + '-failure.png') }).catch(() => {})
    throw error
  }
}
async function visible(locator) { await locator.waitFor({ state: 'visible', timeout: 15000 }) }
async function newConversation() {
  if ((await page.viewportSize()).width < 720) await page.getByRole('button', { name: '会话', exact: true }).click()
  const created = page.waitForResponse(r => r.url().endsWith('/v1/conversations') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '＋ 新建会话', exact: true }).click()
  assert.equal((await created).status(), 201, 'actual conversation creation failed')
  await visible(page.locator('#question'))
}
async function send(query, complete = true) {
  await page.locator('#question').fill(query)
  const created = page.waitForResponse(r => /\/v1\/conversations\/[^/]+\/turns$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST')
  await page.locator('#question').press('Enter')
  const response = await created
  assert.equal(response.status(), 202, 'actual turn creation failed')
  const turn = await response.json()
  if (complete) await page.getByRole('button', { name: '发送 ↗', exact: true }).waitFor({ state: 'visible', timeout: 15000 })
  return turn
}
async function result(turn) {
  const response = await page.request.get(base + '/v1/turns/' + turn.request_id)
  assert.equal(response.status(), 200)
  return response.json()
}
async function openEvidence() {
  if ((await page.viewportSize()).width < 1024) await page.getByRole('button', { name: /^证据与引用 \(/ }).click()
  await visible(page.locator('#evidence-pane'))
}
async function closeEvidence() {
  if ((await page.viewportSize()).width < 1024) await page.keyboard.press('Escape')
}
async function restored(turn) {
  // Completion display can precede the final GET. Wait for the persisted result
  // binding before comparing the browser to the authoritative response.
  await page.waitForFunction(id => document.querySelector('.result-binding')?.textContent.includes(id), turn.request_id)
}
try {
  for (const [width, height] of [[1440,900],[1280,800],[1024,768],[390,844]]) {
    const context = await browser.newContext({ viewport: { width, height } })
    page = await context.newPage()
    page.on('pageerror', () => problems.push('browser_script_error'))
    await test(`viewport-${width}-${height}`, async () => {
      await page.goto(base + '/chat'); await visible(page.locator('#question'))
      const session = await (await page.request.get(base + '/v1/session')).json()
      assert.equal(session.profile, 'synthetic_import_mock')
      await newConversation()
      await page.locator('#question').focus()
      await page.keyboard.type('规格'); await page.keyboard.press('Shift+Enter')
      assert.equal(await page.locator('#question').inputValue(), '规格\n', 'Shift+Enter must insert newline')
      await page.keyboard.press('Tab')
      assert.ok(await page.getByRole('button', { name: '发送 ↗', exact: true }).evaluate(e => e === document.activeElement), 'Tab must reach send')
      // IME Enter must not send a partial Chinese composition.
      await page.locator('#question').evaluate(e => e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })))
      assert.equal(await page.locator('#question').inputValue(), '规格\n')
      const turn = await send('SKU-CUP-480 保温杯容量和材质？')
      await restored(turn)
      const before = await result(turn)
      assert.ok(before.evidence.some(e => e.documentId === 'syn-products-a'))
      assert.ok(before.answer.includes('304'))
      assert.equal(before.customer_reply.can_copy, false)
      await openEvidence()
      assert.ok(await page.locator('.citation').count() > 0, 'actual citations missing')
      assert.equal(await page.getByRole('button', { name: '复制草稿 · 模拟结果不可复制' }).isDisabled(), true)
      const source = await page.locator('.evidence-card footer').allTextContents()
      await closeEvidence()
      if (width < 1024) assert.ok(await page.getByRole('button', { name: /^证据与引用 \(/ }).evaluate(e => e === document.activeElement), 'Esc must restore opener focus')
      await page.reload(); await visible(page.locator('#question')); await restored(turn); await openEvidence()
      assert.deepEqual(await page.locator('.evidence-card footer').allTextContents(), source, 'refresh lost versions/evidence')
      assert.equal((await result(turn)).answer, before.answer)
      await closeEvidence()
      const layout = await page.locator('#question').boundingBox()
      assert.ok(layout && layout.y >= 0 && layout.y + layout.height <= height, 'input must remain in viewport')
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal overflow')
      if (width < 720) {
        await page.getByRole('button', { name: '会话', exact: true }).click()
        assert.equal(await page.locator('[role=dialog]:visible').count(), 1, 'at most one drawer')
        await page.keyboard.press('Shift+Tab')
        assert.ok(await page.evaluate(() => !!document.activeElement.closest('.drawer-open')), 'drawer focus trap')
        await page.keyboard.press('Escape')
      }
      await page.screenshot({ path: path.join(output, `viewport-${width}-${height}.png`) })
    })
    await context.close()
  }
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  page = await context.newPage(); page.on('pageerror', () => problems.push('browser_script_error'))
  await page.goto(base + '/chat'); await visible(page.locator('#question'))
  await test('asking-refresh-parent-child', async () => {
    await newConversation(); const parent = await send('那个怎么样？'); await restored(parent)
    await visible(page.getByText('需要补充信息', { exact: true }))
    assert.equal((await result(parent)).status, 'ASKING')
    await page.reload(); await visible(page.locator('#question')); await visible(page.getByText('需要补充信息', { exact: true }))
    const child = await send('SKU-CUP-480 保温杯容量？'); await restored(child)
    assert.equal((await result(child)).parent_turn_id, parent.turn_id)
  })
  await test('cancel-server-terminal', async () => {
    await newConversation(); const turn = await send('低款和高款收纳箱如何选？', false)
    await visible(page.getByRole('button', { name: '停止', exact: true }))
    await page.getByRole('button', { name: '停止', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.turn-state')?.textContent.includes('CANCELLED'))
    await restored(turn)
    assert.equal((await result(turn)).status, 'CANCELLED')
    await page.reload(); await visible(page.locator('#question'))
    assert.ok((await page.locator('.turn-state').textContent()).includes('CANCELLED'))
  })
  await test('active-turn-refresh', async () => {
    await newConversation(); const turn = await send('低款和高款收纳箱如何选？', false)
    await visible(page.getByRole('button', { name: '停止', exact: true })); await page.reload()
    await visible(page.getByRole('button', { name: '发送 ↗', exact: true })); await restored(turn)
    assert.equal(await page.locator('.message.assistant .bubble').last().textContent(), (await result(turn)).answer, 'active refresh duplicated or lost deltas')
  })
  await test('sse-disconnect-duplicate-gap-replay', async () => {
    await newConversation(); let requests = 0; const cursors = []
    await page.route('**/v1/turns/*/events', async route => {
      requests++; cursors.push(route.request().headers()['last-event-id'] ?? '')
      const response = await route.fetch(); const raw = await response.text()
      const frames = raw.split('\n\n').filter(frame => frame.includes('data: '))
      if (requests === 1) {
        // Same genuine first event twice, followed by genuine third event:
        // sequence2 is absent, so client must reconnect after accepted seq1.
        assert.ok(frames.length >= 3)
        await route.fulfill({ response, body: [frames[0],frames[0],frames[2]].join('\n\n') + '\n\n' })
      } else {
        await route.fulfill({ response, body: [frames[0],...frames].join('\n\n') + '\n\n' })
      }
    })
    const turn = await send('低款和高款收纳箱如何选？'); await restored(turn)
    assert.ok(requests >= 2 && cursors[1] === '1', 'Last-Event-ID replay missing')
    assert.equal(await page.locator('.message.assistant .bubble').last().textContent(), (await result(turn)).answer, 'replay duplicated or lost text')
    await page.unroute('**/v1/turns/*/events')
  })
  await test('history-evidence-selection', async () => {
    const first = await send('配送需要几个工作日？'); await restored(first)
    const second = await send('竹纤维毛巾怎么护理？'); await restored(second)
    await page.getByRole('button', { name: '查看本轮证据', exact: true }).nth(1).click()
    await restored(first)
    assert.ok((await page.locator('.evidence-card footer').allTextContents()).some(text => text.includes('syn-policy-a')))
  })
  await test('inaccessible-route-error', async () => {
    await page.goto(base + '/chat/0')
    await visible(page.getByText('会话不存在或无法访问', { exact: true }))
    assert.equal(await page.locator('.evidence-card:visible').count(), 0)
  })
  await test('dependency-error-retry', async () => {
    await page.route('**/v1/session', route => route.fulfill({ status: 503, json: { code: 'unavailable' } }))
    await page.goto(base + '/chat'); await visible(page.getByText('工作台暂不可用', { exact: true }))
    await page.unroute('**/v1/session'); await page.getByRole('button', { name: '重试', exact: true }).click(); await visible(page.locator('#question'))
  })
  await test('authorization-error-clears-ui', async () => {
    await newConversation(); const turn = await send('配送需要几个工作日？'); await restored(turn)
    await page.route('**/v1/turns/*', route => route.fulfill({ status: 403, json: { code: 'execution_permission_changed' } }))
    await page.getByRole('button', { name: '查看本轮证据', exact: true }).last().click()
    await visible(page.getByText('当前账号没有访问权限，请重新连接或联系管理员', { exact: true }))
    assert.equal(await page.locator('.evidence-card:visible,.message:visible,.draft-panel:visible').count(), 0)
    await page.unroute('**/v1/turns/*')
  })
  assert.equal(problems.length, 0, 'browser script errors')
  await context.close()
} finally {
  await writeFile(path.join(output, 'report.json'), JSON.stringify({ profile: 'synthetic_import_mock', real_service_acceptance: false, checks, script_error_count: problems.length, fault_injection: ['SSE transport truncation/dedup/gap', 'HTTP 503/403 UI handling; actual RBAC tested separately'] }, null, 2), { mode: 0o600 })
  await browser.close()
}
console.log(`PASS actual synthetic workspace browser: ${checks.length} checks, four viewports and keyboard`)
