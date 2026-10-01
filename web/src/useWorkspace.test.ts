import { describe, expect, it, vi } from 'vitest'
import { api, type Conversation, type LastTurn, type TurnEvent } from './api'
import { useWorkspace } from './useWorkspace'

const identity = { tenantId: 'synthetic-a', userId: 'agent', role: 'operator', shopId: 'east', shopName: 'east', displayName: 'agent', profile: 'synthetic_import_mock', is_mock: true }
const result = (status = 'DONE', request = 'r1', id = '1'): LastTurn => ({ conversation_id: id, turn_id: '11', execution_no: 1, request_id: request, status, answer: '商品规格', is_mock: true, evidence: [{ id: 'e1', title: '合成商品', snippet: '480毫升', sourceType: 'synthetic', documentId: 'd1', versionId: 'v1', score: 1 }], citations: [{ evidence_id: 'e1', citation_index: 1 }], customer_reply: { can_copy: false, blocked_reason: 'mock' }, clarification: status === 'ASKING' ? '请说明商品' : '' })
const detail = (turn = result()): Conversation => ({ id: turn.conversation_id, title: '商品', messages: [{ role: 'user', content: '商品规格' }, { role: 'assistant', content: '商品规格', request_id: turn.request_id, mock: true }], last_turn: turn })
function harness() {
  let callbacks!: { event: (event: TurnEvent) => void; done: () => void; error: (value: Error & { status?: number }) => void }
  const cancel = vi.fn()
  const client = { ...api, session: vi.fn(async () => identity), conversations: vi.fn(async () => [detail()]), conversation: vi.fn(async () => detail()), createConversation: vi.fn(async () => detail()), turn: vi.fn(async () => result()), createTurn: vi.fn<typeof api.createTurn>(async () => ({ id: 'r2', request_id: 'r2', turn_id: '12', conversation_id: '1', execution_no: 1, status: 'EXECUTING' })), cancelTurn: vi.fn(async () => ({ request_id: 'r2', status: 'CANCEL_REQUESTED' })), streamEvents: vi.fn((_id, event, done, error) => { callbacks = { event, done, error }; return cancel }) } satisfies typeof api
  return { client, workspace: useWorkspace(client), cancel, callbacks: () => callbacks }
}
async function flush() { for (let i = 0; i < 8; i++) await Promise.resolve() }

describe('server authoritative workspace', () => {
  it('restores evidence, citations, draft and ASKING parent from the service', async () => {
    const h = harness(); h.client.conversation.mockResolvedValue(detail(result('ASKING')))
    await h.workspace.loadWorkspace('1')
    expect(h.workspace.clarification.value).toBe('请说明商品')
    expect(h.workspace.parentTurnId.value).toBe('11')
    expect(h.workspace.citations.value).toHaveLength(1)
    h.client.turn.mockResolvedValue(result('DONE', 'r2'))
    h.client.conversation.mockResolvedValue(detail(result('DONE', 'r2')))
    h.workspace.input.value = '保温杯'
    await h.workspace.send()
    expect(h.client.createTurn.mock.calls[0][2]).toBe('11')
    expect(h.workspace.canCopy.value).toBe(false)
  })
  it('does not substitute a listed conversation for an inaccessible route', async () => {
    const h = harness(); h.client.conversation.mockRejectedValue(Object.assign(new Error('hidden'), { status: 404 }))
    await h.workspace.loadWorkspace('missing')
    expect(h.client.conversation).toHaveBeenCalledWith('missing')
    expect(h.workspace.activeConversation.value).toBeNull()
    expect(h.workspace.error.value).toContain('无法访问')
  })
  it.each([401, 403])('clears restricted data when %s occurs after loading', async status => {
    const h = harness(); await h.workspace.loadWorkspace('1')
    h.client.conversation.mockRejectedValue(Object.assign(new Error('denied'), { status }))
    await h.workspace.selectConversation({ id: '2' })
    expect(h.workspace.session.value).toBeNull()
    expect(h.workspace.conversations.value).toHaveLength(0)
    expect(h.workspace.evidence.value).toHaveLength(0)
    expect(h.workspace.draft.value).toBe('')
    expect(h.workspace.loading.value).toBe(false)
  })
  it('shows dependency failure and retries requests to the service', async () => {
    const h = harness(); h.client.session.mockRejectedValueOnce(Object.assign(new Error('offline'), { status: 503 }))
    await h.workspace.loadWorkspace('1'); expect(h.workspace.unavailable.value).toBe(true)
    await h.workspace.loadWorkspace('1'); expect(h.workspace.unavailable.value).toBe(false)
    expect(h.client.session).toHaveBeenCalledTimes(2)
  })
  it('isolates a late detail response after switching conversations', async () => {
    const h = harness(); let release!: (value: Conversation) => void
    h.client.conversation.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const old = h.workspace.selectConversation({ id: '1' })
    h.client.conversation.mockResolvedValue(detail(result('DONE', 'r2', '2')))
    await h.workspace.selectConversation({ id: '2' }); release(detail()); await old
    expect(h.workspace.activeConversation.value?.id).toBe('2')
    expect(h.workspace.selectedRequestId.value).toBe('r2')
  })
  it('resumes active turns and rejects old stream updates on conversation change', async () => {
    const h = harness(); h.client.conversation.mockResolvedValueOnce(detail(result('EXECUTING')))
    await h.workspace.loadWorkspace('1'); const old = h.callbacks()
    old.event({ type: 'delta', request_id: 'wrong', text: 'foreign' })
    expect(h.workspace.draft.value).toBe('')
    await h.workspace.selectConversation({ id: '2' })
    old.event({ type: 'delta', request_id: 'r1', text: 'late' })
    expect(h.workspace.draft.value).toBe('商品规格')
    expect(h.cancel).toHaveBeenCalled()
  })
  it('keeps CANCEL_REQUESTED until completed and then reads persisted citations', async () => {
    const h = harness(); h.client.conversation.mockResolvedValue(detail(result('EXECUTING', 'r2')))
    await h.workspace.loadWorkspace('1'); await h.workspace.stop()
    expect(h.workspace.turnStatus.value).toBe('CANCEL_REQUESTED')
    expect(h.workspace.busy.value).toBe(true); expect(h.cancel).not.toHaveBeenCalled()
    h.client.turn.mockResolvedValue(result('CANCELLED', 'r2'))
    h.client.conversation.mockResolvedValue(detail(result('CANCELLED', 'r2')))
    h.callbacks().event({ type: 'completed', status: 'CANCELLED', request_id: 'r2' }); h.callbacks().done(); await flush()
    expect(h.workspace.turnStatus.value).toBe('CANCELLED')
    expect(h.workspace.citations.value).toHaveLength(1)
    expect(h.workspace.busy.value).toBe(false)
  })
  it('reuses the same idempotency key after an uncertain submission', async () => {
    const h = harness(); await h.workspace.loadWorkspace('1'); h.workspace.input.value = '规格'
    h.client.createTurn.mockRejectedValueOnce(new Error('transport failed'))
    await h.workspace.send(); await h.workspace.send()
    expect(h.client.createTurn.mock.calls[0][3]).toBe(h.client.createTurn.mock.calls[1][3])
  })
  it('a stream failure cannot invent a service terminal', async () => {
    const h = harness(); h.client.conversation.mockResolvedValue(detail(result('EXECUTING')))
    h.client.turn.mockResolvedValue(result('EXECUTING'))
    await h.workspace.loadWorkspace('1'); h.callbacks().error(new Error('transport failed')); await flush()
    expect(h.workspace.turnStatus.value).toBe('EXECUTING')
  })
})
