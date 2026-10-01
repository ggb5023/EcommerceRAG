export type TurnStatus = 'PENDING' | 'EXECUTING' | 'ASKING' | 'WAITING_REVIEW' | 'DONE' | 'FAILED' | 'CANCEL_REQUESTED' | 'CANCELLED'

const active = new Set<TurnStatus>(['PENDING', 'EXECUTING', 'CANCEL_REQUESTED'])

export function isTurnActive(status: string): boolean {
  return active.has(status as TurnStatus)
}

export function isTurnTerminal(status: string): boolean {
  return ['ASKING', 'WAITING_REVIEW', 'DONE', 'FAILED', 'CANCELLED'].includes(status)
}
