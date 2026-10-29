import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const BAND = (surface: 'terminal' | 'desktop') =>
  ({
    plugin: 'autorouter',
    component: 'AbovePrompt',
    surface,
    props: { hasSurvey: false, isWorking: true, maxRows: 10, bodyColumns: 120, scroll: { offset: 0, bodyRows: 10 }, view: {} },
  }) as const

const spawn = (subagentType: string) => ({
  tool_use_id: `tu-${subagentType}`,
  prompt: 'task',
  description: 'task',
  subagentType,
  provider: { plugin: 'autorouter', tier: 'user' },
  parentModel: 'claude-sonnet-5-5',
  background: false,
  fork: false,
}) as const

const SNAPSHOT = JSON.stringify({
  total: 8,
  shares: { quick: 4, builder: 2, architect: 2 },
  cap: 0.3,
  escalations: 1,
  last: { tier: 'quick', final: 'builder' },
})

// Stand-ins for the engine beneath the plugin.
function engine(on: On, snapshot: string | null) {
  mock.clock(on, { now: 1_000_000 })
  mock.store(on)
  mock.env(on, { HOME: '/home/u' })
  on('command.register', () => ({ value: { command: 'band' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.read', () => (snapshot === null ? { deny: 'ENOENT: no such file' } : { value: snapshot }))
  on('agent.spawn', (_, e) => ({ agentId: `a-${e.subagentType}`, model: 'claude-opus-5-5-20261001' }))
  on('turn.complete', () => ({ text: '' }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
}

test('the band shows running agents with their model, the tier mix and the last route', async ($, on) => {
  engine(on, SNAPSHOT)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.agent.spawn(spawn('autorouter:architect'))

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(BAND(surface))
    expect((await ui.find({ type: 'Text', text: /▶ architect/ }))?.text).toContain('opus-5-5')
    expect(await ui.find({ type: 'Text', text: /architect 25%\/30%/ })).toBeTruthy()
    expect((await ui.find({ type: 'Text', text: /^last / }))?.text).toBe('last quick→builder')
    // Another mod's band (here the engine's) still draws below ours.
    expect(await ui.find({ type: 'Text', text: 'engine' })).toBeTruthy()
    await ui.unmount()
  }
})

test('a finished subagent leaves the band', async ($, on) => {
  engine(on, SNAPSHOT)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.agent.spawn(spawn('autorouter:quick'))
  await $.turn.complete({ answer: 'done', durationMs: 10, isAborted: false, turnId: 't1', agentId: 'a-autorouter:quick', reason: 'answer' })

  const ui = await $.ui.mount(BAND('terminal'))
  expect(await ui.find({ type: 'Text', text: 'idle' })).toBeTruthy()
  await ui.unmount()
})

test('with nothing routed yet, the slot is left to others', async ($, on) => {
  engine(on, null)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND('terminal'))
  expect(await ui.find({ type: 'Text', text: /router/ })).toBeFalsy()
  expect(await ui.find({ type: 'Text', text: 'engine' })).toBeTruthy()
  await ui.unmount()
})

test('Hide hides the band', async ($, on) => {
  engine(on, SNAPSHOT)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount(BAND('terminal'))
  await ui.press({ key: 'hide' })
  await ui.unmount()
  const again = await $.ui.mount(BAND('terminal'))
  expect(await again.find({ type: 'Text', text: /router/ })).toBeFalsy()
  await again.unmount()
})
