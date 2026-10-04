<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, get, post, type AdminSession } from './api'
import { adminPageFromPath, adminPathForPage, type AdminPage } from './routes'

type Row = Record<string, any>
type ItemResponse = { items: Row[] }
type Detail = Row & { content?: { chunk_index: number; content: string }[] }

const route = useRoute()
const router = useRouter()
const session = ref<AdminSession | null>(null)
const data = ref<Row[]>([])
const detail = ref<Detail | null>(null)
const jobDetail = ref<Row | null>(null)
const loading = ref(true)
const unavailable = ref(false)
const error = ref('')
const actionBusy = ref(false)
const notice = ref('')
const selectedShop = ref('')
const formReason = ref('')
const dialog = ref<'request' | 'review' | 'version' | 'grant_revoke' | 'job' | null>(null)
const dialogTarget = ref<Row | null>(null)
const reviewDecision = ref('approved')
const disclosure = ref('external_allowed')
const manifestText = ref('')
const selectedFiles = ref<File[]>([])
const manifestFile = ref<File | null>(null)
const requestRole = ref('merchant_admin')
const requestScope = ref('shops')
const requestShops = ref<string[]>([])
const statusFilter = ref('')
const confirmationChecked = ref(false)

const page = computed(() => adminPageFromPath(route.path))
const title = computed(() => ({
  overview: '运行状态', accounts: '账号与权限', requests: '权限申请', audit: '授权审计',
  shops: '店铺范围', sources: '资料来源', ingestion: '摄取任务', reviews: '版本审核',
  'not-found': '页面不可用',
}[page.value]))
const roles = computed(() => new Set(session.value?.admin_roles ?? []))
const roleCatalog = computed(() => session.value?.admin_role_catalog ?? [])
const canAccess = computed(() => roles.value.has('access_admin'))
const canManage = computed(() => roles.value.has('merchant_admin'))
const canReview = computed(() => roles.value.has('knowledge_reviewer'))
const canDecideRequests = computed(() => canAccess.value || canManage.value)
const canObserve = computed(() => roles.value.has('platform_observer'))
const canSeeOperations = computed(() => canManage.value || canReview.value || canObserve.value)
const shopQuery = computed(() => selectedShop.value ? `?shop_id=${encodeURIComponent(selectedShop.value)}` : '')

function roleLabel(role: string) {
  return roleCatalog.value.find(item => item.storage_role === role || item.role_key === role)?.display_name ?? role
}

const requestableRoles = computed(() => roleCatalog.value.filter(item => item.enabled && item.requestable && item.storage_role))

const navigation = computed(() => {
  const items: { label: string; page: AdminPage; visible: boolean }[] = [
    { label: '运行状态', page: 'overview', visible: canSeeOperations.value },
    { label: '账号与权限', page: 'accounts', visible: canAccess.value },
    { label: '权限申请', page: 'requests', visible: true },
    { label: '授权审计', page: 'audit', visible: canAccess.value || canReview.value },
    { label: '店铺范围', page: 'shops', visible: canSeeOperations.value },
    { label: '资料来源', page: 'sources', visible: canSeeOperations.value },
    { label: '摄取任务', page: 'ingestion', visible: canSeeOperations.value },
    { label: '版本审核', page: 'reviews', visible: canReview.value },
  ]
  return items.filter(item => item.visible)
})

function endpointForPage() {
  const q = shopQuery.value
  switch (page.value) {
    case 'overview': return `/admin/v1/overview${q}`
    case 'accounts': return `/admin/v1/access/accounts${q}`
    case 'requests': return `/admin/v1/access/requests${statusFilter.value ? `${q ? '&' : '?'}status=${statusFilter.value}` : q}`
    case 'audit': return `/admin/v1/access/audit${q}`
    case 'shops': return `/admin/v1/merchant/shops${q}`
    case 'sources': return `/admin/v1/merchant/sources${q}`
    case 'ingestion': return `/admin/v1/merchant/ingestion${q}`
    case 'reviews': return `/admin/v1/merchant/reviews${q}`
    default: return `/admin/v1/overview${q}`
  }
}

function clearRestrictedContent(clearSession = true) {
  data.value = []
  detail.value = null
  jobDetail.value = null
  manifestText.value = ''
  manifestFile.value = null
  selectedFiles.value = []
  formReason.value = ''
  requestShops.value = []
  dialog.value = null
  dialogTarget.value = null
  confirmationChecked.value = false
  if (clearSession) session.value = null
}

function handleFailure(caught: unknown, fallback: string) {
  const apiError = caught instanceof ApiError ? caught : new ApiError(0, fallback)
  error.value = apiError.message
  notice.value = ''
  if (apiError.status === 401 || apiError.status === 403) {
    clearRestrictedContent(true)
    unavailable.value = false
  } else if (apiError.status >= 500 || apiError.status === 0) {
    clearRestrictedContent(false)
    unavailable.value = true
  }
}

async function loadPage() {
  loading.value = true
  unavailable.value = false
  error.value = ''
  notice.value = ''
  detail.value = null
  try {
    if (!session.value) session.value = await get<AdminSession>('/admin/v1/session')
    const response = await get<ItemResponse | Row>(endpointForPage())
    data.value = page.value === 'overview'
      ? [response as Row]
      : 'items' in response && Array.isArray(response.items) ? response.items : []
    if (page.value === 'shops') {
      const allowed = data.value.map(item => String(item.shop_id))
      if (!selectedShop.value || !allowed.includes(selectedShop.value)) selectedShop.value = session.value.shop_id || allowed[0] || ''
      requestShops.value = requestShops.value.filter(shop => allowed.includes(shop))
    }
  } catch (caught) {
    handleFailure(caught, '服务连接失败，请检查管理服务。')
  } finally {
    loading.value = false
  }
}

watch(() => [route.fullPath, selectedShop.value, statusFilter.value], () => { if (session.value || !loading.value) void loadPage() })
onMounted(() => { void loadPage() })

function go(target: AdminPage) { void router.push(adminPathForPage(target)) }
function shopName(id: string) { return data.value.find(item => item.shop_id === id)?.name ?? id }
function formatTime(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—' }
function statusLabel(value?: string) {
  const labels: Record<string, string> = { healthy: '正常', unavailable: '不可用', unknown: '未知', not_configured: '未配置',
    pending: '待处理', queued: '排队中', parsing: '解析中', building: '构建中', retry_wait: '等待重试',
    running: '运行中', awaiting_review: '待审核', done: '完成', failed: '失败', cancelled: '已取消', cancel_requested: '正在取消',
    ready: '待发布', active: '当前版本', superseded: '已替换', revoked: '已撤销', approved: '已批准', rejected: '已拒绝', needs_revision: '待补正' }
  return labels[value ?? ''] ?? value ?? '—'
}
function statusTone(value?: string) {
  if (['healthy', 'done', 'active', 'approved', 'success'].includes(value ?? '')) return 'good'
  if (['failed', 'unavailable', 'rejected', 'revoked', 'denied'].includes(value ?? '')) return 'bad'
  if (['unknown', 'not_configured'].includes(value ?? '')) return 'muted'
  return 'pending'
}

async function refresh() { await loadPage() }
function openDecision(row: Row, decision = 'approved', kind: 'request' | 'review' = 'request') {
  dialogTarget.value = row
  reviewDecision.value = decision
  formReason.value = ''
  confirmationChecked.value = false
  dialog.value = kind
}
function openVersion(row: Row, operation: 'publish' | 'rollback' | 'revoke') {
  dialogTarget.value = { ...row, operation }
  formReason.value = ''
  confirmationChecked.value = false
  dialog.value = 'version'
}
function closeDialog() { dialog.value = null; dialogTarget.value = null; confirmationChecked.value = false }

async function decideRequest() {
  if (!dialogTarget.value || formReason.value.trim().length < 10) return
  actionBusy.value = true
  try {
    await post(`/admin/v1/access/requests/${dialogTarget.value.id}/decision`, {
      confirm: true, decision: reviewDecision.value, reason: formReason.value.trim(),
    })
    notice.value = '权限申请已处理，审计结果已记录。'
    closeDialog(); await loadPage()
  } catch (caught) { handleFailure(caught, '权限申请处理失败。') }
  finally { actionBusy.value = false }
}

async function submitPermissionRequest() {
  if (formReason.value.trim().length < 10) return
  actionBusy.value = true
  try {
    await post('/admin/v1/access/requests', { role: requestRole.value, scope_kind: requestScope.value,
      shop_ids: requestScope.value === 'tenant' ? [] : requestShops.value, reason: formReason.value.trim() })
    notice.value = '权限申请已提交。'
    formReason.value = ''; await loadPage()
  } catch (caught) { handleFailure(caught, '权限申请提交失败。') }
  finally { actionBusy.value = false }
}

async function revokeGrant(row: Row) {
  if (formReason.value.trim().length < 10) return
  actionBusy.value = true
  try {
    await post(`/admin/v1/access/grants/${row.id}/revoke`, { confirm: true, reason: formReason.value.trim() })
    notice.value = '授权已撤销，审计结果已记录。'; closeDialog(); await loadPage()
  } catch (caught) { handleFailure(caught, '授权撤销失败。') }
  finally { actionBusy.value = false }
}

function submitDialogAction() {
  const target = dialogTarget.value
  if (!dialog.value || !target || !confirmationChecked.value) return
  if (dialog.value === 'request') void decideRequest()
  else if (dialog.value === 'review') void decideVersion()
  else if (dialog.value === 'grant_revoke') void revokeGrant(target)
  else if (dialog.value === 'job') void runJobOperation()
  else void runVersionOperation()
}

async function loadReviewDetail(row: Row) {
  error.value = ''
  detail.value = null
  try { detail.value = await get<Detail>(`/admin/v1/merchant/reviews/${row.version_id}${shopQuery.value}`) }
  catch (caught) { handleFailure(caught, '审核资料不可用。') }
}

async function loadJobDetail(row: Row) {
  jobDetail.value = null
  error.value = ''
  try { jobDetail.value = await get<Row>(`/admin/v1/merchant/ingestion/${row.job_id}${shopQuery.value}`) }
  catch (caught) { handleFailure(caught, '摄取任务明细不可用。') }
}

function openJobAction(row: Row, operation: 'cancel' | 'retry') {
  dialogTarget.value = { ...row, operation }
  formReason.value = ''
  confirmationChecked.value = false
  dialog.value = 'job'
}

async function runJobOperation() {
  const row = dialogTarget.value
  if (!row || formReason.value.trim().length < 10) return
  actionBusy.value = true
  try {
    const body = { confirm: true, tenant_id: session.value?.tenant_id, shop_id: row.shop_id,
      job_id: row.job_id, permission_revision: session.value?.permission_revision,
      expected_fencing_epoch: row.fencing_epoch, reason: formReason.value.trim() }
    await post(`/admin/v1/merchant/ingestion/${row.job_id}/${row.operation}`, body)
    notice.value = row.operation === 'cancel' ? '取消请求已记录，worker 正在处理终态。' : '重试任务已重新排队。'
    closeDialog(); await loadPage()
    const refreshed = data.value.find(item => item.job_id === row.job_id)
    if (refreshed) await loadJobDetail(refreshed)
  } catch (caught) { handleFailure(caught, '任务操作失败，状态未确认。') }
  finally { actionBusy.value = false }
}

function openGrantRevoke(row: Row) {
  dialogTarget.value = row
  formReason.value = ''
  confirmationChecked.value = false
  dialog.value = 'grant_revoke'
}

async function decideVersion() {
  const row = dialogTarget.value
  if (!row || formReason.value.trim().length < 10) return
  actionBusy.value = true
  try {
    await post(`/admin/v1/merchant/versions/${row.version_id}/review`, {
      confirm: true, decision: reviewDecision.value, disclosure_class: disclosure.value,
      external_allowed: disclosure.value === 'external_allowed', expected_hash: row.source_hash,
      expected_fencing_epoch: row.fencing_epoch, permission_revision: session.value?.permission_revision,
      reason: formReason.value.trim(),
    })
    notice.value = '审核结论已记录。'
    closeDialog(); detail.value = null; await loadPage()
  } catch (caught) { handleFailure(caught, '审核结果未提交。') }
  finally { actionBusy.value = false }
}

async function runVersionOperation() {
  const row = dialogTarget.value
  if (!row || formReason.value.trim().length < 10) return
  actionBusy.value = true
  try {
    const body = { confirm: true, tenant_id: session.value?.tenant_id, shop_id: row.shop_id,
      document_id: row.document_id, version_hash: row.source_hash,
      permission_revision: session.value?.permission_revision,
      expected_fencing_epoch: row.fencing_epoch, reason: formReason.value.trim() }
    await post(`/admin/v1/merchant/versions/${row.version_id}/${row.operation}`, body)
    notice.value = `版本${row.operation === 'publish' ? '已发布' : row.operation === 'rollback' ? '已回退' : '已撤销'}，索引和审计状态已刷新。`
    closeDialog(); detail.value = null; await loadPage()
  } catch (caught) { handleFailure(caught, '版本操作失败，状态未确认。') }
  finally { actionBusy.value = false }
}

async function importPackage() {
  if (!manifestText.value.trim() || !manifestFile.value || selectedFiles.value.length === 0 || !selectedShop.value) return
  const form = new FormData()
  form.set('shop_id', selectedShop.value)
  form.set('manifest', manifestText.value)
  for (const file of selectedFiles.value) {
    const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
    form.append('paths', relative)
    form.append('files', file, file.name)
  }
  actionBusy.value = true
  try {
    const response = await fetch('/admin/v1/merchant/sources/import', { method: 'POST', credentials: 'same-origin', body: form })
    if (!response.ok) throw new ApiError(response.status, `资料导入失败，HTTP ${response.status}。`)
    const result = await response.json() as Row
    notice.value = `摄取任务已排队：${result.item_count} 个资料项，状态 ${statusLabel(result.status)}。`
    manifestText.value = ''; manifestFile.value = null; selectedFiles.value = []
    await loadPage()
  } catch (caught) { handleFailure(caught, '资料导入失败。') }
  finally { actionBusy.value = false }
}

function onManifestChange(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] ?? null
  manifestFile.value = file
  if (file) void file.text().then(value => { manifestText.value = value })
}
function onFilesChange(event: Event) { selectedFiles.value = Array.from((event.target as HTMLInputElement).files ?? []) }
</script>

<template>
  <div class="admin-shell">
    <aside class="rail">
      <a class="brand" href="/admin/overview" @click.prevent="go('overview')"><span class="brand-mark">ER</span><span><strong>知识管理</strong><small>运营中控台</small></span></a>
      <div class="rail-label">工作空间</div>
      <nav aria-label="管理工作空间">
        <button v-for="item in navigation" :key="item.page" type="button" class="nav-item" :class="{ active: page === item.page }" @click="go(item.page)">
          <span class="nav-mark" aria-hidden="true">{{ ({ overview: '◫', accounts: '♙', requests: '⇄', audit: '≡', shops: '⌂', sources: '▤', ingestion: '↻', reviews: '✓' } as Record<string, string>)[item.page] }}</span>
          {{ item.label }}
        </button>
      </nav>
      <div class="rail-bottom" v-if="session">
        <span class="env-mark">{{ session.is_mock ? 'SYNTHETIC' : 'CONNECTED' }}</span>
        <strong>{{ session.tenant_id }}</strong>
        <small>{{ session.runtime_role }} · {{ session.shop_id }}</small>
      </div>
    </aside>

    <main class="main-area">
      <header class="page-head">
        <div><p class="eyebrow">ECOMMERCE RAG / ADMIN</p><h1>{{ title }}</h1></div>
        <div class="head-actions">
          <label v-if="data.some(item => item.shop_id)" class="shop-picker">店铺
            <select v-model="selectedShop" aria-label="筛选店铺"><option value="">全部授权店铺</option><option v-for="shop in session?.allowed_shop_ids ?? []" :key="shop" :value="shop">{{ shop }}</option></select>
          </label>
          <span v-if="session" class="identity">{{ session.user_id }} <small>{{ session.admin_roles?.map(roleLabel).join(' · ') || '无管理授权' }}</small></span>
          <button class="icon-action" type="button" title="刷新" aria-label="刷新数据" @click="refresh">↻</button>
        </div>
      </header>

      <div v-if="error" class="alert" role="alert"><span>{{ error }}</span><button type="button" @click="refresh">重试</button></div>
      <div v-if="notice" class="notice" role="status">{{ notice }}</div>
      <section v-if="loading" class="state-panel" role="status"><strong>正在读取授权范围内的数据</strong><span>状态以服务端响应为准。</span></section>
      <section v-else-if="unavailable" class="state-panel unavailable"><strong>管理服务暂不可用</strong><span>没有使用缓存内容替代服务端状态。</span><button class="primary" @click="refresh">重试</button></section>
      <section v-else-if="!session" class="state-panel unavailable"><strong>没有可用的管理会话</strong><span>请使用已配置的身份服务建立会话。</span></section>
      <section v-else-if="navigation.length === 0" class="state-panel unavailable"><strong>当前账号没有管理权限</strong><span>页面访问范围由 Go 服务端授权决定。</span></section>
      <template v-else-if="!loading">
        <section v-if="page === 'not-found'" class="state-panel unavailable"><strong>管理页面不存在</strong><span>请从当前账号可访问的工作空间进入。</span><button class="secondary" type="button" @click="go('overview')">运行状态</button></section>
        <section v-if="page === 'overview'" class="content-area">
          <div class="overview-meta"><span>只读运行信息</span><span>15 分钟窗口 · {{ formatTime(new Date().toISOString()) }}</span></div>
          <div class="health-grid">
            <article v-for="(component, name) in (data[0]?.components ?? {})" :key="name" class="health-cell">
              <span>{{ ({ go: 'Go 网关', python: 'Python RAG', postgresql: 'PostgreSQL', redis: 'Redis', worker: '摄取 Worker' } as Record<string, string>)[name] ?? name }}</span>
              <strong :class="`tone-${statusTone(component.status)}`">{{ statusLabel(component.status) }}</strong>
              <small>{{ component.version ?? component.role ?? component.detail ?? '运行状态' }}</small>
            </article>
          </div>
          <div class="summary-grid">
            <article><span>迁移版本</span><strong>{{ data[0]?.migration?.current_version ?? '—' }}</strong><small>只读检查</small></article>
            <article><span>摄取任务</span><strong>{{ data[0]?.ingestion?.jobs ?? 0 }}</strong><small>待处理 {{ data[0]?.ingestion?.awaiting_or_running ?? 0 }} · 失败 {{ data[0]?.ingestion?.failed ?? 0 }}</small></article>
            <article><span>请求错误率</span><strong>{{ ((data[0]?.error_rate?.rate ?? 0) * 100).toFixed(1) }}%</strong><small>{{ data[0]?.error_rate?.errors ?? 0 }} / {{ data[0]?.error_rate?.requests ?? 0 }} 次请求</small></article>
            <article><span>应用版本</span><strong>{{ data[0]?.version?.application ?? '—' }}</strong><small>{{ data[0]?.version?.revision ?? 'revision unknown' }}</small></article>
          </div>
          <p class="scope-note">授权范围：{{ data[0]?.scope?.tenant_id }} / {{ data[0]?.scope?.shop_id }}。历史迁移摘要未验证：{{ data[0]?.migration?.legacy_unverified ?? '—' }}。</p>
        </section>

        <section v-else-if="page === 'accounts'" class="content-area">
          <div class="section-head"><div><h2>授权范围内的账号</h2><p>撤权状态和权限版本由服务端当前记录决定。</p></div></div>
          <div class="table-wrap"><table><thead><tr><th>账号</th><th>运行角色</th><th>店铺范围</th><th>管理角色</th><th>权限版本</th><th>状态</th></tr></thead>
            <tbody><tr v-for="row in data" :key="row.user_id"><td><strong>{{ row.display_name }}</strong><small>{{ row.external_id }} · {{ row.user_id }}</small></td><td>{{ row.runtime_role }}</td><td>{{ (row.shop_ids ?? []).join(', ') || '租户级' }}</td><td><div v-for="grant in row.grants ?? []" :key="grant.id" class="grant-line"><span>{{ roleLabel(grant.role) }} <small>({{ grant.role }})</small> · {{ grant.scope_kind === 'tenant' ? '租户' : grant.shop_ids.join(', ') }}</span><button v-if="canAccess && grant.status === 'active'" class="text-action danger-text" @click="openGrantRevoke({ ...grant, external_id: row.external_id, display_name: row.display_name })">撤销</button></div><span v-if="!row.grants?.length">—</span></td><td>{{ row.permission_revision }}</td><td><span class="status" :class="`tone-${row.revoked ? 'bad' : 'good'}`">{{ row.revoked ? '已撤权' : '有效' }}</span></td></tr>
              <tr v-if="data.length === 0"><td colspan="6" class="empty">当前授权范围没有账号记录。</td></tr></tbody></table></div>
        </section>

        <section v-else-if="page === 'requests'" class="content-area split-layout">
          <div class="primary-column"><div class="section-head"><div><h2>权限申请队列</h2><p>审批决定不会自动扩大当前授权的 tenant/shop 范围。</p></div><select v-model="statusFilter" aria-label="申请状态"><option value="">全部状态</option><option value="pending">待处理</option><option value="approved">已批准</option><option value="needs_revision">待补正</option><option value="rejected">已拒绝</option></select></div>
            <div class="table-wrap"><table><thead><tr><th>申请人 / 角色</th><th>范围</th><th>理由</th><th>状态</th><th>操作</th></tr></thead><tbody>
              <tr v-for="row in data" :key="row.id"><td><strong>{{ row.requester_name }}</strong><small>{{ row.role }} · {{ row.id }}</small></td><td>{{ row.scope_kind === 'tenant' ? '租户' : row.shop_ids.join(', ') }}</td><td class="reason-cell">{{ row.reason }}</td><td><span class="status" :class="`tone-${statusTone(row.status)}`">{{ statusLabel(row.status) }}</span></td><td><button v-if="canDecideRequests && row.status === 'pending'" class="text-action" @click="openDecision(row, 'approved')">处理</button></td></tr>
              <tr v-if="data.length === 0"><td colspan="5" class="empty">没有符合筛选条件的申请。</td></tr></tbody></table></div>
          </div>
          <form v-if="session" class="side-form" @submit.prevent="submitPermissionRequest"><h2>提交权限申请</h2><label>申请角色<select v-model="requestRole"><option v-for="role in requestableRoles" :key="role.role_key" :value="role.storage_role ?? ''">{{ role.display_name }}（{{ role.role_key }}）</option></select></label><small v-if="requestableRoles.length === 0" class="form-warning">当前角色目录没有开放可申请的权限。</small><label>授权范围<select v-model="requestScope"><option value="shops">指定店铺</option><option value="tenant">当前租户</option></select></label><fieldset v-if="requestScope === 'shops'"><legend>选择店铺</legend><label v-for="shop in session.allowed_shop_ids" :key="shop" class="check-row"><input v-model="requestShops" type="checkbox" :value="shop">{{ shop }}</label></fieldset><label>申请理由<textarea v-model="formReason" minlength="10" maxlength="2000" rows="4" required /></label><button class="primary" :disabled="actionBusy || requestableRoles.length === 0">提交申请</button></form>
        </section>

        <section v-else-if="page === 'audit'" class="content-area"><div class="section-head"><div><h2>追加式授权审计</h2><p>按当前权限范围显示的近期管理动作。</p></div></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>资源</th><th>版本 / Hash</th><th>原因与结果</th></tr></thead><tbody>
          <tr v-for="row in data" :key="row.id"><td>{{ formatTime(row.created_at) }}</td><td>{{ row.actor_user_id }}</td><td>{{ row.action }}</td><td>{{ row.resource_type }} · {{ row.resource_id }}</td><td><span>{{ row.resource_version ?? '—' }}</span><small class="hash">{{ row.content_hash ?? '' }}</small></td><td>{{ row.reason ?? '—' }}<small><span class="status" :class="`tone-${statusTone(row.result)}`">{{ row.result }}</span></small></td></tr>
          <tr v-if="data.length === 0"><td colspan="6" class="empty">暂无审计记录。</td></tr></tbody></table></div></section>

        <section v-else-if="page === 'shops'" class="content-area"><div class="section-head"><div><h2>当前授权店铺</h2><p>列表内容来自服务端权限摘要和店铺接口。</p></div></div><div class="shop-grid"><article v-for="row in data" :key="row.shop_id" class="shop-row"><span class="shop-symbol">⌂</span><div><strong>{{ row.name }}</strong><small>{{ row.shop_id }}</small></div><span class="status" :class="`tone-${statusTone(row.status === 'active' ? 'healthy' : row.status)}`">{{ statusLabel(row.status) }}</span></article></div></section>

        <section v-else-if="page === 'sources'" class="content-area">
          <div class="section-head"><div><h2>已登记资料来源</h2><p>只显示来源元数据，不在此页面展开资料正文。</p></div></div>
          <div class="table-wrap"><table><thead><tr><th>来源</th><th>店铺</th><th>类型</th><th>资料数</th><th>最近导入</th></tr></thead><tbody><tr v-for="row in data" :key="row.source_id"><td><strong>{{ row.source_id }}</strong><small>{{ row.uri ?? 'manifest package' }}</small></td><td>{{ row.shop_id }}</td><td>{{ row.type }}</td><td>{{ row.documents }}</td><td>{{ formatTime(row.latest_import) }}</td></tr><tr v-if="data.length === 0"><td colspan="5" class="empty">尚无管理台导入的资料来源。</td></tr></tbody></table></div>
          <form v-if="canManage" class="import-form" @submit.prevent="importPackage"><div class="section-head"><div><h2>导入 YAML 资料包</h2><p>文件只进入当前授权店铺；新版本保持待审核，不会直接发布。</p></div><span class="env-mark">{{ session?.is_mock ? '合成环境' : '受限环境' }}</span></div>
            <div class="form-grid"><label>目标店铺<select v-model="selectedShop" required><option v-for="shop in session?.allowed_shop_ids ?? []" :key="shop" :value="shop">{{ shop }}</option></select></label><label>manifest.yaml<input type="file" accept=".yaml,.yml" required @change="onManifestChange"></label><label class="wide">资料文件<input type="file" multiple required @change="onFilesChange"><small>manifest 中的相对路径必须与文件名对应；单个文件上限由服务端执行。</small></label><label class="wide">YAML manifest<textarea v-model="manifestText" rows="10" spellcheck="false" required /></label></div>
            <div class="form-footer"><span>{{ selectedFiles.length }} 个文件已选择</span><button class="primary" :disabled="actionBusy || !manifestText || selectedFiles.length === 0">校验并排队</button></div>
          </form>
        </section>

        <section v-else-if="page === 'ingestion'" class="content-area">
          <div class="section-head"><div><h2>资料摄取任务</h2><p>任务持久化在 PostgreSQL；worker 重启后依据租约和 fencing epoch 接管。</p></div></div>
          <div class="table-wrap"><table><thead><tr><th>任务</th><th>阶段</th><th>状态</th><th>资料项</th><th>清单 / 数据摘要</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
            <tr v-for="row in data" :key="row.job_id" class="click-row" tabindex="0" @click="loadJobDetail(row)" @keydown.enter="loadJobDetail(row)">
              <td>{{ row.job_id }}</td><td>{{ row.stage }}</td><td><span class="status" :class="`tone-${statusTone(row.status)}`">{{ statusLabel(row.status) }}</span></td>
              <td>{{ row.item_count }}<small>待 {{ row.pending }} · 完成 {{ row.done }} · 失败 {{ row.failed }}</small></td>
              <td><small class="hash">M {{ row.manifest_sha256 }}</small><small class="hash">D {{ row.dataset_sha256 ?? '解析后生成' }}</small></td>
              <td>{{ formatTime(row.created_at) }}</td><td class="job-actions">
                <button v-if="canManage && ['pending','running'].includes(row.status)" class="text-action danger-text" @click.stop="openJobAction(row, 'cancel')">取消</button>
                <button v-if="canManage && row.status === 'failed'" class="text-action" @click.stop="openJobAction(row, 'retry')">重试</button>
              </td>
            </tr><tr v-if="data.length === 0"><td colspan="7" class="empty">暂无摄取任务。</td></tr>
          </tbody></table></div>
          <div v-if="jobDetail" class="job-items">
            <div class="section-head"><div><h2>任务 {{ jobDetail.job_id }} 的资料项</h2><p>{{ statusLabel(jobDetail.status) }} · {{ jobDetail.shop_id }} · fence {{ jobDetail.fencing_epoch }} · {{ jobDetail.cancel_requested ? '取消请求已记录' : '无取消请求' }}</p></div>
              <button v-if="canManage && ['pending','running'].includes(jobDetail.status)" class="text-action danger-text" @click="openJobAction(jobDetail, 'cancel')">取消任务</button>
              <button v-if="canManage && jobDetail.status === 'failed'" class="text-action" @click="openJobAction(jobDetail, 'retry')">重试任务</button>
            </div>
            <div class="table-wrap"><table><thead><tr><th>资料</th><th>状态 / 阶段</th><th>版本状态</th><th>Payload hash</th><th>重试 / Fence</th><th>错误代码</th></tr></thead><tbody>
              <tr v-for="item in jobDetail.items" :key="item.item_id"><td>{{ item.document_id }}<small>{{ item.item_id }}</small></td><td>{{ statusLabel(item.status) }} / {{ item.stage }}</td><td>{{ statusLabel(item.version_status) }} · {{ statusLabel(item.review_status) }}</td><td class="hash">{{ item.payload_hash }}</td><td>{{ item.retry_count }} / {{ item.fencing_epoch }}</td><td>{{ item.error_code ?? '—' }}</td></tr>
              <tr v-if="!jobDetail.items?.length"><td colspan="6" class="empty">任务没有资料项记录。</td></tr>
            </tbody></table></div>
          </div>
        </section>

        <section v-else-if="page === 'reviews'" class="content-area review-layout">
          <div class="review-list"><div class="section-head"><div><h2>资料版本</h2><p>审核基于版本哈希和权限版本。</p></div></div><div v-for="row in data" :key="row.version_id" class="version-row" :class="{ selected: detail?.version_id === row.version_id }" role="button" tabindex="0" @click="loadReviewDetail(row)" @keydown.enter="loadReviewDetail(row)"><div class="version-title"><strong>{{ row.title }}</strong><span class="status" :class="`tone-${statusTone(row.status)}`">{{ statusLabel(row.status) }}</span></div><small>{{ row.document_id }} · {{ row.shop_id }} · {{ statusLabel(row.review_status) }}</small><small class="hash">{{ row.source_hash }}</small><div class="version-meta"><span>{{ row.chunk_count ?? 0 }} chunks</span><span>fence {{ row.fencing_epoch }}</span><span>{{ row.disclosure_class }}</span></div></div><p v-if="data.length === 0" class="empty">当前范围没有资料版本。</p></div>
          <div class="review-detail"><template v-if="detail"><div class="detail-head"><div><p class="eyebrow">VERSION {{ detail.version_id }}</p><h2>{{ detail.title }}</h2></div><span class="status" :class="`tone-${statusTone(detail.review_status)}`">{{ statusLabel(detail.review_status) }}</span></div><dl class="detail-grid"><div><dt>租户</dt><dd>{{ session.tenant_id }}</dd></div><div><dt>店铺</dt><dd>{{ detail.shop_id }}</dd></div><div><dt>文档</dt><dd>{{ detail.document_id }}</dd></div><div><dt>状态</dt><dd>{{ statusLabel(detail.status) }}</dd></div><div><dt>披露分类</dt><dd>{{ detail.disclosure_class }}</dd></div><div><dt>Fencing epoch</dt><dd>{{ detail.fencing_epoch }}</dd></div><div class="wide"><dt>Source hash</dt><dd class="hash">{{ detail.source_hash }}</dd></div><div class="wide"><dt>Reviewed hash</dt><dd class="hash">{{ detail.reviewed_hash ?? '尚未审核' }}</dd></div></dl>
              <div class="content-preview"><div class="preview-head"><strong>审核内容</strong><span>{{ detail.disclosure_class === 'internal_only' ? '内部资料 · 仅授权审核员' : detail.disclosure_class === 'unclassified' ? '未分类 · 不进入检索' : '授权审核范围' }}</span></div><article v-for="chunk in detail.content ?? []" :key="chunk.chunk_index"><small>CHUNK {{ chunk.chunk_index + 1 }}</small><p>{{ chunk.content }}</p></article><p v-if="!detail.content?.length" class="empty">无可展示的内容片段。</p></div>
              <div class="high-risk-actions"><button v-if="canReview && detail.status === 'ready' && detail.review_status === 'pending'" class="primary" @click="openDecision(detail, 'approved', 'review')">记录审核结论</button><button v-if="canReview && detail.status === 'ready' && detail.review_status === 'approved'" class="primary" @click="openVersion(detail, 'publish')">发布此版本</button><button v-if="canReview && detail.status === 'superseded' && detail.review_status === 'approved'" class="secondary" @click="openVersion(detail, 'rollback')">回退到此版本</button><button v-if="canReview && detail.status !== 'revoked'" class="danger-button" @click="openVersion(detail, 'revoke')">撤销版本</button></div>
              <div class="reviewer-line">审核账号：{{ detail.reviewed_by ?? '待审核' }} · 审计结果由服务端返回</div>
            </template><div v-else class="detail-placeholder"><span class="placeholder-mark">▤</span><strong>选择一个版本查看审核信息</strong><small>只有当前审核授权范围内的版本会出现在列表中。</small></div></div>
        </section>
      </template>
    </main>

    <div v-if="dialog" class="modal-backdrop" @click.self="closeDialog"><section class="modal" role="dialog" aria-modal="true" :aria-label="dialog === 'version' ? '确认版本操作' : dialog === 'review' ? '确认资料审核' : dialog === 'grant_revoke' ? '确认撤销授权' : dialog === 'job' ? '确认摄取任务操作' : '提交权限决定'"><header><div><p class="eyebrow">{{ ['version','review','grant_revoke','job'].includes(dialog) ? 'HIGH RISK OPERATION' : 'ACCESS DECISION' }}</p><h2>{{ dialog === 'version' ? (dialogTarget?.operation === 'publish' ? '发布资料版本' : dialogTarget?.operation === 'rollback' ? '回退资料版本' : '撤销资料版本') : dialog === 'review' ? '确认资料审核结论' : dialog === 'grant_revoke' ? '撤销管理授权' : dialog === 'job' ? (dialogTarget?.operation === 'cancel' ? '取消摄取任务' : '重试摄取任务') : '处理权限申请' }}</h2></div><button class="icon-action" aria-label="关闭" @click="closeDialog">×</button></header>
      <div v-if="dialog === 'version' && dialogTarget" class="confirmation-facts"><div><span>Tenant</span><strong>{{ session?.tenant_id }}</strong></div><div><span>Shop</span><strong>{{ dialogTarget.shop_id }}</strong></div><div><span>Document / Version</span><strong>{{ dialogTarget.document_id }} / {{ dialogTarget.version_id }}</strong></div><div><span>Version hash</span><strong class="hash">{{ dialogTarget.source_hash }}</strong></div><div><span>披露分类 / 外部资格</span><strong>{{ dialogTarget.disclosure_class }} · {{ dialogTarget.external_allowed ? '可对外检索' : '禁止对外检索' }}</strong></div><div><span>Permission revision</span><strong>{{ session?.permission_revision }}</strong></div><div><span>Fencing epoch</span><strong>{{ dialogTarget.fencing_epoch }}</strong></div><div><span>当前审核权限</span><strong>{{ [...roles].join(', ') }}</strong></div></div>
      <div v-if="dialog === 'review' && dialogTarget" class="confirmation-facts"><div><span>Tenant / Shop</span><strong>{{ session?.tenant_id }} / {{ dialogTarget.shop_id }}</strong></div><div><span>Document / Version</span><strong>{{ dialogTarget.document_id }} / {{ dialogTarget.version_id }}</strong></div><div><span>Source hash</span><strong class="hash">{{ dialogTarget.source_hash }}</strong></div><div><span>Permission revision</span><strong>{{ session?.permission_revision }}</strong></div><div><span>Fencing epoch</span><strong>{{ dialogTarget.fencing_epoch }}</strong></div><div><span>当前审核权限</span><strong>{{ [...roles].join(', ') }}</strong></div></div>
      <div v-if="dialog === 'grant_revoke' && dialogTarget" class="confirmation-facts"><div><span>Tenant</span><strong>{{ session?.tenant_id }}</strong></div><div><span>账号</span><strong>{{ dialogTarget.display_name }} · {{ dialogTarget.external_id }}</strong></div><div><span>角色</span><strong>{{ dialogTarget.role }}</strong></div><div><span>授权范围</span><strong>{{ dialogTarget.scope_kind === 'tenant' ? '当前租户' : dialogTarget.shop_ids.join(', ') }}</strong></div><div><span>Permission revision</span><strong>{{ session?.permission_revision }}</strong></div><div><span>当前权限</span><strong>{{ [...roles].join(', ') }}</strong></div></div>
      <div v-if="dialog === 'request' && dialogTarget" class="confirmation-facts"><div><span>Tenant / Shop</span><strong>{{ session?.tenant_id }} / {{ dialogTarget.shop_ids?.join(', ') || '租户级' }}</strong></div><div><span>申请人 / 目标角色</span><strong>{{ dialogTarget.requester_name }} / {{ dialogTarget.role }}</strong></div><div><span>申请 ID</span><strong>{{ dialogTarget.id }}</strong></div><div><span>当前权限范围</span><strong>{{ [...roles].join(', ') }} · {{ session?.allowed_shop_ids.join(', ') }}</strong></div></div>
      <div v-if="dialog === 'job' && dialogTarget" class="confirmation-facts"><div><span>Tenant</span><strong>{{ session?.tenant_id }}</strong></div><div><span>Shop</span><strong>{{ dialogTarget.shop_id }}</strong></div><div><span>Ingestion job</span><strong>{{ dialogTarget.job_id }}</strong></div><div><span>当前状态</span><strong>{{ statusLabel(dialogTarget.status) }}</strong></div><div><span>Permission revision</span><strong>{{ session?.permission_revision }}</strong></div><div><span>Fencing epoch</span><strong>{{ dialogTarget.fencing_epoch }}</strong></div><div class="wide"><span>当前管理权限</span><strong>{{ [...roles].join(', ') }}</strong></div></div>
      <template v-if="dialog === 'request'"><label>决定<select v-model="reviewDecision"><option value="approved">批准</option><option value="rejected">拒绝</option><option value="needs_revision">退回补正</option></select></label><label>权限申请理由<input :value="dialogTarget?.reason" disabled></label></template>
      <template v-if="dialog === 'review'"><label>审核结论<select v-model="reviewDecision"><option value="approved">批准版本</option><option value="rejected">拒绝版本</option></select></label><label>披露分类<select v-model="disclosure"><option value="external_allowed">external_allowed · 可对外检索</option><option value="internal_only">internal_only · 仅内部</option><option value="unclassified">unclassified · 未分类</option></select></label></template>
      <label>操作理由<textarea v-model="formReason" minlength="10" maxlength="2000" rows="4" required /></label>
      <label class="confirm-row"><input v-model="confirmationChecked" type="checkbox">我已核对以上租户、店铺、对象和操作。</label>
      <footer><button class="secondary" @click="closeDialog">取消</button><button class="primary" :disabled="actionBusy || formReason.trim().length < 10 || !confirmationChecked" @click="submitDialogAction">{{ actionBusy ? '提交中…' : '确认并记录审计' }}</button></footer>
    </section></div>
  </div>
</template>

<style>
:root{font-family:Inter,ui-sans-serif,system-ui,"Microsoft YaHei",sans-serif;color:#1b2b26;background:#f4f7f5;font-synthesis:none}*{box-sizing:border-box}body{margin:0;min-width:320px}button,input,select,textarea{font:inherit}button{cursor:pointer}.admin-shell{min-height:100dvh;display:grid;grid-template-columns:234px minmax(0,1fr)}.rail{position:sticky;top:0;height:100dvh;padding:21px 14px 15px;background:#163c33;color:#eaf3ef;display:flex;flex-direction:column}.brand{display:flex;align-items:center;gap:11px;padding:0 9px 22px;color:inherit;text-decoration:none;border-bottom:1px solid #ffffff20}.brand-mark{width:34px;height:34px;display:grid;place-items:center;background:#dceee3;color:#164336;border-radius:7px;font-weight:800;font-size:11px}.brand strong,.brand small{display:block}.brand strong{font-size:13px}.brand small{margin-top:2px;color:#a6c1b4;font-size:10px}.rail-label{padding:22px 10px 9px;color:#89aa9a;font-size:10px}.rail nav{display:grid;gap:3px}.nav-item{display:flex;align-items:center;gap:11px;width:100%;min-height:39px;padding:0 10px;border:0;border-left:2px solid transparent;background:transparent;text-align:left;color:#c5d7ce;font-size:12px}.nav-item:hover,.nav-item.active{background:#ffffff12;color:#fff}.nav-item.active{border-left-color:#8fd1a9}.nav-mark{width:17px;text-align:center;color:#a7c6b4;font-size:15px}.rail-bottom{margin-top:auto;border-top:1px solid #ffffff20;padding:13px 9px 2px;display:grid;gap:6px}.rail-bottom strong{font-size:11px;overflow-wrap:anywhere}.rail-bottom small{font-size:10px;color:#a6c1b4}.env-mark{justify-self:start;padding:3px 6px;border-radius:3px;background:#d9eee1;color:#236248;font-size:9px;font-weight:800}.main-area{min-width:0;padding:0 28px 36px}.page-head{position:sticky;top:0;z-index:5;min-height:91px;display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0 -28px 20px;padding:14px 28px;border-bottom:1px solid #e1e9e4;background:#fff}.eyebrow{margin:0 0 5px;color:#82958c;font-size:9px;font-weight:700;letter-spacing:0}.page-head h1{margin:0;font-size:19px}.head-actions{display:flex;align-items:center;gap:12px}.identity{display:grid;text-align:right;font-size:11px;font-weight:700}.identity small{margin-top:3px;color:#82938a;font-size:9px;font-weight:400}.shop-picker{display:flex;align-items:center;gap:7px;color:#66776e;font-size:10px}.shop-picker select{max-width:155px}.icon-action{width:32px;height:32px;border:1px solid #dce6df;border-radius:5px;background:#fff;color:#316b54;font-size:18px}.shop-picker select,select,input,textarea{min-height:35px;border:1px solid #d8e2dc;border-radius:4px;padding:7px 9px;background:#fff;color:#253c32;font-size:11px}textarea{width:100%;resize:vertical;line-height:1.5}.content-area{max-width:1480px;margin:0 auto}.overview-meta,.section-head{display:flex;align-items:center;justify-content:space-between;gap:14px}.overview-meta{margin:5px 0 12px;color:#8b9992;font-size:10px}.health-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border:1px solid #dfe8e2;background:#fff}.health-cell{display:grid;gap:8px;min-height:103px;padding:15px;border-right:1px solid #e8eeea}.health-cell:last-child{border:0}.health-cell>span,.summary-grid article>span{color:#65776d;font-size:10px}.health-cell strong{font-size:13px}.health-cell small,.summary-grid small{color:#8a9991;font-size:9px;overflow-wrap:anywhere}.tone-good{color:#28734f}.tone-bad{color:#b34b44}.tone-pending{color:#9a711d}.tone-muted{color:#7c8982}.summary-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;margin-top:18px;border:1px solid #dfe8e2;background:#dfe8e2}.summary-grid article{min-height:105px;background:#fff;padding:15px;display:grid;align-content:start;gap:9px}.summary-grid strong{font-size:21px;font-weight:650}.scope-note{margin-top:13px;color:#7b8c83;font-size:10px}.section-head{margin:2px 0 13px}.section-head h2,.side-form h2{margin:0 0 5px;font-size:14px}.section-head p{margin:0;color:#86958d;font-size:10px}.section-head select{min-width:124px}.table-wrap{overflow:auto;border:1px solid #dfe8e2;background:#fff}table{width:100%;border-collapse:collapse;text-align:left;font-size:10px}th{height:37px;padding:8px 11px;background:#f7f9f7;color:#74847c;font-size:9px;font-weight:600;white-space:nowrap}td{padding:10px 11px;border-top:1px solid #eaf0ec;color:#495c51;vertical-align:top}td strong,td small{display:block}td strong{color:#2c4236;font-size:10px}td small{margin-top:4px;color:#8c9993;font-size:9px}.status{display:inline-flex;font-size:9px;font-weight:650;white-space:nowrap}.empty{height:90px;text-align:center;vertical-align:middle;color:#95a19b}.grant-line{display:flex;align-items:center;gap:7px;margin:3px 0}.text-action{padding:0;border:0;background:transparent;color:#347453;font-size:10px}.danger-text{color:#ae4a44}.reason-cell{max-width:260px;line-height:1.5}.split-layout{display:grid;grid-template-columns:minmax(0,1fr) 270px;align-items:start;gap:16px}.side-form,.import-form{border:1px solid #dfe8e2;background:#fff;padding:16px;display:grid;gap:12px}.side-form>label,.modal>label{display:grid;gap:6px;color:#64756c;font-size:10px}.side-form textarea{min-height:80px}.side-form fieldset{border:1px solid #e1e9e4;display:grid;gap:6px}.side-form legend{color:#718178;font-size:10px}.check-row{display:flex;align-items:center;gap:7px;color:#53675b;font-size:10px}.check-row input{min-height:0}.primary,.secondary,.danger-button{min-height:33px;border:0;border-radius:4px;padding:7px 12px;font-size:10px;font-weight:650}.primary{background:#1d664b;color:white}.primary:disabled{opacity:.5;cursor:not-allowed}.secondary{border:1px solid #d6e1da;background:#fff;color:#52685b}.danger-button{border:1px solid #e7c7c3;background:#fff8f7;color:#a7443e}.shop-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.shop-row{display:flex;align-items:center;gap:10px;min-height:66px;padding:11px;border:1px solid #dfe8e2;background:#fff}.shop-row>div{min-width:0;flex:1}.shop-row strong,.shop-row small{display:block}.shop-row strong{font-size:11px}.shop-row small{margin-top:4px;color:#84938b;font-size:9px}.shop-symbol{color:#3c8060;font-size:17px}.import-form{margin-top:20px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:11px}.form-grid label{display:grid;gap:6px;color:#66776d;font-size:10px}.form-grid .wide{grid-column:1/-1}.form-grid input[type=file]{width:100%;font-size:10px}.form-grid small{color:#8a9991;font-size:9px}.form-footer{display:flex;justify-content:space-between;align-items:center;color:#7f8e86;font-size:10px}.hash{font-family:ui-monospace,monospace!important;overflow-wrap:anywhere;max-width:240px;font-size:9px!important}.review-layout{display:grid;grid-template-columns:minmax(290px,.75fr) minmax(440px,1.25fr);align-items:start;gap:13px}.review-list,.review-detail{border:1px solid #dfe8e2;background:#fff;min-height:260px}.review-list{padding:13px}.version-row{display:grid;gap:7px;padding:11px 8px;border-top:1px solid #edf1ee;cursor:pointer}.version-row.selected{background:#f0f6f1;border-left:2px solid #32805c}.version-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.version-title strong{font-size:11px}.version-row>small{color:#7d8c83;font-size:9px}.version-row .hash{max-width:none}.version-meta{display:flex;gap:12px;color:#8a9891;font-size:9px}.review-detail{padding:17px}.detail-head{display:flex;justify-content:space-between;gap:12px;align-items:center}.detail-head h2{margin:0;font-size:16px}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 15px;margin:16px 0;padding:13px;background:#f7f9f7}.detail-grid div{min-width:0}.detail-grid .wide{grid-column:1/-1}.detail-grid dt{color:#89978f;font-size:9px}.detail-grid dd{margin:3px 0 0;color:#394e43;font-size:10px;overflow-wrap:anywhere}.detail-grid dd.hash{max-width:none}.content-preview{border:1px solid #e2eae5}.preview-head{display:flex;justify-content:space-between;gap:10px;padding:10px;background:#f8faf8;border-bottom:1px solid #e2eae5}.preview-head strong{font-size:10px}.preview-head span{color:#7c8d83;font-size:9px}.content-preview article{padding:11px;border-top:1px solid #edf1ee}.content-preview article small{color:#759281;font-size:9px}.content-preview article p{margin:6px 0 0;color:#4b6154;font-size:10px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}.high-risk-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:13px}.reviewer-line{margin-top:12px;color:#84928a;font-size:9px}.detail-placeholder{min-height:260px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;color:#83938a;text-align:center}.placeholder-mark{font-size:23px;color:#75a487}.detail-placeholder strong{color:#54685c;font-size:12px}.detail-placeholder small{font-size:9px}.alert,.notice{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:0 auto 11px;padding:10px 12px;border:1px solid #efd2ce;background:#fff7f6;color:#a44841;font-size:10px;max-width:1480px}.alert button{border:0;background:transparent;color:inherit;font-weight:700}.notice{border-color:#cde4d5;background:#f0f8f2;color:#34714d}.state-panel{max-width:620px;margin:55px auto;padding:32px;border:1px solid #dfe8e2;background:#fff;display:flex;flex-direction:column;align-items:center;gap:9px;text-align:center}.state-panel strong{font-size:14px}.state-panel span{color:#829189;font-size:10px}.state-panel .primary{margin-top:4px}.unavailable strong{color:#9d4842}.modal-backdrop{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:16px;background:#12271f80}.modal{width:min(560px,100%);max-height:90dvh;overflow:auto;padding:19px;background:#fff;border:1px solid #dfe8e2}.modal>header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.modal header h2{margin:0;font-size:15px}.confirmation-facts{display:grid;grid-template-columns:1fr 1fr;gap:1px;margin:12px 0 14px;border:1px solid #e0e8e3;background:#e0e8e3}.confirmation-facts div{display:grid;gap:5px;min-width:0;padding:9px;background:#f8faf8}.confirmation-facts span{color:#89978f;font-size:9px}.confirmation-facts strong{font-size:9px;overflow-wrap:anywhere}.modal>label{margin:11px 0}.modal footer{display:flex;justify-content:flex-end;gap:8px;margin-top:15px}.review-detail .empty{display:block;height:auto;padding:22px}
@media(max-width:1050px){.admin-shell{grid-template-columns:205px minmax(0,1fr)}.main-area{padding-left:18px;padding-right:18px}.page-head{margin-left:-18px;margin-right:-18px;padding-left:18px;padding-right:18px}.health-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.health-cell:nth-child(3){border-right:0}.health-cell:nth-child(n+4){border-top:1px solid #e8eeea}.review-layout{grid-template-columns:1fr}.review-list{max-height:340px;overflow:auto}}
@media(max-width:700px){.admin-shell{display:block}.rail{position:static;width:100%;height:auto;padding:10px 12px}.brand{padding:0 2px 10px;border-bottom:0}.rail-label,.rail-bottom{display:none}.rail nav{display:flex;overflow:auto;gap:4px;padding:4px 0}.nav-item{width:auto;flex:0 0 auto;min-height:34px;padding:0 9px;border-left:0;border-bottom:2px solid transparent;font-size:10px}.nav-item.active{border-bottom-color:#8fd1a9}.nav-mark{width:14px}.main-area{padding:0 10px 22px}.page-head{position:static;min-height:76px;margin:0 -10px 12px;padding:11px 10px}.page-head h1{font-size:16px}.eyebrow{font-size:8px}.head-actions{gap:6px}.identity{max-width:90px;font-size:9px}.identity small{max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.shop-picker{gap:4px;font-size:9px}.shop-picker select{max-width:105px;font-size:9px;padding:5px}.icon-action{width:29px;height:29px}.health-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.health-cell{min-height:86px;padding:11px;border-bottom:1px solid #e8eeea}.health-cell:nth-child(2n){border-right:0}.health-cell:nth-child(3){border-right:1px solid #e8eeea}.health-cell:nth-child(4){border-right:0}.health-cell:nth-child(n+4){border-top:0}.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:12px}.summary-grid article{min-height:90px;padding:11px}.summary-grid strong{font-size:18px}.split-layout{grid-template-columns:1fr}.side-form{grid-row:1}.shop-grid{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}.form-grid .wide{grid-column:auto}.form-footer{align-items:flex-start;gap:8px}.review-detail{padding:11px}.review-layout{gap:9px}.detail-grid{gap:8px}.table-wrap{max-width:calc(100vw - 20px)}table{min-width:680px}.modal{padding:14px}.confirmation-facts{grid-template-columns:1fr}.overview-meta{font-size:9px}}
.confirm-row{display:flex!important;grid-template-columns:auto 1fr!important;align-items:center;gap:8px;margin:14px 0!important;color:#52685b!important;font-size:11px!important}.confirm-row input{width:16px;min-height:16px;margin:0;accent-color:#1d664b}
</style>
