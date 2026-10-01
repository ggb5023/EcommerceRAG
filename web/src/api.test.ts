import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type TurnEvent } from './api'

function sseResponse(frames: string[], splitBytes = false): Response {
  const bytes = new TextEncoder().encode(frames.join(''))
  const chunks = splitBytes ? Array.from(bytes, (byte) => new Uint8Array([byte])) : [bytes]
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function frame(id: number, event: TurnEvent): string {
  return `id: ${id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`
}

function streamUntilTerminal() {
  const events: TurnEvent[] = []
  let resolve!: () => void
  let reject!: (error: Error) => void
  const done = new Promise<void>((yes, no) => { resolve = yes; reject = no })
  const cancel = api.streamEvents('turn-test', (event) => events.push(event), resolve,
    (reason) => reject(new Error(reason)))
  return { events, done, cancel }
}

afterEach(() => vi.unstubAllGlobals())

describe('SSE transport', () => {
  it('sends clarification replies as child turns', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(JSON.parse(String(init?.body))).toEqual({ text: '配送政策', parent_turn_id: 'm1-parent' })
      expect(new Headers(init?.headers).get('Idempotency-Key')).toBeTruthy()
      return new Response(JSON.stringify({ id: 'm1-child', status: 'EXECUTING' }), { status: 202 })
    })
    vi.stubGlobal('fetch', fetchMock)
    await api.createTurn('conversation', '配送政策', 'm1-parent')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('handles split UTF-8, multiline data, and duplicate sequence numbers', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse([
      'id: 1\nevent: delta\ndata: {\ndata:   "type":"delta",\ndata:   "seq":1,\ndata:   "text":"配送政策"\ndata:}\n\n',
      frame(1, { type: 'delta', seq: 1, text: 'duplicate' }),
      frame(2, { type: 'completed', seq: 2, status: 'DONE', is_mock: true, can_copy: false }),
    ], true)))
    const run = streamUntilTerminal()
    await run.done
    run.cancel()
    expect(run.events).toHaveLength(2)
    expect(run.events[0].text).toBe('配送政策')
    expect(run.events[1].type).toBe('completed')
  })

  it('reconnects with the last event id without resetting the turn', async () => {
    const headersSeen: Headers[] = []
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      headersSeen.push(new Headers(init?.headers))
      if (headersSeen.length === 1) return sseResponse([frame(1, { type: 'delta', seq: 1, text: 'A' })])
      return sseResponse([frame(2, { type: 'completed', seq: 2, status: 'DONE' })])
    })
    vi.stubGlobal('fetch', fetchMock)
    const run = streamUntilTerminal()
    await run.done
    run.cancel()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(headersSeen[1].get('Last-Event-ID')).toBe('1')
    expect(run.events.map((event) => event.seq)).toEqual([1, 2])
  })

  it('replays from the beginning when the initial event sequence has a gap', async () => {
    const fetchMock = vi.fn(async () => {
      if (fetchMock.mock.calls.length === 1) {
        return sseResponse([frame(2, { type: 'completed', seq: 2, status: 'DONE' })])
      }
      return sseResponse([
        frame(1, { type: 'delta', seq: 1, text: 'A' }),
        frame(2, { type: 'completed', seq: 2, status: 'DONE' }),
      ])
    })
    vi.stubGlobal('fetch', fetchMock)
    const run = streamUntilTerminal()
    await run.done
    run.cancel()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(run.events.map((event) => event.seq)).toEqual([1, 2])
  })

  it('surfaces authorization failures without retrying or inventing a reply', async () => {
    const fetchMock = vi.fn(async () => new Response('{"code":"forbidden"}', { status: 403 }))
    vi.stubGlobal('fetch', fetchMock)
    let resolve!: (reason: string) => void
    const failed = new Promise<string>((done) => { resolve = done })
    const cancel = api.streamEvents('turn-test', () => { throw new Error('unexpected event') },
      () => { throw new Error('unexpected completion') }, resolve)
    expect(await failed).toContain('403')
    cancel()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('workspace contracts', () => {
  it('exposes the conversation route without changing the API boundary', async () => {
    const { routes } = await import('./routes')
    expect(routes.map((route) => route.path)).toEqual(['/', '/chat', '/chat/:conversation_id', '/:pathMatch(.*)*'])
  })
})
