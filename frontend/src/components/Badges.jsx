// Delta 6-Pillar badge system, theme-aware via CSS design tokens.
const PILLAR_COLORS = {
  production:         { bg: 'var(--ops-bg)',        border: 'var(--ops-border)',        text: 'var(--ops)' },
  license_to_operate: { bg: 'var(--caution-bg)',    border: 'var(--caution-border)',    text: 'var(--caution)' },
  cost:               { bg: 'var(--risk-bg)',       border: 'var(--risk-border)',       text: 'var(--risk)' },
  people:             { bg: 'var(--accent-bg)',     border: 'var(--accent-border)',     text: 'var(--info)' },
  quality:            { bg: 'var(--teal-bg)',       border: 'var(--teal-border)',       text: 'var(--teal)' },
  future_readiness:   { bg: 'var(--purple-bg)',     border: 'var(--purple-border)',     text: 'var(--purple)' },
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
  strong:   { color: 'var(--text-bright)',  fontWeight: 600 },
  moderate: { color: 'var(--text-secondary)', fontWeight: 500 },
  weak:     { color: 'var(--text-muted)',   fontWeight: 400 },
}

const STATUS_COLORS = {
  unscreened:        { bg: 'var(--card)',           text: 'var(--text-muted)',    border: 'var(--border)' },
  qualified:         { bg: 'var(--accent-bg)',      text: 'var(--info)',          border: 'var(--accent-border)' },
  enriched:          { bg: 'var(--teal-bg)',        text: 'var(--teal)',          border: 'var(--teal-border)' },
  ready_for_outreach:{ bg: 'var(--positive-bg)',    text: 'var(--positive)',      border: 'var(--positive-border)' },
  suggested_dq:      { bg: 'var(--caution-bg)',     text: 'var(--caution)',       border: 'var(--caution-border)' },
  disqualified:      { bg: 'var(--risk-bg)',        text: 'var(--risk)',          border: 'var(--risk-border)' },
  archived:          { bg: 'var(--card)',           text: 'var(--text-muted)',    border: 'var(--border)' },
}

const TIER_COLORS = {
  hot:            { bg: 'var(--risk-bg)',    border: 'var(--risk-border)',    text: 'var(--risk)',    label: 'Hot' },
  warm:           { bg: 'var(--ops-bg)',     border: 'var(--ops-border)',     text: 'var(--ops)',     label: 'Warm' },
  watch:          { bg: 'var(--caution-bg)', border: 'var(--caution-border)', text: 'var(--caution)', label: 'Watch' },
  not_qualified:  { bg: 'var(--card)',       border: 'var(--border)',         text: 'var(--text-muted)', label: 'Not Qualified' },
}

const badgeBase = {
  display: 'inline-block',
  lineHeight: 1.4,
  borderRadius: 2,
}

export function PillarBadge({ type, size = 'sm' }) {
  const c = PILLAR_COLORS[type] || { bg: 'var(--card)', border: 'var(--border)', text: 'var(--text-secondary)' }
  const label = PILLAR_LABELS[type] || type
  return (
    <span
      className="font-mono uppercase tracking-wider"
      style={{
        ...badgeBase,
        background: c.bg,
        border: `1px solid ${c.border}`,
        color: c.text,
        fontSize: size === 'sm' ? '0.65rem' : '0.7rem',
        padding: size === 'sm' ? '2px 7px' : '3px 9px',
      }}
    >
      {label}
    </span>
  )
}

export const PressureBadge = PillarBadge

export function StrengthBadge({ strength }) {
  const s = STRENGTH_STYLES[strength] || { color: 'var(--text-secondary)', fontWeight: 400 }
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
        ...badgeBase,
        background: c.bg,
        border: `1px solid ${c.border}`,
        color: c.text,
        fontSize: '0.65rem',
        padding: '2px 7px',
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
        ...badgeBase,
        background: t.bg,
        border: `1px solid ${t.border}`,
        color: t.text,
        fontSize: '0.65rem',
        padding: '2px 8px',
      }}
    >
      {t.label}
    </span>
  )
}

export function ScoreBar({ score, max = 25 }) {
  const pct = Math.min(100, ((score || 0) / max) * 100)
  const color = score >= 15 ? 'var(--positive)' : score >= 8 ? 'var(--caution)' : 'var(--accent)'
  return (
    <div className="flex items-center gap-2">
      <div style={{ width: 64, height: 5, background: 'var(--border)', borderRadius: 2 }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 2, transition: 'width 0.3s ease' }} />
      </div>
      <span className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>
        {score ? score.toFixed(1) : '\u2014'}
      </span>
    </div>
  )
}

export { PILLAR_COLORS, PILLAR_LABELS, TIER_COLORS }
