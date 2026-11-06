import { host, useValue, useQuery, queryClient, COMPOSER_AREAS } from '@hermes/plugin-sdk'
import { useEffect } from 'react'
import { jsx } from 'react/jsx-runtime'

const NAMES = {
  'openai-codex': 'Codex',
  'opencode-go': 'OpenCode Go',
  anthropic: 'Claude',
  openrouter: 'OpenRouter',
  nous: 'Nous',
}

const BALANCE_LINE = /^(?:Credits balance|Total usable|Top-up credits|Subscription credits):\s*\$?([\d,.]+)/

export function providerLabel(provider) {
  const name = String(provider || '').trim()
  if (NAMES[name]) return NAMES[name]
  const words = name.replace(/[-_]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  return words || 'Provider'
}

export function resetText(value, now = Date.now()) {
  const delta = Date.parse(value) - now
  if (!Number.isFinite(delta)) return ''
  if (delta <= 0) return 'reset due'
  const minutes = Math.ceil(delta / 60000)
  if (minutes >= 1440) return `resets in ${Math.floor(minutes / 1440)}d ${Math.floor((minutes % 1440) / 60)}h`
  return minutes >= 60 ? `resets in ${Math.floor(minutes / 60)}h ${minutes % 60}m` : `resets in ${minutes}m`
}

/**
 * Turn one `/quota` payload into the chip's one line plus its hover breakdown.
 * The most depleted window is always the headline: showing the healthiest
 * window can hide an exhausted one.
 */
export function describeQuota(data, now = Date.now()) {
  const name = providerLabel(data?.provider)
  if (!data?.available) {
    return { text: `${name} · quota unavailable`, title: data?.reason || 'Quota could not be read.', low: false }
  }
  const windows = (data.windows || []).filter(
    w => typeof w.used_percent === 'number' && Number.isFinite(w.used_percent),
  )
  const details = data.details || []
  const lines = windows.map(w => {
    const reset = w.reset_at ? ` · ${resetText(w.reset_at, now)}` : ''
    const detail = w.detail ? ` · ${w.detail}` : ''
    return `${w.label}: ${Math.max(0, Math.round(100 - w.used_percent))}% left${reset}${detail}`
  })
  const limiting = windows.reduce((worst, w) => (!worst || w.used_percent > worst.used_percent ? w : worst), null)
  const balance = details.map(line => BALANCE_LINE.exec(line)).find(Boolean)
  const left = limiting ? Math.max(0, Math.round(100 - limiting.used_percent)) : null
  const text = left !== null
    ? `${name} · ${left}% left${limiting.reset_at ? ' · ' + resetText(limiting.reset_at, now) : ''}`
    : balance ? `${name} · $${balance[1]} left`
      : `${name} · quota unavailable`
  return {
    text,
    title: [
      name + (data.plan ? ` (${data.plan})` : ''),
      ...lines,
      ...details,
      data.checked_at ? `Checked ${new Date(data.checked_at).toLocaleTimeString()}` : '',
      'Click to refresh',
    ].filter(Boolean).join('\n'),
    low: left !== null && left <= 15,
  }
}

function QuotaChip({ ctx }) {
  const sessionId = useValue(host.state.focusedSessionId)
  const profile = useValue(host.state.focusedSessionProfile)
  const model = useValue(host.state.model)
  const gateway = useValue(host.state.gateway)

  // A provider switch or a resumed chat changes the account under the chip.
  useEffect(() => host.onEvent('session.info', () => {
    void queryClient.invalidateQueries({ queryKey: ['provider-quota', profile, sessionId] })
  }), [profile, sessionId])

  const query = useQuery({
    queryKey: ['provider-quota', profile, sessionId, model, gateway],
    queryFn: () => ctx.rest(`/quota?session_id=${encodeURIComponent(sessionId)}`, { timeoutMs: 25000 }),
    enabled: Boolean(sessionId) && gateway === 'open',
    refetchInterval: 60000,
    staleTime: 45000,
    retry: false,
    refetchOnWindowFocus: true,
  })

  if (!sessionId) return null
  const offline = gateway !== 'open'
  const view = query.isError || offline
    ? {
      text: 'Quota unavailable',
      title: offline
        ? 'Gateway disconnected.'
        : 'Quota backend unavailable. If just installed, restart Hermes once to activate it.',
      low: false,
    }
    : query.data
      ? describeQuota(query.data)
      : { text: 'Quota…', title: 'Reading provider allowance (no model requests).', low: false }

  return jsx('button', {
    type: 'button',
    'data-provider-quota': 'true',
    'aria-label': view.text,
    title: view.title,
    disabled: query.isFetching || offline,
    onClick: () => void query.refetch(),
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      whiteSpace: 'nowrap',
      maxWidth: '320px',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      border: 'none',
      background: 'transparent',
      font: 'inherit',
      fontSize: '11px',
      padding: '3px 6px',
      borderRadius: '5px',
      cursor: 'pointer',
      color: view.low ? 'var(--ui-accent)' : 'var(--ui-text-tertiary)',
      opacity: query.isFetching && query.data ? 0.65 : 1,
    },
    children: view.text,
  })
}

export default {
  id: 'provider-quota',
  name: 'Provider Quota',
  register(ctx) {
    ctx.register({
      id: 'current-provider',
      area: COMPOSER_AREAS.actions,
      order: 90,
      render: () => jsx(QuotaChip, { ctx }),
    })
  },
}
