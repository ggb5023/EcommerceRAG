export interface Session {
  tenantId: string
  userId: string
  role: string
  shopId: string
  shopName: string
  displayName: string
}

export interface Message { role: 'user' | 'assistant'; content: string; mock?: boolean }
export interface Evidence {
  id: string
  title: string
  snippet: string
  sourceType: string
  documentId: string
  versionId: string
  sourceRef?: string
  score: number
}
export interface Conversation {
  id: string
  title: string
  shop_id?: string
  messages: Message[]
  evidence?: Evidence[]
  lastReply?: string
  lastReplyCopyable?: boolean
}
export interface Turn { id: string; request_id?: string; status?: string; events_url?: string }
export interface TurnEvent {
  type: 'delta' | 'evidence' | 'completed' | 'error' | 'status'
  seq?: number
  text?: string
  evidence?: Evidence[]
  message?: string
  code?: string
  status?: string
  clarification?: string
  is_mock?: boolean
  can_copy?: boolean
}

const base = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    const error = new Error(detail?.message ?? `请求失败（${response.status}）`)
    Object.assign(error, { status: response.status, code: detail?.code })
    throw error
  }
  return response.json() as Promise<T>
}

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms)
    signal.addEventListener('abort', () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }, { once: true })
  })
}

function parseFrame(frame: string): { id?: string; data: string } {
  let id: string | undefined
  const data: string[] = []
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator < 0 ? line : line.slice(0, separator)
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '')
    if (field === 'id') id = value
    if (field === 'data') data.push(value)
  }
  return { id, data: data.join('\n') }
}

export const api = {
  session: () => json<Session>('/v1/session'),
  conversations: async () => {
    const result = await json<{ items: Conversation[] }>('/v1/conversations')
    return result.items
  },
  createConversation: (title = '') => json<Conversation>('/v1/conversations', {
    method: 'POST', body: JSON.stringify({ title }),
  }),
  conversation: (id: string) => json<Conversation>(`/v1/conversations/${encodeURIComponent(id)}`),
  createTurn: (conversationId: string, text: string, parentTurnId?: string) => json<Turn>(
    `/v1/conversations/${encodeURIComponent(conversationId)}/turns`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ text, ...(parentTurnId ? { parent_turn_id: parentTurnId } : {}) }),
    },
  ),
  cancelTurn: (turnId: string) => json(`/v1/turns/${encodeURIComponent(turnId)}/cancel`, {
    method: 'POST', body: '{}',
  }),
  streamEvents(
    turnId: string,
    onEvent: (event: TurnEvent) => void,
    onDone: () => void,
    onError: (reason: string) => void,
  ) {
    const controller = new AbortController()
    const deadline = Date.now() + 60_000
    let lastSeq = 0
    let finished = false

    const consume = async () => {
      let delay = 250
      while (!controller.signal.aborted && Date.now() < deadline && !finished) {
        try {
          const headers: Record<string, string> = { Accept: 'text/event-stream' }
          if (lastSeq > 0) headers['Last-Event-ID'] = String(lastSeq)
          const response = await fetch(`${base}/v1/turns/${encodeURIComponent(turnId)}/events`, {
            headers, signal: controller.signal,
          })
          if (response.status === 401 || response.status === 403 || response.status === 404) {
            throw Object.assign(new Error(`事件流请求失败（${response.status}）`), { terminal: true })
          }
          if (!response.ok || !response.body) throw new Error(`事件流不可用（${response.status}）`)

          const reader = response.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''
          let terminal = false
          const consumeFrames = (flush = false) => {
            const frames = buffer.split(/\r?\n\r?\n/)
            buffer = flush ? '' : (frames.pop() ?? '')
            if (flush && frames.at(-1) === '') frames.pop()
            for (const frame of frames) {
              const parsed = parseFrame(frame)
              if (!parsed.data) continue
              const event = JSON.parse(parsed.data) as TurnEvent
              const sequence = event.seq ?? Number(parsed.id ?? 0)
              if (!Number.isFinite(sequence) || sequence <= lastSeq) continue
              if (sequence !== lastSeq + 1) throw new Error('事件序号缺失，正在尝试恢复')
              event.seq = sequence
              lastSeq = sequence
              onEvent(event)
              if (event.type === 'completed') terminal = true
            }
          }
          while (true) {
            const { done, value } = await reader.read()
            buffer += decoder.decode(value, { stream: !done })
            consumeFrames(done)
            if (done) break
          }
          if (terminal) {
            finished = true
            onDone()
            return
          }
          await wait(delay, controller.signal).catch(() => undefined)
          delay = Math.min(delay * 2, 4_000)
        } catch (error) {
          if (controller.signal.aborted) return
          if ((error as { terminal?: boolean }).terminal) {
            finished = true
            onError(error instanceof Error ? error.message : '事件流失败')
            return
          }
          if (Date.now() + delay >= deadline) break
          await wait(delay, controller.signal).catch(() => undefined)
          delay = Math.min(delay * 2, 4_000)
        }
      }
      if (!controller.signal.aborted && !finished) {
        finished = true
        onError('请求超过 60 秒期限或事件流中断')
      }
    }

    void consume()
    return () => controller.abort()
  },
}
