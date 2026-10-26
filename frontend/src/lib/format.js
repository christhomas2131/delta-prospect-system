// Formatting helpers shared by components (kept out of component files for fast refresh).

export function fmtAud(dollars) {
  if (dollars == null) return '—'
  const n = Number(dollars)
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `$${Math.round(n / 1e3)}K`
  return `$${n}`
}

export function freshnessLabel(days) {
  if (days == null) return { text: 'no dated evidence', tone: 'var(--text-muted)' }
  if (days <= 30) return { text: `${days}d old`, tone: 'var(--positive)' }
  if (days <= 180) return { text: `${days}d old`, tone: 'var(--text-secondary)' }
  if (days <= 365) return { text: `${days}d old — ageing`, tone: 'var(--caution)' }
  return { text: `${days}d old — stale`, tone: 'var(--negative, #c0392b)' }
}

export const STAGES = ['none', 'researching', 'outreach_planned', 'contacted', 'meeting', 'proposal', 'won', 'lost']

export function isOlderThanHours(iso, hours) {
  return !!iso && (Date.now() - new Date(iso).getTime()) > hours * 3.6e6
}
