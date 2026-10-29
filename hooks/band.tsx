// The Autorouter band: a row above the prompt with the subagents running now
// (and the model each really runs on), the 24h tier mix, and the last route.
// It shares the slot with other mods (Ponytail's band) instead of replacing it.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Run, Snapshot } from '../types'

const TIERS = ['quick', 'builder', 'architect'] as const
const STALE_MS = 30 * 60 * 1000

const running = atom({ plugin: 'autorouter', key: 'running' } as const, [], { shape: 'running-1' })
const snapshot = atom({ plugin: 'autorouter', key: 'snapshot' } as const, null, { shape: 'snapshot-1' })

// "autorouter:quick" -> "quick"; agents that are not ours keep their type.
export const tierOf = (agentType: string) => agentType.split(':').pop() ?? agentType

// "claude-haiku-5-5-20261001" -> "haiku-5-5": enough to tell generations apart.
export const shortModel = (model: string) =>
  model.replace(/^claude-/, '').replace(/-\d{8}$/, '').replace(/\[.*\]$/, '')

const pct = (n: number, total: number) => (total > 0 ? Math.round((100 * n) / total) : 0)

async function routerHome($: EngineInterface): Promise<string> {
  const custom = await $.env.get('AUTOROUTER_HOME')
  if (custom) return custom
  const home = (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? ''
  const claude = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${home}/.claude`
  return `${claude}/autorouter`
}

// The Python hooks own routing and history; they leave a small JSON for us.
async function refresh($: EngineInterface) {
  try {
    const text = await $.fs.read(`${await routerHome($)}/band.json`)
    const snap = JSON.parse(text) as Snapshot
    await update($, snapshot, () => snap)
  } catch {
    // No file yet (no routed prompt) or a partial write: keep what we had.
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'band', description: 'Show or hide the Autorouter band' })
    await refresh($)
    return next(e)
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'band' }, async $ => {
    const hidden = (await $.store.get('hidden')) === true
    await $.store.set('hidden', !hidden)
    return { text: hidden ? 'Autorouter band shown.' : 'Autorouter band hidden. /autorouter:band brings it back.' }
  })

  // The router's prompt hook runs beneath us: read its decision once it has run.
  on('prompt.submit', async ($, e, next) => {
    const result = await next(e)
    await refresh($)
    return result
  }).catch(($, e, next) => next(e))

  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)
    if (result.agentId !== undefined && !e.fork) {
      const run: Run = { id: result.agentId, tier: tierOf(e.subagentType), model: result.model, since: await $.clock.now() }
      await update($, running, list => [...list.filter(r => r.id !== run.id), run])
    }
    return result
  }).catch(($, e, next) => next(e))

  // A subagent's run is one turn of its loop: its end is ours to clear.
  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) {
      const id = e.agentId
      await update($, running, list => list.filter(r => r.id !== id))
    } else {
      await refresh($)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await $.store.get('hidden')) === true) return next(e)

    const now = await $.clock.now()
    const runs = (await read($, running)).filter(r => now - r.since < STALE_MS)
    const snap = await read($, snapshot)
    if (runs.length === 0 && snap === null) return next(e)

    // Other mods share this slot: draw theirs below ours. next(e) rejects only
    // when nothing answers beneath (a test harness).
    const below = await next(e).catch(() => undefined)

    const { Box, Text, Button } = $.ui.resolve(e)
    const color = (tier: string) =>
      tier === 'quick' ? 'success' : tier === 'architect' ? 'warning' : tier === 'builder' ? 'suggestion' : undefined
    const width = e.surface === 'terminal' ? e.props.bodyColumns : undefined

    const archShare = snap ? pct(snap.shares.architect, snap.total) : 0
    const overCap = snap !== null && snap.total > 0 && archShare > Math.round(snap.cap * 100)

    const mine = (
      <Box flexDirection="row" alignItems="center" gap={2} width={width}>
        <Text dimColor>⇄ router</Text>
        {runs.length === 0 ? (
          <Text dimColor>idle</Text>
        ) : (
          runs.map(r => (
            <Text color={color(r.tier)}>
              ▶ {r.tier} <Text dimColor>{shortModel(r.model)}</Text>
            </Text>
          ))
        )}
        {snap !== null && snap.total > 0 && (
          <Text>
            <Text dimColor>24h </Text>
            {TIERS.map(t => (
              <Text color={t === 'architect' && overCap ? 'error' : color(t)}>
                {t} {pct(snap.shares[t], snap.total)}%{t === 'architect' ? `/${Math.round(snap.cap * 100)}%` : ''}{' '}
              </Text>
            ))}
          </Text>
        )}
        {snap?.last && (
          <Text dimColor>
            last {snap.last.tier}
            {snap.last.final !== snap.last.tier ? `→${snap.last.final}` : ''}
          </Text>
        )}
        {snap !== null && snap.escalations > 0 && <Text dimColor>↑{snap.escalations}</Text>}
        <Box flexGrow={1} />
        <Button key="stats" plain label="Stats" onPress={() => $.command.run({ command: 'autorouter:router-stats', args: '' })} />
        <Button key="learn" plain label="Learn" onPress={() => $.command.run({ command: 'autorouter:router-learn', args: '' })} />
        <Button key="hide" plain label="Hide" onPress={() => $.store.set('hidden', true)} />
      </Box>
    )
    return below ? <Box flexDirection="column">{mine}{below}</Box> : mine
  }).catch(($, e, next) => next(e))
}
