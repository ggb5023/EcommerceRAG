import { computed, ref } from 'vue'
import { api, type Conversation, type Evidence, type Session, type TurnEvent } from './api'
import { isTurnActive } from './turnState'

export type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'disconnected'

export function useWorkspace() {
  const session = ref<Session | null>(null)
  const conversations = ref<Conversation[]>([])
  const activeConversation = ref<Conversation | null>(null)
  const evidence = ref<Evidence[]>([])
  const citations = ref<Array<{ evidence_id: string; citation_index: number }>>([])
  const customerReply = ref<Conversation['last_turn'] extends infer T ? T extends { customer_reply: infer R } ? R : undefined : undefined>()
  const draft = ref('')
  const input = ref('')
  const turnStatus = ref('')
  const clarification = ref('')
  const parentTurnId = ref('')
  const connectionState = ref<ConnectionState>('idle')
  const loading = ref(true)
  const unavailable = ref(false)
  const error = ref('')
  const busy = computed(() => isTurnActive(turnStatus.value))
  const canCopy = computed(() => Boolean(customerReply.value?.can_copy && !activeConversation.value?.last_turn?.is_mock))
  let cancelStream: (() => void) | undefined
  let activeTurnId = ''

  function clearTurnState() {
    cancelStream?.()
    cancelStream = undefined
    activeTurnId = ''
    turnStatus.value = ''
    connectionState.value = 'idle'
    evidence.value = []
    citations.value = []
    customerReply.value = undefined
    draft.value = ''
    clarification.value = ''
    parentTurnId.value = ''
  }

  function describeError(value: unknown, fallback: string) {
    const status = (value as { status?: number }).status
    if (status === 401 || status === 403) return '当前账号没有访问权限，请重新登录或联系管理员'
    if (status && status >= 500) return '工作台暂不可用，请检查服务状态后重试'
    return value instanceof Error ? value.message : fallback
  }

  function restore(detail: Conversation) {
    detail.messages ??= []
    activeConversation.value = detail
    const turn = detail.last_turn
    evidence.value = turn?.evidence ?? detail.evidence ?? []
    citations.value = turn?.citations ?? detail.citations ?? []
    draft.value = turn?.answer ?? detail.lastReply ?? ''
    customerReply.value = turn?.customer_reply
    turnStatus.value = turn?.status ?? ''
  }

  async function selectConversation(item: Conversation) {
    clearTurnState()
    try {
      restore(await api.conversation(item.id))
      unavailable.value = false
      error.value = ''
    } catch (value) {
      activeConversation.value = null
      unavailable.value = true
      error.value = describeError(value, '读取会话失败')
    }
  }

  async function newConversation() {
    clearTurnState()
    try {
      const item = await api.createConversation()
      item.messages ??= []
      conversations.value.unshift(item)
      restore(item)
      unavailable.value = false
      error.value = ''
    } catch (value) {
      error.value = describeError(value, '创建会话失败')
    }
  }

  function handleEvent(current: Conversation, event: TurnEvent) {
    connectionState.value = 'connected'
    if (event.type === 'status') turnStatus.value = event.status ?? turnStatus.value
    if (event.type === 'status' && event.status === 'ASKING') {
      clarification.value = event.clarification ?? '请补充更多问题细节。'
      parentTurnId.value = activeTurnId
    }
    if (event.type === 'evidence') evidence.value = event.evidence ?? []
    if (event.type === 'delta') draft.value += event.text ?? ''
    if (event.type === 'completed') {
      turnStatus.value = event.status ?? turnStatus.value
      if (event.status === 'ASKING') {
        clarification.value = event.clarification ?? clarification.value
        error.value = '需要补充信息后才能继续'
      } else if (event.status === 'DONE') {
        current.lastReply = draft.value
        current.lastReplyCopyable = false
        current.evidence = evidence.value
        current.messages.push({ role: 'assistant', content: draft.value, mock: true })
      } else if (event.status === 'CANCELLED') {
        error.value = '生成已停止'
      } else if (event.status === 'FAILED') {
        error.value ||= '生成失败，请重试'
      }
      if (event.status !== 'ASKING') connectionState.value = 'connected'
      activeTurnId = ''
    }
    if (event.type === 'error') error.value = event.message ?? '生成失败'
  }

  async function send() {
    const text = input.value.trim()
    const current = activeConversation.value
    if (!text || busy.value || !current) return
    input.value = ''
    error.value = ''
    current.messages.push({ role: 'user', content: text })
    turnStatus.value = 'PENDING'
    connectionState.value = 'connecting'
    draft.value = ''
    clarification.value = ''
    evidence.value = []
    citations.value = []
    customerReply.value = undefined
    try {
      const turn = await api.createTurn(current.id, text, parentTurnId.value || undefined)
      parentTurnId.value = ''
      activeTurnId = turn.id
      turnStatus.value = turn.status ?? 'EXECUTING'
      cancelStream?.()
      cancelStream = api.streamEvents(turn.id, (event) => handleEvent(current, event), () => {
        connectionState.value = 'connected'
        if (!isTurnActive(turnStatus.value)) activeTurnId = ''
      }, (reason) => {
        connectionState.value = 'disconnected'
        error.value = reason
        turnStatus.value = 'FAILED'
        activeTurnId = ''
      })
    } catch (value) {
      error.value = describeError(value, '提交失败')
      turnStatus.value = 'FAILED'
      connectionState.value = 'disconnected'
    }
  }

  async function stop() {
    if (activeTurnId) {
      turnStatus.value = 'CANCEL_REQUESTED'
      try { await api.cancelTurn(activeTurnId) } catch (value) { error.value = describeError(value, '取消失败') }
    }
    clearTurnState()
  }

  async function copyDraft() {
    if (!canCopy.value || !draft.value || !customerReply.value?.text_plain) return false
    await navigator.clipboard.writeText(customerReply.value.text_plain)
    return true
  }

  async function loadWorkspace(preferredId?: string) {
    loading.value = true
    unavailable.value = false
    error.value = ''
    clearTurnState()
    try {
      session.value = await api.session()
      conversations.value = await api.conversations()
      const preferred = preferredId && conversations.value.find((item) => item.id === preferredId)
      if (preferred) await selectConversation(preferred)
      else if (conversations.value.length) await selectConversation(conversations.value[0])
      else await newConversation()
    } catch (value) {
      session.value = null
      conversations.value = []
      activeConversation.value = null
      unavailable.value = true
      error.value = describeError(value, '工作台加载失败')
    } finally { loading.value = false }
  }

  return { session, conversations, activeConversation, evidence, citations, customerReply, draft, input, turnStatus, clarification, connectionState, loading, unavailable, error, busy, canCopy, loadWorkspace, newConversation, selectConversation, send, stop, copyDraft, clearTurnState }
}
