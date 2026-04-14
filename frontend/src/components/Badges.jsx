// New Delta 6-Pillar color scheme
const PILLAR_COLORS = {
  production:         { bg: '#2a1500', border: '#7c2d06', text: '#f97316' },
  license_to_operate: { bg: '#1c1700', border: '#713f12', text: '#eab308' },
  cost:               { bg: '#1f0808', border: '#7f1d1d', text: '#ef4444' },
  people:             { bg: '#0a1628', border: '#1e3a5f', text: '#3b82f6' },
  quality:            { bg: '#042f2e', border: '#134e4a', text: '#14b8a6' },
  future_readiness:   { bg: '#1a0a2e', border: '#581c87', text: '#a855f7' },
}

const PILLAR_LABELS = {
  production: 'Production',
  license_to_operate: 'License to Operate',
  cost: 'Cost',
  people: 'People',
  quality: 'Quality',
  future_readiness: 'Future Readiness',
}

const STRENGTH_STYLES = {
  strong:   { color: '#f8fafc', fontWeight: 600 },
  moderate: { color: '#94a3b8', fontWeight: 500 },
  weak:     { color: '#475569', fontWeight: 400 },
}

const STATUS_COLORS = {
  unscreened:       { bg: '#111418', text: '#4a5a70', border: '#1e2530' },
  qualified:        { bg: '#0a1628', text: '#3b82f6', border: '#1e3a5f' },
  enriched:         { bg: '#042f2e', text: '#14b8a6', border: '#134e4a' },
  ready_for_outreach:{ bg: '#052e16', text: '#22c55e', border: '#14532d' },
  suggested_dq:     { bg: '#1c1700', text: '#eab308', border: '#713f12' },
  disqualified:     { bg: '#1f0808', text: '#ef4444', border: '#7f1d1d' },
  archived:         { bg: '#111418', text: '#4a5a70', border: '#1e2530' },
}

const TIER_COLORS = {
  hot:            { bg: '#1f0808', border: '#991b1b', text: '#ef4444', label: 'Hot' },
  warm:           { bg: '#1c1700', border: '#92400e', text: '#f97316', label: 'Warm' },
  watch:          { bg: '#1c1700', border: '#713f12', text: '#eab308', label: 'Watch' },
  not_qualified:  { bg: '#111418', border: '#1e2530', text: '#4a5a70', label: 'Not Qualified' },
}

export function PillarBadge({ type, size = 'sm' }) {
  const c = PILLAR_COLORS[type] || { bg: '#111418', border: '#1e2530', text: '#8fa3bf' }
  const label = PILLAR_LABELS[type] || type
  return (
    <span
      className="font-mono uppercase tracking-wider"
      style={{
        background: c.bg,
        border: `1px solid ${c.border}`,
        color: c.text,
        fontSize: size === 'sm' ? '0.65rem' : '0.7rem',
        padding: size === 'sm' ? '1px 6px' : '2px 8px',
        display: 'inline-block',
      }}
    >
      {label}
    </span>
  )
}

// Keep old name as alias for backward compatibility in Deep Intelligence
export const PressureBadge = PillarBadge

export function StrengthBadge({ strength }) {
  const s = STRENGTH_STYLES[strength] || { color: '#8fa3bf', fontWeight: 400 }
  return (
    <span
      className="font-mono uppercase tracking-wider text-xs"
      style={{ color: s.color, fontWeight: s.fontWeight }}
    >
      {strength}
    </span>
  )
}

export function StatusBadge({ status }) {
  const label = (status || '').replace(/_/g, ' ')
  const c = STATUS_COLORS[status] || STATUS_COLORS.unscreened
  return (
    <span
      className="font-mono uppercase tracking-wider"
      style={{
        background: c.bg,
        border: `1px solid ${c.border}`,
        color: c.text,
        fontSize: '0.65rem',
        padding: '1px 6px',
        display: 'inline-block',
      }}
    >
      {label}
    </span>
  )
}

export function LeadTierBadge({ tier }) {
  const t = TIER_COLORS[tier] || TIER_COLORS.not_qualified
  return (
    <span
      className="font-mono uppercase tracking-wider font-semibold"
      style={{
        background: t.bg,
        border: `1px solid ${t.border}`,
        color: t.text,
        fontSize: '0.65rem',
        padding: '2px 8px',
        display: 'inline-block',
      }}
    >
      {t.label}
    </span>
  )
}

export function ScoreBar({ score, max = 25 }) {
  const pct = Math.min(100, ((score || 0) / max) * 100)
  const color = score >= 15 ? '#22c55e' : score >= 8 ? '#eab308' : '#3b82f6'
  return (
    <div className="flex items-center gap-2">
      <div style={{ width: 60, height: 4, background: '#1e2530', borderRadius: 0 }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color }} />
      </div>
      <span className="font-mono text-xs" style={{ color: '#8fa3bf' }}>
        {score ? score.toFixed(1) : '\u2014'}
      </span>
    </div>
  )
}

export { PILLAR_COLORS, PILLAR_LABELS, TIER_COLORS }
