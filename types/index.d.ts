// State contract of the Autorouter band (hooks/band.tsx).

/** A subagent of one of our tiers, running now. */
export type Run = {
  /** The agent's loop id, as agent.spawn answers it and turn.complete carries it. */
  id: string
  /** quick | builder | architect, or the agent type when it is not ours. */
  tier: string
  /** The model the engine resolved for it, e.g. claude-haiku-5-5. */
  model: string
  /** When it started, ms since epoch. */
  since: number
}

/** What the router's Python hooks write to ~/.claude/autorouter/band.json. */
export type Snapshot = {
  total: number
  shares: { quick: number; builder: number; architect: number }
  cap: number
  escalations: number
  last: { tier: string; final: string } | null
}

declare module 'claude-code' {
  interface PluginState {
    autorouter: { running: Shaped<Run[]>; snapshot: Shaped<Snapshot | null> }
  }
}
