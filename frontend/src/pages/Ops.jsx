import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import { isOlderThanHours } from '../lib/format'

function fmtBytes(b) { return b == null ? '—' : b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB` }
function fmtWhen(iso) { return iso ? new Date(iso).toLocaleString() : 'never' }
function ago(iso) {
  if (!iso) return 'never'
  const h = (Date.now() - new Date(iso).getTime()) / 3.6e6
  return h < 1 ? `${Math.round(h * 60)}m ago` : h < 48 ? `${h.toFixed(1)}h ago` : `${Math.round(h / 24)}d ago`
}

export default function Ops() {
  const [health, setHealth] = useState(null)
  const [jobs, setJobs] = useState(null)
  const [costs, setCosts] = useState(null)
  const [funnel, setFunnel] = useState(null)
  const [policy, setPolicy] = useState(null)
  const [error, setError] = useState(null)
  const [toast, setToast] = useState(null)
  const show = (ok, msg) => { setToast({ ok, msg }); setTimeout(() => setToast(null), 5000) }

  const [reloadKey, setReloadKey] = useState(0)
  const loadStatic = useCallback(() => setReloadKey(k => k + 1), [])
  useEffect(() => {
    let cancelled = false
    Promise.all([
      api.get('/api/ops/health'), api.get('/api/metrics/costs'), api.get('/api/metrics/funnel'), api.get('/api/settings/score-policy'),
    ]).then(([h, c, f, p]) => {
      if (cancelled) return
      setHealth({ ...h, refresh_stale: isOlderThanHours(h.freshness?.last_successful_refresh, 48) })
      setCosts(c); setFunnel(f); setPolicy(p); setError(null)
    }).catch(e => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [reloadKey])
  usePoll('/api/jobs?limit=25', { interval: 5000, onData: setJobs })

  const cancel = async (id) => {
    try { const r = await api.post(`/api/jobs/${id}/cancel`); show(true, r.message) } catch (e) { show(false, e.message) }
  }
  const runChecks = async () => {
    try { const r = await api.post('/api/ops/alerts/check'); show(true, r.fired.length ? `Alerts fired: ${r.fired.join(', ')}` : 'No alert conditions met'); loadStatic() } catch (e) { show(false, e.message) }
  }
  const switchPolicy = async (version) => {
    if (!window.confirm(`Switch the displayed score to ${version} and rescore all prospects?`)) return
    try { const r = await api.put('/api/settings/score-policy', { version, rescore: true }); show(true, `Score policy ${r.version} — ${r.rescored} rescored`); loadStatic() } catch (e) { show(false, e.message) }
  }

  const f = health?.freshness || {}
  const t = health?.targets || {}
  const stale = !!health?.refresh_stale

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
        <div>
          <h1 className="font-mono text-lg" style={{ color: 'var(--text-primary)' }}>Operations</h1>
          <div className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
            {health ? `${health.env} · ${health.version} · worker ${health.worker_in_process ? 'in-process' : 'external'} · scheduler ${health.scheduler_enabled ? 'on' : 'off'} · auth ${health.auth_configured ? 'on' : 'OFF'}` : 'Loading…'}
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={runChecks} className="font-mono text-xs px-3 py-1" style={{ background: 'none', border: '1px solid var(--border)', color: 'var(--text-secondary)', cursor: 'pointer' }}>Run alert checks</button>
          <button onClick={loadStatic} className="font-mono text-xs px-3 py-1" style={{ background: 'none', border: '1px solid var(--border)', color: 'var(--text-secondary)', cursor: 'pointer' }}>Refresh</button>
        </div>
      </div>
      {error && <div className="card p-3 mb-4 font-mono text-xs" style={{ color: 'var(--caution)' }}>{error}</div>}
      {health && !health.auth_configured && (
        <div className="card p-3 mb-4 font-mono text-xs" style={{ color: 'var(--caution)', borderLeft: '3px solid var(--caution)' }}>
          Authentication is not configured. Set AUTH_USER and AUTH_PASSWORD on the server.
        </div>
      )}

      <div className="grid gap-3 mb-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))' }}>
        <Tile label="Last successful refresh" value={ago(f.last_successful_refresh)} sub={fmtWhen(f.last_successful_refresh)} warn={stale} />
        <Tile label="Market data fresh (36h)" value={f.detail_freshness_pct != null ? `${f.detail_freshness_pct}%` : '—'}
          sub={`target ≥ ${t.detail_freshness_pct ?? 95}% · ${f.detail_fresh_36h ?? 0}/${f.target_companies ?? 0}`} warn={f.detail_freshness_pct != null && f.detail_freshness_pct < (t.detail_freshness_pct ?? 95)} />
        <Tile label="Refresh success (30d)" value={f.scheduled_success_pct_30d != null ? `${f.scheduled_success_pct_30d}%` : '—'} sub={`${f.failed_refreshes_7d ?? 0} failed in 7d`} warn={f.failed_refreshes_7d > 0} />
        <Tile label="Jobs" value={`${health?.jobs?.queued ?? 0} queued · ${health?.jobs?.running ?? 0} running`} sub={`${health?.jobs?.failed_24h ?? 0} failed 24h · ${health?.jobs?.stale_leases ?? 0} stale leases`} warn={(health?.jobs?.stale_leases ?? 0) > 0} />
        <Tile label="Missing market data" value={`${f.missing_market_cap ?? 0} cap · ${f.missing_volume ?? 0} vol`} sub="target companies without values" />
        <Tile label="Watchlist stale research" value={f.watchlist_stale_research ?? 0} sub=">30 days since enrichment" warn={f.watchlist_stale_research > 0} />
        <Tile label="Contacts unverified 90d" value={f.contacts_unverified_90d ?? 0} sub="emails never/not recently verified" />
        <Tile label="AI spend this month" value={health?.ai_spend ? `$${(health.ai_spend.committed_usd || 0).toFixed(2)}` : '—'}
          sub={health?.ai_spend?.budget_usd ? `budget $${health.ai_spend.budget_usd} · remaining $${(health.ai_spend.remaining_usd || 0).toFixed(2)}` : 'no budget ceiling set'} />
        <Tile label="Database" value={fmtBytes(f.db_size_bytes)} sub={`last backup event ${ago(f.last_backup_event)}`} warn={!f.last_backup_event} />
      </div>

      <div className="grid gap-4 mb-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))' }}>
        <div className="card">
          <div className="px-4 py-3 font-mono text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>Background jobs</div>
          <div className="table-scroll">
            <table className="w-full" style={{ minWidth: 520 }}>
              <tbody>
                {(jobs?.jobs || []).map(j => (
                  <tr key={j.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td className="px-4 py-2 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>{j.job_type}</td>
                    <td className="px-4 py-2 font-mono text-xs" style={{ color: j.status === 'failed' ? 'var(--caution)' : j.status === 'running' ? 'var(--positive)' : 'var(--text-secondary)' }}>{j.status}</td>
                    <td className="px-4 py-2 text-xs" style={{ color: 'var(--text-secondary)' }}>
                      {j.progress?.phase || j.progress?.message || (j.progress?.total ? `${j.progress.current}/${j.progress.total} ${j.progress.ticker || ''}` : '')}
                      {j.error && <div style={{ color: 'var(--caution)' }}>{String(j.error).slice(0, 120)}</div>}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs" style={{ color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>{ago(j.created_at)}</td>
                    <td className="px-4 py-2">
                      {(j.status === 'queued' || j.status === 'running') && (
                        <button onClick={() => cancel(j.id)} className="font-mono text-xs px-2 py-0.5" style={{ border: '1px solid var(--border)', color: 'var(--text-secondary)', background: 'none', cursor: 'pointer' }}>Cancel</button>
                      )}
                    </td>
                  </tr>
                ))}
                {jobs && jobs.jobs.length === 0 && <tr><td className="px-4 py-4 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>No jobs yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <div className="px-4 py-3 font-mono text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>Recent operational events</div>
          <ul>
            {(health?.recent_events || []).map(e => (
              <li key={e.id} className="px-4 py-2 text-xs flex gap-3" style={{ borderBottom: '1px solid var(--border)' }}>
                <span className="font-mono" style={{ color: e.severity === 'error' ? 'var(--caution)' : e.severity === 'warning' ? 'var(--gold)' : 'var(--text-muted)', minWidth: 60 }}>{e.severity}</span>
                <span className="font-mono" style={{ color: 'var(--text-secondary)', minWidth: 150 }}>{e.event_type}</span>
                <span style={{ color: 'var(--text-primary)', flex: 1 }}>{e.message}</span>
                <span className="font-mono" style={{ color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>{ago(e.created_at)}</span>
              </li>
            ))}
            {health && health.recent_events.length === 0 && <li className="px-4 py-4 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>No events recorded.</li>}
          </ul>
        </div>
      </div>

      <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))' }}>
        <div className="card p-4">
          <div className="font-mono text-xs uppercase tracking-widest mb-2" style={{ color: 'var(--text-muted)' }}>Scoring policy</div>
          {policy && (
            <>
              <div className="text-sm mb-2" style={{ color: 'var(--text-primary)' }}>Displayed: <b>{policy.version}</b>
                <span className="font-mono text-xs ml-2" style={{ color: 'var(--text-muted)' }}>avg v1 {policy.comparison.avg_v1 ?? '—'} · avg v2 {policy.comparison.avg_v2 ?? '—'} · {policy.comparison.large_moves} large moves</span>
              </div>
              <ul className="text-xs mb-3" style={{ color: 'var(--text-secondary)' }}>
                <li><b>v1</b> — {policy.policies.v1}</li>
                <li className="mt-1"><b>v2</b> — {policy.policies.v2}</li>
              </ul>
              <div className="flex gap-2">
                {['v1', 'v2'].map(v => (
                  <button key={v} disabled={policy.version === v} onClick={() => switchPolicy(v)} className="font-mono text-xs px-3 py-1"
                    style={{ border: '1px solid var(--border)', background: policy.version === v ? 'var(--card-hover)' : 'none', color: 'var(--text-secondary)', cursor: policy.version === v ? 'default' : 'pointer' }}>
                    {policy.version === v ? `${v} (active)` : `Switch to ${v}`}
                  </button>
                ))}
              </div>
              <div className="font-mono text-xs mt-2" style={{ color: 'var(--text-muted)' }}>Review `python scripts/compare_score_policies.py` output before switching.</div>
            </>
          )}
        </div>

        <div className="card p-4">
          <div className="font-mono text-xs uppercase tracking-widest mb-2" style={{ color: 'var(--text-muted)' }}>AI spend by month (estimated)</div>
          <table className="w-full text-xs">
            <thead><tr style={{ color: 'var(--text-muted)' }}><th className="text-left font-mono">Month</th><th className="text-right font-mono">Calls</th><th className="text-right font-mono">Companies</th><th className="text-right font-mono">Spend</th><th className="text-right font-mono">$/company</th><th className="text-right font-mono">$/qualified</th></tr></thead>
            <tbody>
              {(costs?.months || []).map(m => (
                <tr key={m.period} style={{ color: 'var(--text-secondary)', borderTop: '1px solid var(--border)' }}>
                  <td className="font-mono py-1">{m.period}</td><td className="text-right font-mono">{m.calls}</td><td className="text-right font-mono">{m.companies}</td>
                  <td className="text-right font-mono">${Number(m.spend_usd).toFixed(2)}</td>
                  <td className="text-right font-mono">{m.cost_per_analysed_company_usd != null ? `$${Number(m.cost_per_analysed_company_usd).toFixed(3)}` : '—'}</td>
                  <td className="text-right font-mono">{m.cost_per_qualified_conversation_usd != null ? `$${Number(m.cost_per_qualified_conversation_usd).toFixed(2)}` : '—'}</td>
                </tr>
              ))}
              {costs && costs.months.length === 0 && <tr><td colSpan={6} className="font-mono py-2" style={{ color: 'var(--text-muted)' }}>No paid analysis recorded yet.</td></tr>}
            </tbody>
          </table>
          {costs && <div className="font-mono text-xs mt-2" style={{ color: 'var(--text-muted)' }}>{costs.caveat}</div>}
        </div>

        <div className="card p-4">
          <div className="font-mono text-xs uppercase tracking-widest mb-2" style={{ color: 'var(--text-muted)' }}>Opportunity funnel (n = {funnel?.sample_size ?? 0})</div>
          <div className="flex flex-wrap gap-2 mb-2">
            {['researching', 'outreach_planned', 'contacted', 'meeting', 'proposal', 'won', 'lost'].map(s => (
              <div key={s} className="font-mono text-xs px-2 py-1" style={{ border: '1px solid var(--border)', color: 'var(--text-secondary)' }}>
                {s.replace(/_/g, ' ')}: <b style={{ color: 'var(--text-primary)' }}>{funnel?.by_stage?.[s] ?? 0}</b>
              </div>
            ))}
          </div>
          {funnel?.lost_reasons?.length > 0 && (
            <div className="text-xs" style={{ color: 'var(--text-secondary)' }}>Lost reasons: {funnel.lost_reasons.map(r => `${r.reason} (${r.n})`).join(' · ')}</div>
          )}
          {funnel && <div className="font-mono text-xs mt-2" style={{ color: 'var(--text-muted)' }}>{funnel.caveat}</div>}
        </div>
      </div>

      {toast && (
        <div role="status" className="fixed bottom-4 right-4 font-mono text-xs px-4 py-3 card"
          style={{ borderLeft: `3px solid ${toast.ok ? 'var(--positive)' : 'var(--caution)'}`, color: 'var(--text-primary)' }}>{toast.msg}</div>
      )}
    </div>
  )
}

function Tile({ label, value, sub, warn }) {
  return (
    <div className="card p-3" style={{ borderLeft: warn ? '3px solid var(--caution)' : undefined }}>
      <div className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{label}</div>
      <div className="font-mono text-base mt-1" style={{ color: warn ? 'var(--caution)' : 'var(--text-primary)' }}>{value}</div>
      {sub && <div className="font-mono text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>{sub}</div>}
    </div>
  )
}
