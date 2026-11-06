/**
 * Renders the REAL desktop plugin (loaded uncompiled, exactly as the app loads
 * it) against a real React tree, a real QueryClient and jsdom — no test
 * framework, no build step, no network.
 *
 *   npm install && npm test
 *
 * `--experimental-vm-modules` lets us link the plugin's ESM imports to a
 * synthetic `@hermes/plugin-sdk`, which is the only way to exercise the file
 * the desktop app actually loads.
 */
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import assert from 'node:assert/strict'

const require = createRequire(new URL('../package.json', import.meta.url))
const React = require('react')
const jsx = require('react/jsx-runtime')
const { JSDOM } = require('jsdom')

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' })
globalThis.window = dom.window
globalThis.document = dom.window.document
// Node 22 exposes `navigator` as a getter-only global.
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const { createRoot } = require('react-dom/client')
const { QueryClient, QueryClientProvider, useQuery } = require('@tanstack/react-query')
const { act, useSyncExternalStore } = React

function atom(initial) {
  let value = initial
  const listeners = new Set()
  return {
    get: () => value,
    set: next => { value = next; listeners.forEach(fn => fn()) },
    subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn) },
  }
}

const state = {
  focusedSessionId: atom('a'),
  focusedSessionProfile: atom('default'),
  model: atom('codex'),
  gateway: atom('open'),
}
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
const events = new Map()
const sdk = {
  host: {
    state,
    onEvent: (event, callback) => { events.set(event, callback); return () => events.delete(event) },
  },
  useValue: store => useSyncExternalStore(store.subscribe, store.get, store.get),
  useQuery,
  queryClient,
  COMPOSER_AREAS: { actions: 'composer.actions' },
}
const wrap = exports => new vm.SyntheticModule(Object.keys(exports), function () {
  for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
})
const mod = new vm.SourceTextModule(readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8'))
await mod.link(name => wrap(name === '@hermes/plugin-sdk' ? sdk : name === 'react' ? React : jsx))
await mod.evaluate()

const { describeQuota, resetText, default: plugin } = mod.namespace
const NOW = Date.parse('2026-01-01T00:00:00Z')
const codex = {
  provider: 'openai-codex',
  available: true,
  plan: 'Plus',
  windows: [
    { label: 'Session', used_percent: 17, reset_at: '2026-01-01T04:08:00Z' },
    { label: 'Weekly', used_percent: 10, reset_at: null },
  ],
  details: [],
}

// ── text contract ────────────────────────────────────────────────────
assert.equal(describeQuota(codex, NOW).text, 'Codex · 83% left · resets in 4h 8m')
assert.match(describeQuota(codex, NOW).title, /Weekly: 90% left/)
assert.match(describeQuota(codex, NOW).title, /Plus/)
assert.equal(describeQuota({ ...codex, windows: [{ label: 'Weekly', used_percent: 95 }] }).low, true)
assert.equal(describeQuota({ ...codex, windows: [{ label: 'Weekly', used_percent: 5 }] }).low, false)
assert.equal(describeQuota({ ...codex, windows: [{ label: 'Weekly', used_percent: 100 }] }).text, 'Codex · 0% left')
// the most depleted window leads, so a healthy session window can't hide an empty week
assert.equal(describeQuota({
  ...codex,
  windows: [{ label: 'Session', used_percent: 2 }, { label: 'Weekly', used_percent: 99, reset_at: '2026-01-01T04:08:00Z' }],
}, NOW).text, 'Codex · 1% left · resets in 4h 8m')
assert.equal(describeQuota({ provider: 'openrouter', available: true, details: ['Credits balance: $12.34'] }).text,
  'OpenRouter · $12.34 left')
assert.equal(describeQuota({ provider: 'nous', available: true, details: ['Top-up credits: $3.00'] }).text,
  'Nous · $3.00 left')
assert.match(describeQuota({ provider: 'gemini', available: false }).text, /^Gemini · quota unavailable$/)
assert.match(describeQuota({ provider: 'opencode-go', available: false, reason: 'nope' }).title, /nope/)
assert.equal(resetText('bad', NOW), '')
assert.equal(resetText('2025-01-01', NOW), 'reset due')
assert.equal(resetText('2026-01-03T03:00:00Z', NOW), 'resets in 2d 3h')
assert.match(describeQuota({ ...codex, provider: 'opencode-go' }, NOW).text, /^OpenCode Go · 83% left/)
console.log('PASS text contract: remaining %, limiting window, balance, unavailable, reset formats')

// ── real mount: query, session switch, failure, disconnect ───────────
let contribution
let fail = false
const calls = []
const ctx = {
  register: c => { contribution = c },
  rest: async path => {
    calls.push(path)
    if (fail) throw new Error('offline test service failure')
    return path.includes('session_id=b')
      ? { provider: 'openrouter', available: true, details: ['Credits balance: $12.34'] }
      : codex
  },
}
plugin.register(ctx)
assert.equal(contribution.area, 'composer.actions')

const root = createRoot(document.getElementById('root'))
const settle = async () => { await act(async () => { await new Promise(r => setTimeout(r, 60)) }) }
const chip = () => document.querySelector('[data-provider-quota]')

await act(async () => { root.render(React.createElement(QueryClientProvider, { client: queryClient }, contribution.render())) })
await settle()
assert.match(document.body.textContent, /Codex · 83% left/)
assert.match(chip().title, /Weekly: 90% left/)
assert.match(chip().getAttribute('aria-label'), /^Codex · 83% left/)

// a different focused session (different provider) re-reads quota
await act(async () => { state.focusedSessionId.set('b'); state.model.set('openrouter-model') })
await settle()
assert.match(document.body.textContent, /OpenRouter · \$12.34 left/)
assert.ok(calls.some(path => path.includes('session_id=b')), 'query follows the focused session')

// a failed refresh never leaves the stale number on screen
fail = true
await act(async () => { chip().click() })
await settle()
assert.equal(document.body.textContent, 'Quota unavailable')
assert.match(chip().title, /restart Hermes once/i)

// disconnected gateway: honest copy and a dead control
await act(async () => { state.gateway.set('closed') })
assert.equal(chip().disabled, true)
assert.equal(document.body.textContent, 'Quota unavailable')

// no chat open → no chip
await act(async () => { state.focusedSessionId.set(null) })
assert.equal(chip(), null)

await act(async () => { root.unmount() })
queryClient.clear()
dom.window.close()
console.log('PASS live mount: provider switch, hover detail, failed refresh, disconnect, unmount')
