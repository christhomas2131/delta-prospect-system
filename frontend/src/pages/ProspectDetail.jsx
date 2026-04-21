import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { PressureBadge, StrengthBadge, StatusBadge } from '../components/Badges'

const GOLD = '#D4AF37'
const GOLD_BG = '#1a1508'
const GOLD_BORDER = '#8B7120'

const STATUS_ACTIONS = [
  { value: 'qualified', label: 'Qualify' },
  { value: 'enriched', label: 'Mark Enriched' },
  { value: 'ready_for_outreach', label: 'Ready for Outreach' },
  { value: 'suggested_dq', label: 'Suggest DQ' },
]

function fmtMarketCap(cents) {
  if (!cents) return null
  const aud = cents / 100
  if (aud >= 1e9) return `$${(aud / 1e9).toFixed(1)}B`
  if (aud >= 1e6) return `$${(aud / 1e6).toFixed(0)}M`
  return `$${(aud / 1e3).toFixed(0)}K`
}

// Small inline badge for signal source type
function SourceBadge({ signal }) {
  const isAI = signal.model_version === 'claude-deep-v1'
  const isValidatedByAI = signal.validated_by === 'claude-deep-v1' && signal.is_valid === true
  const isDisputedByAI = signal.validated_by === 'claude-deep-v1' && signal.is_valid === false

  return (
    <span className="flex gap-1 flex-wrap mt-0.5">
      {!isAI && (
        <span className="font-mono text-xs px-1 py-0.5 leading-none"
          style={{ background: '#111c2b', border: '1px solid #1e3a5f', color: '#4a8fbf', fontSize: 9 }}>
          RULE
        </span>
      )}
      {isAI && (
        <span className="font-mono text-xs px-1 py-0.5 leading-none"
          style={{ background: GOLD_BG, border: `1px solid ${GOLD_BORDER}`, color: GOLD, fontSize: 9 }}>
          AI
        </span>
      )}
      {isValidatedByAI && !isAI && (
        <span className="font-mono text-xs px-1 py-0.5 leading-none"
          style={{ background: GOLD_BG, border: `1px solid ${GOLD_BORDER}`, color: GOLD, fontSize: 9 }}>
          AI VERIFIED
        </span>
      )}
      {isDisputedByAI && (
        <span className="font-mono text-xs px-1 py-0.5 leading-none"
          style={{ background: '#1f0808', border: '1px solid #7f1d1d', color: '#ef4444', fontSize: 9 }}>
          AI DISPUTED
        </span>
      )}
    </span>
  )
}

export default function ProspectDetail() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [prospect, setProspect] = useState(null)
  const [signals, setSignals] = useState([])
  const [loading, setLoading] = useState(true)
  const [notes, setNotes] = useState('')
  const [saving, setSaving] = useState(false)
  const [enriching, setEnriching] = useState(false)
  const [deepAnalysing, setDeepAnalysing] = useState(false)
  const [toast, setToast] = useState(null)
  const [deepAvailable, setDeepAvailable] = useState(false)
  const [lastDeepAt, setLastDeepAt] = useState(null)
  const [deepResult, setDeepResult] = useState(null)
  const [deepJob, setDeepJob] = useState(null)
  const [isWatchlisted, setIsWatchlisted] = useState(false)

  const [error, setError] = useState(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await fetch(`/api/prospects/${id}`)
      if (!r.ok) throw new Error(r.status === 404 ? 'not_found' : 'api_error')
      const d = await r.json()
      setProspect(d.prospect)
      setSignals(d.signals || [])
      setNotes(d.prospect.analyst_notes || '')
      setDeepAvailable(d.deep_analysis_available || false)
      setLastDeepAt(d.last_deep_analysis_at || null)
      setIsWatchlisted(!!d.prospect.is_watchlisted)
    } catch (e) {
      if (e.message === 'not_found') setProspect(null)
      else setError('Cannot reach the API — is the backend running?')
    }
    setLoading(false)
  }

  useEffect(() => { load() }, [id])

  useEffect(() => {
    let cancelled = false
    const loadDeepStatus = async () => {
      try {
        const r = await fetch(`/api/prospects/${id}/deep-analysis/status`)
        if (!r.ok) return
        const d = await r.json()
        if (!cancelled) {
          setDeepJob(d)
          setDeepAnalysing(d.status === 'running')
        }
      } catch {
        // ignore
      }
    }
    loadDeepStatus()
    return () => { cancelled = true }
  }, [id])

  useEffect(() => {
    if (!id || !deepAnalysing) return

    const poll = setInterval(async () => {
      try {
        const r = await fetch(`/api/prospects/${id}/deep-analysis/status`)
        if (!r.ok) return
        const d = await r.json()
        setDeepJob(d)

        if (d.status === 'completed') {
          clearInterval(poll)
          setDeepAnalysing(false)
          if (d.result) {
            setDeepResult(d.result)
            showToast(true,
              `Deep analysis complete — ${d.result.new_signals_count} new signals, ` +
              `${d.result.confirmed_count} confirmed, ${d.result.disputed_count} disputed ` +
              `(${d.result.tokens_used?.toLocaleString() || 0} tokens)`
            )
            load()
          }
        } else if (d.status === 'failed') {
          clearInterval(poll)
          setDeepAnalysing(false)
          showToast(false, d.error || d.message || 'Deep analysis failed')
        }
      } catch {
        // ignore polling blips
      }
    }, 2000)

    return () => clearInterval(poll)
  }, [id, deepAnalysing])

  const showToast = (ok, msg) => {
    setToast({ ok, msg })
    setTimeout(() => setToast(null), 5000)
  }

  const saveNotes = async () => {
    setSaving(true)
    const r = await fetch(`/api/prospects/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ analyst_notes: notes }),
    })
    if (r.ok) showToast(true, 'Notes saved')
    else showToast(false, 'Save failed')
    setSaving(false)
  }

  const updateStatus = async (newStatus) => {
    const r = await fetch(`/api/prospects/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus }),
    })
    if (r.ok) { showToast(true, `Status → ${newStatus.replace(/_/g, ' ')}`); load() }
    else showToast(false, 'Update failed')
  }

  const toggleWatchlist = async () => {
    const newVal = !isWatchlisted
    setIsWatchlisted(newVal)
    try {
      await fetch(`/api/prospects/${id}/watchlist`, { method: 'PATCH' })
    } catch {
      setIsWatchlisted(!newVal)
    }
  }

  const triggerEnrich = async () => {
    setEnriching(true)
    const r = await fetch(`/api/enrich/${prospect.ticker}`, { method: 'POST' })
    const d = await r.json()
    showToast(r.ok, d.message || 'Enrichment triggered')
    setEnriching(false)
  }

  const triggerDeepAnalysis = async () => {
    setDeepResult(null)
    try {
      const r = await fetch(`/api/prospects/${id}/deep-analysis/start`, { method: 'POST' })
      const d = await r.json()
      if (!r.ok) {
        showToast(false, d.detail || 'Deep analysis failed')
      } else {
        setDeepJob(d)
        setDeepAnalysing(true)
      }
    } catch {
      showToast(false, 'Request failed')
    }
  }

  const hasAISignals = signals.some(
    s => s.model_version === 'claude-deep-v1' || s.validated_by === 'claude-deep-v1'
  )

  const copyBrief = () => {
    if (!prospect) return
    const topSignals = signals
      .filter(s => s.strength === 'strong')
      .slice(0, 3)
      .map(s => `  • ${s.pressure_type}: ${s.summary}`)
      .join('\n')

    const brief = [
      `${prospect.ticker} — ${prospect.company_name}`,
      `${prospect.gics_sector} | Score: ${prospect.prospect_score ? Number(prospect.prospect_score).toFixed(1) : 'N/A'} | Likelihood: ${prospect.likelihood_score || 'N/A'}/10`,
      prospect.primary_headwind ? `Headwind: ${prospect.primary_headwind}` : null,
      topSignals ? `Key signals:\n${topSignals}` : null,
    ].filter(Boolean).join('\n')

    navigator.clipboard.writeText(brief)
    showToast(true, 'Brief copied to clipboard')
  }

  if (loading) return (
    <div className="p-6 font-mono text-xs" style={{ color: '#4a5a70' }}>Loading prospect...</div>
  )
  if (error) return (
    <div className="p-6">
      <div className="card p-6 text-center" style={{ borderLeft: '3px solid #ef4444' }}>
        <div className="font-mono text-sm mb-2" style={{ color: '#ef4444' }}>⚠ Connection Error</div>
        <div className="text-sm mb-4" style={{ color: '#8fa3bf' }}>{error}</div>
        <button onClick={load} className="font-mono text-xs px-4 py-2"
          style={{ background: '#1e6fd4', color: '#fff', border: 'none', cursor: 'pointer' }}>
          Retry
        </button>
      </div>
    </div>
  )
  if (!prospect) return (
    <div className="p-6 font-mono text-xs" style={{ color: '#ef4444' }}>Prospect not found.</div>
  )

  return (
    <div className="p-6 max-w-5xl">
      {/* Back */}
      <button onClick={() => navigate('/prospects')} className="font-mono text-xs mb-4 flex items-center gap-1"
        style={{ background: 'none', border: 'none', color: '#8fa3bf', cursor: 'pointer' }}>
        ← Back to Matrix
      </button>

      {/* Toast */}
      {toast && (
        <div className="mb-4 px-4 py-2 text-sm font-mono"
          style={{ background: toast.ok ? '#052e16' : '#1f0808', border: `1px solid ${toast.ok ? '#14532d' : '#7f1d1d'}`, color: toast.ok ? '#22c55e' : '#ef4444' }}>
          {toast.msg}
        </div>
      )}

      {/* Header */}
      <div className="card p-5 mb-4">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <span className="font-mono text-2xl font-bold" style={{ color: '#1e6fd4' }}>{prospect.ticker}</span>
              <button
                onClick={toggleWatchlist}
                title={isWatchlisted ? 'Remove from watchlist' : 'Add to watchlist'}
                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 2px', lineHeight: 1 }}
              >
                <span style={{ fontSize: 22, color: isWatchlisted ? '#D4AF37' : '#555', transition: 'color 0.15s' }}>
                  {isWatchlisted ? '★' : '☆'}
                </span>
              </button>
              <StatusBadge status={prospect.status} />
            </div>
            <div className="text-lg font-semibold mb-1" style={{ color: '#e2e8f0' }}>{prospect.company_name}</div>
            <div className="font-mono text-xs" style={{ color: '#8fa3bf' }}>
              {prospect.gics_sector} · {prospect.gics_industry_group}
              {prospect.market_cap_aud && <span> · {fmtMarketCap(prospect.market_cap_aud)} AUD</span>}
              {prospect.website && (
                <a href={prospect.website.startsWith('http') ? prospect.website : `https://${prospect.website}`}
                  target="_blank" rel="noopener noreferrer"
                  style={{ color: '#1e6fd4', marginLeft: 8 }}>
                  {prospect.website} ↗
                </a>
              )}
            </div>
          </div>
          <div className="text-right">
            <div className="font-mono text-xs mb-1" style={{ color: '#4a5a70' }}>PROSPECT SCORE</div>
            <div className="font-mono text-3xl font-bold" style={{ color: prospect.prospect_score >= 15 ? '#22c55e' : prospect.prospect_score >= 8 ? '#eab308' : '#3b82f6' }}>
              {prospect.prospect_score ? Number(prospect.prospect_score).toFixed(1) : '—'}
            </div>
            {prospect.likelihood_score && (
              <div className="font-mono text-xs mt-1" style={{ color: '#8fa3bf' }}>
                likelihood {prospect.likelihood_score}/10
              </div>
            )}
            {hasAISignals && (
              <div className="font-mono text-xs mt-1" style={{ color: GOLD }}>◆ AI Enhanced</div>
            )}
            <button onClick={copyBrief}
              className="font-mono text-xs px-3 py-1.5 mt-2"
              style={{ background: 'none', border: '1px solid #1e2530', color: '#8fa3bf', cursor: 'pointer' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = '#2d3a4d'; e.currentTarget.style.color = '#e2e8f0' }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = '#1e2530'; e.currentTarget.style.color = '#8fa3bf' }}
            >
              ⧉ Copy Brief
            </button>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
        {/* Strategic Profile */}
        {(prospect.strategic_direction || prospect.primary_tailwind || prospect.primary_headwind) && (
          <div className="card p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="font-mono text-xs uppercase tracking-widest" style={{ color: '#4a5a70' }}>
                Strategic Profile
              </div>
              {hasAISignals && (
                <span className="font-mono text-xs px-1.5 py-0.5"
                  style={{ background: GOLD_BG, border: `1px solid ${GOLD_BORDER}`, color: GOLD, fontSize: 9 }}>
                  AI-ENHANCED
                </span>
              )}
            </div>
            {prospect.strategic_direction && (
              <div className="mb-3">
                <div className="font-mono text-xs mb-1" style={{ color: '#4a5a70' }}>DIRECTION</div>
                <div className="text-sm" style={{ color: '#e2e8f0' }}>{prospect.strategic_direction}</div>
              </div>
            )}
            {prospect.primary_tailwind && (
              <div className="mb-3">
                <div className="font-mono text-xs mb-1" style={{ color: '#22c55e' }}>↑ TAILWIND</div>
                <div className="text-sm" style={{ color: '#e2e8f0' }}>{prospect.primary_tailwind}</div>
              </div>
            )}
            {prospect.primary_headwind && (
              <div>
                <div className="font-mono text-xs mb-1" style={{ color: '#ef4444' }}>↓ HEADWIND</div>
                <div className="text-sm" style={{ color: '#e2e8f0' }}>{prospect.primary_headwind}</div>
              </div>
            )}
            {deepResult?.profile?.likelihood_reasoning && (
              <div className="mt-3 pt-3" style={{ borderTop: `1px solid ${GOLD_BORDER}` }}>
                <div className="font-mono text-xs mb-1" style={{ color: GOLD }}>◆ AI LIKELIHOOD REASONING</div>
                <div className="text-xs" style={{ color: '#8fa3bf', lineHeight: 1.5 }}>
                  {deepResult.profile.likelihood_reasoning}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="card p-4">
          <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: '#4a5a70' }}>
            Actions
          </div>
          <div className="flex flex-wrap gap-2 mb-3">
            {STATUS_ACTIONS.map(a => (
              <button key={a.value} onClick={() => updateStatus(a.value)}
                className="font-mono text-xs px-3 py-1.5 transition-all"
                style={{
                  background: prospect.status === a.value ? '#1e6fd4' : 'none',
                  border: '1px solid',
                  borderColor: prospect.status === a.value ? '#1e6fd4' : '#1e2530',
                  color: prospect.status === a.value ? '#fff' : '#8fa3bf',
                  cursor: 'pointer',
                }}>
                {a.label}
              </button>
            ))}
          </div>

          <div className="flex gap-2">
            {/* Rule-based enrich */}
            <button onClick={triggerEnrich} disabled={enriching}
              className="font-mono text-xs px-3 py-2 flex-1"
              style={{
                background: enriching ? '#1e2530' : '#1558a8',
                color: enriching ? '#4a5a70' : '#e2e8f0',
                border: '1px solid #1e3a5f',
                cursor: enriching ? 'not-allowed' : 'pointer',
              }}>
              {enriching ? 'Enriching...' : '⟳ Enrich'}
            </button>

            {/* Deep Analysis */}
            {deepAvailable ? (
              <button onClick={triggerDeepAnalysis} disabled={deepAnalysing}
                className="font-mono text-xs px-3 py-2 flex-1"
                style={{
                  background: deepAnalysing ? '#1e2530' : GOLD_BG,
                  color: deepAnalysing ? '#4a5a70' : GOLD,
                  border: `1px solid ${deepAnalysing ? '#1e2530' : GOLD_BORDER}`,
                  cursor: deepAnalysing ? 'not-allowed' : 'pointer',
                  fontWeight: 600,
                }}>
                {deepAnalysing ? '◆ Analysing...' : '◆ Deep Analysis'}
              </button>
            ) : (
              <button
                disabled
                title="Configure Anthropic API key in Settings to enable Deep Analysis"
                className="font-mono text-xs px-3 py-2 flex-1"
                style={{
                  background: 'none',
                  color: '#2d3a4d',
                  border: '1px solid #1e2530',
                  cursor: 'not-allowed',
                }}>
                ◆ Deep Analysis
              </button>
            )}
          </div>

          {lastDeepAt && (
            <div className="font-mono text-xs mt-2" style={{ color: GOLD_BORDER }}>
              Last AI analysis: {new Date(lastDeepAt).toLocaleDateString()}
            </div>
          )}

          {deepJob?.status === 'running' && (
            <div className="mt-3 p-3" style={{ background: '#0d1017', border: `1px solid ${GOLD_BORDER}` }}>
              <div className="flex items-center justify-between gap-3 mb-2">
                <div className="font-mono text-xs" style={{ color: GOLD }}>
                  ◆ {deepJob.message || 'Running deep analysis'}
                </div>
                <div className="font-mono text-xs" style={{ color: '#8fa3bf' }}>
                  {deepJob.progress_pct || 0}%
                </div>
              </div>
              <div style={{ height: 8, background: '#1e2530', overflow: 'hidden' }}>
                <div style={{
                  width: `${deepJob.progress_pct || 0}%`,
                  height: '100%',
                  background: 'linear-gradient(90deg, #8B7120 0%, #D4AF37 100%)',
                  transition: 'width 0.3s ease',
                }} />
              </div>
              <div className="font-mono text-xs mt-2" style={{ color: '#8fa3bf' }}>
                {deepJob.stage === 'collecting_documents'
                  ? 'Pulling and cleaning source documents'
                  : deepJob.stage === 'running_ai'
                  ? 'AI is reading the evidence pack'
                  : deepJob.stage === 'saving_results'
                  ? 'Saving the new findings'
                  : deepJob.message || 'Working...'}
              </div>
            </div>
          )}

          {!deepAvailable && (
            <div className="font-mono text-xs mt-2" style={{ color: '#2d3a4d' }}>
              Configure API key in{' '}
              <a href="/settings" style={{ color: '#4a5a70', textDecoration: 'underline' }}>Settings</a>
              {' '}to enable
            </div>
          )}
        </div>
      </div>

      {/* Pressure Signals */}
      <div className="card mb-4">
        <div className="px-4 py-3 flex items-center gap-3" style={{ borderBottom: '1px solid #1e2530' }}>
          <span className="font-mono text-xs uppercase tracking-widest" style={{ color: '#4a5a70' }}>
            Pressure Signals ({signals.length})
          </span>
          {hasAISignals && (
            <span className="font-mono text-xs" style={{ color: GOLD }}>◆ includes AI signals</span>
          )}
        </div>
        {signals.length === 0 ? (
          <div className="px-4 py-6 font-mono text-xs text-center" style={{ color: '#4a5a70' }}>
            No signals detected — run enrichment to analyse ASX announcements
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr style={{ borderBottom: '1px solid #1e2530' }}>
                {['Type', 'Strength', 'Summary', 'Source', 'Confidence', 'Date'].map(h => (
                  <th key={h} className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {signals.map(s => {
                const disputed = s.validated_by === 'claude-deep-v1' && s.is_valid === false
                return (
                  <tr key={s.id}
                    style={{
                      borderBottom: '1px solid #1e2530',
                      opacity: disputed ? 0.45 : 1,
                    }}>
                    <td className="px-4 py-2.5"><PressureBadge type={s.pressure_type} /></td>
                    <td className="px-4 py-2.5"><StrengthBadge strength={s.strength} /></td>
                    <td className="px-4 py-2.5 text-sm" style={{ color: disputed ? '#6b7280' : '#e2e8f0', maxWidth: 280 }}>
                      <span style={{ textDecoration: disputed ? 'line-through' : 'none' }}>
                        {s.summary}
                      </span>
                      {s.source_title && (
                        <div className="font-mono text-xs mt-0.5 truncate" style={{ color: '#4a5a70' }}>
                          {s.source_title}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <SourceBadge signal={s} />
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#8fa3bf' }}>
                      {s.confidence_score ? `${Math.round(s.confidence_score * 100)}%` : '—'}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#4a5a70' }}>
                      {s.source_date || '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Analyst Notes */}
      <div className="card p-4">
        <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: '#4a5a70' }}>
          Analyst Notes
        </div>
        <textarea
          value={notes}
          onChange={e => setNotes(e.target.value)}
          placeholder="Add intelligence, context, network paths..."
          rows={4}
          className="w-full p-3 text-sm"
          style={{ resize: 'vertical', background: '#0d1017', border: '1px solid #1e2530', color: '#e2e8f0' }}
        />
        <div className="flex justify-end mt-2">
          <button onClick={saveNotes} disabled={saving}
            className="font-mono text-xs px-4 py-2"
            style={{
              background: saving ? '#1e2530' : '#1e6fd4',
              color: saving ? '#4a5a70' : '#fff',
              border: 'none',
              cursor: saving ? 'not-allowed' : 'pointer',
            }}>
            {saving ? 'Saving...' : 'Save Notes'}
          </button>
        </div>
      </div>
    </div>
  )
}
