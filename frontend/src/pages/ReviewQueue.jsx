import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'

const REASONS = {
  overdue_action: { label: 'Overdue actions', tone: 'var(--caution)' },
  new_evidence: { label: 'New evidence', tone: 'var(--positive)' },
  stale_research: { label: 'Stale research', tone: 'var(--text-secondary)' },
  dq_review: { label: 'DQ review', tone: 'var(--info)' },
}

export default function ReviewQueue() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const reasonFilter = params.get('reason') || ''
  const owner = params.get('owner') || ''
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState(null)

  const [reloadKey, setReloadKey] = useState(0)
  const load = useCallback(() => { setLoading(true); setReloadKey(k => k + 1) }, [])

  useEffect(() => {
    const controller = new AbortController()
    const q = new URLSearchParams({ limit: 300 })
    if (owner) q.set('owner', owner)
    api.get(`/api/review-queue?${q}`, { signal: controller.signal })
      .then(d => { setData(d); setError(null); setLoading(false) })
      .catch(e => { if (e && e.name === 'AbortError') return; setError(e.message); setLoading(false) })
    return () => controller.abort()
  }, [owner, reloadKey])

  const show = (ok, msg) => { setToast({ ok, msg }); setTimeout(() => setToast(null), 5000) }

  const decideDq = async (e, item, action) => {
    e.stopPropagation()
    try {
      await api.post(`/api/prospects/${item.prospect_id}/dq/${action}`, { actor: 'analyst' })
      show(true, action === 'reinstate' ? `${item.ticker} reinstated to qualified` : `${item.ticker} kept disqualified`)
      load()
    } catch (err) {
      show(false, err.message)
    }
  }

  const items = (data?.items || []).filter(i => !reasonFilter || i.reason === reasonFilter)

  return (
    <div className="p-4 md:p-6">
      <div className="flex flex-wrap items-end justify-between gap-3 mb-4">
        <div>
          <h1 className="font-mono text-lg" style={{ color: 'var(--text-primary)' }}>Review Queue</h1>
          <div className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
            What needs attention today and why. {data && `Generated ${new Date(data.generated_at).toLocaleTimeString()}.`}
          </div>
        </div>
        <div className="flex gap-2 items-center">
          <input value={owner} placeholder="filter by owner" aria-label="Filter by owner"
            onChange={e => { const n = new URLSearchParams(params); e.target.value ? n.set('owner', e.target.value) : n.delete('owner'); setParams(n, { replace: true }) }}
            className="font-mono text-xs px-2 py-1" style={{ background: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)' }} />
          <button onClick={load} className="font-mono text-xs px-3 py-1" style={{ background: 'none', border: '1px solid var(--border)', color: 'var(--text-secondary)', cursor: 'pointer' }}>Refresh</button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 mb-4" role="tablist" aria-label="Reason filter">
        <Chip active={!reasonFilter} onClick={() => { const n = new URLSearchParams(params); n.delete('reason'); setParams(n, { replace: true }) }}>
          All {data ? `(${data.total})` : ''}
        </Chip>
        {Object.entries(REASONS).map(([key, meta]) => (
          <Chip key={key} active={reasonFilter === key} tone={meta.tone}
            onClick={() => { const n = new URLSearchParams(params); n.set('reason', key); setParams(n, { replace: true }) }}>
            {meta.label} {data?.counts?.[key] ? `(${data.counts[key]})` : '(0)'}
          </Chip>
        ))}
      </div>

      {error && <div className="card p-4 font-mono text-xs" style={{ color: 'var(--caution)' }}>{error}</div>}
      {loading && !data && <div className="card p-6 font-mono text-xs text-center" style={{ color: 'var(--text-muted)' }}>Loading…</div>}
      {data && items.length === 0 && (
        <div className="card p-8 font-mono text-xs text-center" style={{ color: 'var(--text-muted)' }}>
          Nothing in the queue{reasonFilter ? ` for ${REASONS[reasonFilter]?.label}` : ''}. Good.
        </div>
      )}
      {items.length > 0 && (
        <div className="card table-scroll">
          <table className="w-full sticky-head" style={{ minWidth: 820 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {['Reason', 'Company', 'Why', 'Owner', 'Stage', 'Score', ''].map(h => (
                  <th key={h || 'act'} className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: 'var(--text-muted)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((it, i) => (
                <tr key={`${it.reason}-${it.prospect_id}-${i}`} tabIndex={0} role="link"
                  onClick={() => navigate(`/deep-intelligence/${it.prospect_id}`)}
                  onKeyDown={e => { if (e.key === 'Enter') navigate(`/deep-intelligence/${it.prospect_id}`) }}
                  style={{ borderBottom: '1px solid var(--border)', cursor: 'pointer' }}>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: REASONS[it.reason]?.tone }}>{REASONS[it.reason]?.label || it.reason}</td>
                  <td className="px-4 py-2.5 text-sm" style={{ color: 'var(--text-primary)' }}>
                    <span className="font-mono text-xs mr-2" style={{ color: 'var(--accent)' }}>{it.ticker}</span>{it.company_name}
                  </td>
                  <td className="px-4 py-2.5 text-xs" style={{ color: 'var(--text-secondary)' }}>{it.explanation}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{it.owner || '—'}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{(it.opportunity_stage || 'none').replace(/_/g, ' ')}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{it.prospect_score != null ? Number(it.prospect_score).toFixed(1) : '—'}</td>
                  <td className="px-4 py-2.5">
                    {it.reason === 'dq_review' && (
                      <div className="flex gap-1">
                        <button onClick={e => decideDq(e, it, 'reinstate')} className="font-mono text-xs px-2 py-0.5"
                          style={{ border: '1px solid var(--positive)', color: 'var(--positive)', background: 'none', cursor: 'pointer' }}>Reinstate</button>
                        <button onClick={e => decideDq(e, it, 'dismiss-review')} className="font-mono text-xs px-2 py-0.5"
                          style={{ border: '1px solid var(--border)', color: 'var(--text-secondary)', background: 'none', cursor: 'pointer' }}>Keep DQ</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {toast && (
        <div role="status" className="fixed bottom-4 right-4 font-mono text-xs px-4 py-3 card"
          style={{ borderLeft: `3px solid ${toast.ok ? 'var(--positive)' : 'var(--caution)'}`, color: 'var(--text-primary)' }}>{toast.msg}</div>
      )}
    </div>
  )
}

function Chip({ active, tone, onClick, children }) {
  return (
    <button role="tab" aria-selected={active} onClick={onClick} className="font-mono text-xs px-3 py-1"
      style={{ background: active ? 'var(--card-hover)' : 'none', border: `1px solid ${active ? (tone || 'var(--accent)') : 'var(--border)'}`,
        color: active ? (tone || 'var(--text-primary)') : 'var(--text-secondary)', cursor: 'pointer' }}>
      {children}
    </button>
  )
}
