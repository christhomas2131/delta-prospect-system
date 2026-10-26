// Explains why a company scores what it does, how fresh the evidence is, and
// what the opportunity estimate assumes. Pure presentation — no fetching.

const PILLAR_LABELS = {
  production: 'Production', license_to_operate: 'License to Operate', cost: 'Cost',
  people: 'People', quality: 'Quality', future_readiness: 'Future Readiness',
}

import { fmtAud, freshnessLabel } from '../lib/format'

export default function ScoreBreakdown({ prospect }) {
  if (!prospect) return null
  const b = prospect.score_breakdown || {}
  const pillars = b.pillars || {}
  const policy = b.displayed_policy || prospect.score_policy_version || 'v1'
  const pb = prospect.prize_breakdown || {}
  const range = pb.range
  const fresh = freshnessLabel(b.days_since_latest_evidence)
  const pillarRows = Object.entries(pillars).sort((a, z) => Number(z[1].contribution) - Number(a[1].contribution))
  const maxContribution = Math.max(1, ...pillarRows.map(([, v]) => Number(v.contribution) || 0))

  return (
    <div className="card p-4 mb-4" data-testid="score-breakdown">
      <div className="flex items-center justify-between mb-3">
        <span className="font-mono text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
          Why this score
        </span>
        <span className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
          policy {policy} · evidence <span style={{ color: fresh.tone }}>{fresh.text}</span>
        </span>
      </div>

      <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))' }}>
        <Stat label="Displayed score" value={prospect.prospect_score != null ? Number(prospect.prospect_score).toFixed(1) : '—'} />
        <Stat label="v1 (legacy)" value={b.score_v1 != null ? Number(b.score_v1).toFixed(1) : '—'} muted />
        <Stat label="v2 (explainable)" value={b.score_v2 != null ? Number(b.score_v2).toFixed(1) : '—'} muted />
        <Stat label="Fit factor" value={b.fit_factor != null ? `×${Number(b.fit_factor).toFixed(2)}` : '—'}
          hint={b.likelihood != null ? `likelihood ${b.likelihood}/10` : ''} />
        <Stat label="Signals used" value={b.signals_used ?? '—'}
          hint={b.signals_excluded ? `${b.signals_excluded} excluded (rejected/superseded)` : ''} />
      </div>

      {pillarRows.length > 0 && (
        <div className="mt-4">
          {pillarRows.map(([pillar, v]) => (
            <div key={pillar} className="flex items-center gap-3 mb-1.5">
              <div className="font-mono text-xs" style={{ width: 150, color: 'var(--text-secondary)' }}>{PILLAR_LABELS[pillar] || pillar}</div>
              <div style={{ flex: 1, height: 6, background: 'var(--border)' }}>
                <div style={{ width: `${(100 * Number(v.contribution || 0)) / maxContribution}%`, height: 6, background: 'var(--accent)' }} />
              </div>
              <div className="font-mono text-xs" style={{ width: 170, color: 'var(--text-muted)' }}>
                {Number(v.contribution || 0).toFixed(1)} · {v.signals} sig{v.strong ? ` · ${v.strong} strong` : ''}
                {v.latest_evidence ? ` · ${String(v.latest_evidence).slice(0, 10)}` : ''}
              </div>
            </div>
          ))}
        </div>
      )}

      {range && (
        <div className="mt-4 pt-3" style={{ borderTop: '1px solid var(--border)' }}>
          <div className="font-mono text-xs uppercase tracking-widest mb-2" style={{ color: 'var(--text-muted)' }}>
            Opportunity estimate ({pb.currency || 'AUD'}, client improvement — not fees)
          </div>
          <div className="font-mono text-sm" style={{ color: 'var(--text-primary)' }}>
            {fmtAud(range.low)} – <b>{fmtAud(range.base)}</b> – {fmtAud(range.high)}
            {pb.fee_estimate && (
              <span style={{ color: 'var(--text-muted)' }}> · indicative fee {fmtAud(pb.fee_estimate.low)}–{fmtAud(pb.fee_estimate.high)}</span>
            )}
            {pb.confidence_label && <span style={{ color: 'var(--text-muted)' }}> · {pb.confidence_label}</span>}
          </div>
          {Array.isArray(pb.assumptions) && (
            <ul className="mt-2 pl-4 text-xs" style={{ color: 'var(--text-muted)', listStyle: 'disc' }}>
              {pb.assumptions.map((a, i) => <li key={i}>{a}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, hint, muted }) {
  return (
    <div>
      <div className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{label}</div>
      <div className="font-mono text-lg" style={{ color: muted ? 'var(--text-secondary)' : 'var(--text-primary)' }}>{value}</div>
      {hint && <div className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{hint}</div>}
    </div>
  )
}
