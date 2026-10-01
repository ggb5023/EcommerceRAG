import { computed, ref } from 'vue'
import { api, type ApiError, type Conversation, type Evidence, type LastTurn, type Session, type TurnEvent } from './api'
import { isTurnActive } from './turnState'

export type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected'

export function useWorkspace(client = api) {
  const session = ref<Session | null>(null)
  const conversations = ref<Conversation[]>([])
  const activeConversation = ref<Conversation | null>(null)
  const selectedTurn = ref<LastTurn | null>(null)
  const selectedRequestId = computed(() => selectedTurn.value?.request_id ?? '')
  const evidence = ref<Evidence[]>([])
  const citations = ref<LastTurn['citations']>([])
  const customerReply = ref<LastTurn['customer_reply']>()
  const draft = ref('')
  const input = ref('')
  const turnStatus = ref('')
  const clarification = ref('')
  const parentTurnId = ref('')
  const connectionState = ref<ConnectionState>('idle')
  const loading = ref(true)
  const submitting = ref(false)
  const unavailable = ref(false)
  const error = ref('')
  const busy = computed(() => submitting.value || isTurnActive(turnStatus.value))
  // This workspace only supports mock profiles. Copy approval needs a separate
  // server contract; no historical reply flag may enable it here.
  const canCopy = computed(() => false)
  let epoch = 0
  let selection = 0
  let cancelStream: (() => void) | undefined
  let activeRequest = ''
  let retry: { text: string; parent: string; key: string } | undefined

  function clearTurnState() {
    cancelStream?.(); cancelStream = undefined; activeRequest = ''
    selectedTurn.value = null; evidence.value = []; citations.value = []
    customerReply.value = undefined; draft.value = ''; clarification.value = ''
    parentTurnId.value = ''; turnStatus.value = ''; connectionState.value = 'idle'
    submitting.value = false; retry = undefined; selection++
  }
  function begin() { epoch++; clearTurnState(); activeConversation.value = null; input.value = ''; return epoch }
  function current(token: number, id?: string) { return token === epoch && (!id || id === activeConversation.value?.id) }
  function failure(value: unknown, fallback: string) {
    const status = (value as ApiError)?.status
    if (status === 401 || status === 403) {
      begin(); session.value = null; conversations.value = []; unavailable.value = true
      loading.value = false; error.value = '当前账号没有访问权限，请重新连接或联系管理员'; return
    }
    unavailable.value = Boolean(status && status >= 500) || !activeConversation.value
    error.value = status === 404 ? '会话不存在或无法访问' : status && status >= 500
      ? '工作台暂不可用，请检查服务状态后重试' : value instanceof Error ? value.message : fallback
  }
  function applyTurn(turn?: LastTurn) {
    selectedTurn.value = turn ?? null
    evidence.value = turn?.evidence ?? []; citations.value = turn?.citations ?? []
    customerReply.value = turn?.customer_reply; draft.value = turn?.answer ?? ''
    turnStatus.value = turn?.status ?? ''; clarification.value = turn?.status === 'ASKING' ? turn.clarification ?? '请补充具体商品或问题。' : ''
    parentTurnId.value = turn?.status === 'ASKING' ? turn.turn_id : ''
  }
  async function refreshResult(token: number, id: string, request: string) {
    try {
      const turn = await client.turn(request)
      if (!current(token, id) || activeRequest !== request) return
      const detail = await client.conversation(id)
      if (!current(token, id) || activeRequest !== request) return
      detail.messages ??= []; activeConversation.value = detail
      applyTurn(turn); connectionState.value = 'connected'
      if (!isTurnActive(turn.status)) { activeRequest = ''; cancelStream?.(); cancelStream = undefined }
    } catch (value) { if (current(token, id)) failure(value, '读取最终结果失败') }
  }
  function connect(turn: LastTurn, token: number, id: string) {
    cancelStream?.(); activeRequest = turn.request_id
    // Replay starts at zero after a refresh; never concatenate a cached partial
    // answer with persisted deltas. SSE transport owns seq/dedup/gap recovery.
    draft.value = ''; connectionState.value = 'connecting'
    const request = turn.request_id
    const valid = () => current(token, id) && activeRequest === request
    cancelStream = client.streamEvents(request, (event: TurnEvent) => {
      if (!valid() || (event.request_id && event.request_id !== request)) return
      if (event.type === 'status' && event.status) turnStatus.value = event.status
      if (event.type === 'evidence') evidence.value = event.evidence ?? []
      if (event.type === 'delta') draft.value += event.text ?? ''
      if (event.type === 'error') error.value = event.message ?? '生成失败'
      if (event.type === 'completed') {
        if (event.status) turnStatus.value = event.status
      }
    }, () => { if (valid()) void refreshResult(token, id, request) }, (reason) => {
      if (!valid()) return
      connectionState.value = 'disconnected'
      if (reason.status === 401 || reason.status === 403) failure(reason, '事件流无权限')
      else { error.value = reason.message; void refreshResult(token, id, request) }
    }, (state) => { if (valid()) connectionState.value = state })
  }
  function restore(detail: Conversation, token: number) {
    detail.messages ??= []; activeConversation.value = detail; applyTurn(detail.last_turn)
    if (detail.last_turn && isTurnActive(detail.last_turn.status)) connect(detail.last_turn, token, detail.id)
  }
  async function selectConversation(item: Pick<Conversation, 'id'>) {
    const token = begin(); loading.value = true; error.value = ''; unavailable.value = false
    try { const detail = await client.conversation(item.id); if (current(token)) restore(detail, token) }
    catch (value) { if (current(token)) failure(value, '读取会话失败') }
    finally { if (current(token)) loading.value = false }
  }
  async function selectRequest(request: string) {
    if (busy.value || !activeConversation.value) return
    const token = epoch; const id = activeConversation.value.id; const pick = ++selection
    try {
      const turn = await client.turn(request)
      if (current(token, id) && pick === selection && turn.conversation_id === id) applyTurn(turn)
    } catch (value) { if (current(token, id) && pick === selection) failure(value, '读取历史证据失败') }
  }
  async function newConversation() {
    const token = begin(); loading.value = true; unavailable.value = false; error.value = ''
    try { const item = await client.createConversation(); if (current(token)) { conversations.value.unshift(item); restore(item, token) } }
    catch (value) { if (current(token)) failure(value, '创建会话失败') }
    finally { if (current(token)) loading.value = false }
  }
  async function send() {
    const text = input.value.trim(); const conversation = activeConversation.value
    if (!text || busy.value || !conversation) return
    const token = epoch; const id = conversation.id; const parent = parentTurnId.value
    if (!retry || retry.text !== text || retry.parent !== parent) retry = { text, parent, key: crypto.randomUUID() }
    submitting.value = true; error.value = ''
    try {
      const created = await client.createTurn(id, text, parent || undefined, retry.key)
      if (!current(token, id)) return
      const turn = await client.turn(created.request_id)
      if (!current(token, id)) return
      const detail = await client.conversation(id)
      if (!current(token, id)) return
      input.value = ''; retry = undefined; restore(detail, token)
      if (!isTurnActive(turn.status)) applyTurn(turn)
    } catch (value) { if (current(token, id)) failure(value, '提交失败') }
    finally { if (current(token, id)) submitting.value = false }
  }
  async function stop() {
    const request = activeRequest; const id = activeConversation.value?.id; const token = epoch
    if (!request || !id) return
    try {
      const result = await client.cancelTurn(request)
      if (current(token, id) && activeRequest === request) {
        turnStatus.value = result.status
        if (!isTurnActive(result.status)) await refreshResult(token, id, request)
      }
    } catch (value) { if (current(token, id)) failure(value, '取消失败') }
  }
  async function loadWorkspace(preferredId?: string) {
    const token = begin(); loading.value = true; unavailable.value = false; error.value = ''
    try {
      const identity = await client.session(); if (!current(token)) return; session.value = identity
      const list = await client.conversations(); if (!current(token)) return; conversations.value = list
      // Route IDs locate resources; missing list entries never authorize or
      // redirect to a different conversation silently.
      const detail = preferredId ? await client.conversation(preferredId) : list.length ? await client.conversation(list[0].id) : await client.createConversation()
      if (!current(token)) return
      if (!list.length && !preferredId) conversations.value.push(detail)
      restore(detail, token)
    } catch (value) { if (current(token)) failure(value, '工作台加载失败') }
    finally { if (current(token)) loading.value = false }
  }
  return { session, conversations, activeConversation, selectedTurn, selectedRequestId, evidence, citations, customerReply, draft, input, turnStatus, clarification, parentTurnId, connectionState, loading, unavailable, error, busy, canCopy, loadWorkspace, newConversation, selectConversation, selectRequest, send, stop, clearTurnState }
}
