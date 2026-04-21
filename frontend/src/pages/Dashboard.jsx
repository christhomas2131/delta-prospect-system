import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'

function StatCard({ label, value, sub, color }) {
  return (
    <div className="card p-4" style={{ borderLeft: `3px solid ${color || '#1e6fd4'}` }}>
      <div className="text-xs font-mono uppercase tracking-widest mb-1" style={{ color: '#4a5a70' }}>{label}</div>
      <div className="text-3xl font-mono font-semibold" style={{ color: color || '#e2e8f0' }}>{value ?? '-'}</div>
      {sub && <div className="text-xs mt-1" style={{ color: '#8fa3bf' }}>{sub}</div>}
    </div>
  )
}

function formatAiOutcome(progress) {
  if (!progress) return ''
  const basis = progress.ai_selection_basis === 'prospect_score' ? 'score' : 'signal count'
  if (progress.ai_status === 'skipped_no_api_key') return ' AI skipped: no Anthropic API key configured.'
  if (progress.ai_status === 'skipped_no_candidates') return ' AI skipped: no scored prospects with signals were available.'
  if ((progress.ai_total || 0) === 0) return ''

  const summary = ` AI reviewed ${progress.ai_total} top prospects by ${basis}: ${progress.ai_ok || 0} succeeded, ${progress.ai_fail || 0} failed.`
  if ((progress.ai_fail || 0) > 0 && progress.ai_message) return `${summary} ${progress.ai_message}`
  return summary
}

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [sectors, setSectors] = useState([])
  const [refreshing, setRefreshing] = useState(false)
  const [refreshProgress, setRefreshProgress] = useState(null)
  const [lastRefresh, setLastRefresh] = useState(null)
  const [enriching, setEnriching] = useState(false)
  const [enrichProgress, setEnrichProgress] = useState(null)
  const [toast, setToast] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const navigate = useNavigate()
  const intervalRef = useRef(null)
  const enrichPollRef = useRef(null)
  const refreshPollRef = useRef(null)

  const loadData = async (silent = false) => {
    if (!silent) setLoading(true)
    setError(null)
    try {
      const [statsRes, sectorsRes] = await Promise.all([
        fetch('/api/stats'),
        fetch('/api/sectors'),
      ])
      if (!statsRes.ok || !sectorsRes.ok) throw new Error('API returned an error')
      setStats(await statsRes.json())
      setSectors(await sectorsRes.json())
    } catch {
      setError('Cannot reach the API - is the backend running?')
    }
    setLoading(false)
  }

  const loadLastRefresh = () => {
    fetch('/api/refresh/latest').then(r => r.json()).then(d => {
      if (d && d.started_at) setLastRefresh(d)
    }).catch(() => {})
  }

  const startRefreshPolling = () => {
    if (refreshPollRef.current) return
    refreshPollRef.current = setInterval(async () => {
      try {
        const r = await fetch('/api/refresh/status')
        const d = await r.json()
        setRefreshProgress(d)
        if (!d.running) {
          clearInterval(refreshPollRef.current)
          refreshPollRef.current = null
          setRefreshing(false)
          if (d.phase === 'Failed') {
            setToast({ ok: false, msg: `Refresh failed - ${d.detail}` })
          } else {
            setToast({ ok: true, msg: `ASX refresh complete - ${d.detail}` })
          }
          setTimeout(() => setToast(null), 10000)
          setTimeout(() => setRefreshProgress(null), 10000)
          loadData(true)
          loadLastRefresh()
        }
      } catch { /* ignore */ }
    }, 2000)
  }

  const startEnrichPolling = () => {
    if (enrichPollRef.current) return
    enrichPollRef.current = setInterval(async () => {
      try {
        const r = await fetch('/api/enrich/status')
        const d = await r.json()
        setEnrichProgress(d)
        if (!d.running) {
          clearInterval(enrichPollRef.current)
          enrichPollRef.current = null
          setEnriching(false)
          const aiOutcome = formatAiOutcome(d)
          setToast({
            ok: d.fail === 0 && (d.ai_fail || 0) === 0,
            msg: `Enrichment complete - ${d.ok} enriched, ${d.skip} skipped, ${d.fail} failed.${aiOutcome}`,
          })
          setTimeout(() => setToast(null), 10000)
          setTimeout(() => setEnrichProgress(null), 10000)
          loadData(true)
        }
      } catch { /* ignore */ }
    }, 3000)
  }

  useEffect(() => {
    loadData()
    loadLastRefresh()
    fetch('/api/enrich/status').then(r => r.json()).then(d => {
      if (d.running) { setEnriching(true); setEnrichProgress(d); startEnrichPolling() }
    }).catch(() => {})
    fetch('/api/refresh/status').then(r => r.json()).then(d => {
      if (d.running) { setRefreshing(true); setRefreshProgress(d); startRefreshPolling() }
    }).catch(() => {})
    intervalRef.current = setInterval(() => loadData(true), 60000)
    return () => {
      clearInterval(intervalRef.current)
      if (enrichPollRef.current) clearInterval(enrichPollRef.current)
      if (refreshPollRef.current) clearInterval(refreshPollRef.current)
    }
  }, [])

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const r = await fetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ triggered_by: 'dashboard' }) })
      const d = await r.json()
      setToast({ ok: true, msg: d.message || 'Refresh started' })
      setTimeout(() => setToast(null), 5000)
      startRefreshPolling()
    } catch {
      setToast({ ok: false, msg: 'Refresh failed - check API' })
      setRefreshing(false)
      setTimeout(() => setToast(null), 5000)
    }
  }

  const handleBatchEnrich = async () => {
    setEnriching(true)
    try {
      const r = await fetch('/api/enrich/batch', { method: 'POST' })
      const d = await r.json()
      setToast({ ok: true, msg: d.message || 'Batch enrichment started' })
      setTimeout(() => setToast(null), 5000)
      startEnrichPolling()
    } catch {
      setToast({ ok: false, msg: 'Batch enrichment failed - check API' })
      setEnriching(false)
      setTimeout(() => setToast(null), 5000)
    }
  }

  if (error && !stats) {
    return (
      <div className="p-6">
        <div className="card p-6 text-center" style={{ borderLeft: '3px solid #ef4444' }}>
          <div className="font-mono text-sm mb-2" style={{ color: '#ef4444' }}>Connection Error</div>
          <div className="text-sm mb-4" style={{ color: '#8fa3bf' }}>{error}</div>
          <button onClick={() => loadData()} className="font-mono text-xs px-4 py-2"
            style={{ background: '#1e6fd4', color: '#fff', border: 'none', cursor: 'pointer' }}>
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (loading && !stats) {
    return (
      <div className="p-6">
        <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>Loading dashboard...</div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6 flex-wrap gap-3">
        <div>
          <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: '#4a5a70' }}>
            DELTA PROSPECT SYSTEM
          </div>
          <h1 className="text-2xl font-semibold" style={{ color: '#e2e8f0', margin: 0 }}>
            Intelligence Dashboard
          </h1>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          {lastRefresh && (
            <span className="font-mono text-xs" style={{ color: lastRefresh.status === 'failed' ? '#ef4444' : '#4a5a70' }}>
              Last refresh: {new Date(lastRefresh.started_at).toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
          <button
            onClick={handleBatchEnrich}
            disabled={enriching}
            className="font-mono text-xs px-4 py-2 transition-all"
            style={{
              background: enriching ? '#1e2530' : '#14532d',
              color: enriching ? '#4a5a70' : '#22c55e',
              border: '1px solid',
              borderColor: enriching ? '#1e2530' : '#166534',
              cursor: enriching ? 'not-allowed' : 'pointer',
            }}
          >
            {enriching && enrichProgress?.ai_running
              ? `AI ${enrichProgress.ai_current}/${enrichProgress.ai_total}`
              : enriching && enrichProgress
                ? `ENRICHING ${enrichProgress.current}/${enrichProgress.total}`
                : enriching ? 'ENRICHING...' : 'ENRICH ALL'}
          </button>
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className="font-mono text-xs px-4 py-2 transition-all"
            style={{
              background: refreshing ? '#1e2530' : '#1e6fd4',
              color: refreshing ? '#4a5a70' : '#fff',
              border: 'none',
              cursor: refreshing ? 'not-allowed' : 'pointer',
            }}
          >
            {refreshing && refreshProgress ? 'REFRESHING...' : refreshing ? 'REFRESHING...' : 'REFRESH ASX DATA'}
          </button>
        </div>
      </div>

      {toast && (
        <div className="mb-4 px-4 py-2 text-sm font-mono" style={{ background: toast.ok ? '#052e16' : '#1f0808', border: `1px solid ${toast.ok ? '#14532d' : '#7f1d1d'}`, color: toast.ok ? '#22c55e' : '#ef4444' }}>
          {toast.msg}
        </div>
      )}

      {enrichProgress && (
        <div className="mb-4 px-4 py-3 font-mono text-xs" style={{ background: '#111418', border: '1px solid #14532d' }}>
          {!enrichProgress.ai_running ? (
            <div className="flex items-center justify-between mb-2">
              <span style={{ color: '#22c55e' }}>
                Enriching {enrichProgress.current} of {enrichProgress.total} - {enrichProgress.ticker}
              </span>
              <span style={{ color: '#4a5a70' }}>
                {enrichProgress.ok} done · {enrichProgress.skip} skipped · {enrichProgress.fail} failed
              </span>
            </div>
          ) : (
            <div className="flex items-center justify-between mb-2">
              <span style={{ color: '#D4AF37' }}>
                AI deep analysis {enrichProgress.ai_current} of {enrichProgress.ai_total} - {enrichProgress.ai_ticker}
              </span>
              <span style={{ color: '#8B7120' }}>
                {enrichProgress.ai_ok || 0} succeeded · {enrichProgress.ai_fail || 0} failed
              </span>
            </div>
          )}
          {enrichProgress.ai_message && (
            <div className="mb-2" style={{ color: enrichProgress.ai_fail > 0 ? '#ef4444' : '#8fa3bf' }}>
              {enrichProgress.ai_message}
            </div>
          )}
          <div style={{ width: '100%', height: 4, background: '#1e2530' }}>
            <div style={{
              width: `${
                enrichProgress.ai_running
                  ? (enrichProgress.ai_total > 0 ? (enrichProgress.ai_current / enrichProgress.ai_total * 100) : 0)
                  : (enrichProgress.total > 0 ? (enrichProgress.current / enrichProgress.total * 100) : 0)
              }%`,
              height: '100%',
              background: enrichProgress.ai_running ? '#D4AF37' : '#22c55e',
              transition: 'width 0.3s',
            }} />
          </div>
        </div>
      )}

      {refreshProgress && (
        <div className="mb-4 px-4 py-3 font-mono text-xs" style={{ background: '#111418', border: '1px solid #1e3a5f' }}>
          <div className="flex items-center gap-3">
            <span style={{ color: '#3b82f6' }}>{refreshProgress.phase}</span>
            {refreshProgress.detail && (
              <span style={{ color: '#4a5a70' }}>{refreshProgress.detail}</span>
            )}
          </div>
        </div>
      )}

      {error && stats && (
        <div className="mb-4 px-4 py-2 text-xs font-mono" style={{ background: '#1f0808', border: '1px solid #7f1d1d', color: '#ef4444' }}>
          API unreachable - showing last loaded data.{' '}
          <button onClick={() => loadData()} style={{ textDecoration: 'underline', background: 'none', border: 'none', color: '#ef4444', cursor: 'pointer' }}>Retry</button>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <StatCard label="Total Prospects" value={stats?.total_prospects?.toLocaleString()} color="#3b82f6" />
        <StatCard label="Enriched" value={stats?.enriched} sub={`${stats?.ready_for_outreach || 0} ready for outreach`} color="#14b8a6" />
        <StatCard label="Signals Detected" value={stats?.total_signals?.toLocaleString()} sub={`${stats?.strong_signals || 0} strong`} color="#f97316" />
        <StatCard label="Avg Score" value={stats?.avg_score ? Number(stats.avg_score).toFixed(1) : '-'} color="#22c55e" />
      </div>
      <div className="mb-6">
        <div
          className="card p-4 cursor-pointer"
          style={{ borderLeft: '3px solid #D4AF37', maxWidth: 220 }}
          onClick={() => navigate('/watchlist')}
        >
          <div className="text-xs font-mono uppercase tracking-widest mb-1" style={{ color: '#8B7120' }}>Watchlist</div>
          <div className="text-3xl font-mono font-semibold" style={{ color: '#D4AF37' }}>
            {stats?.watchlist_count ?? '-'}
          </div>
          <div className="text-xs mt-1" style={{ color: '#4a5a70' }}>starred companies</div>
        </div>
      </div>

      {lastRefresh && (
        <div className="mb-6 card px-4 py-3" style={{
          borderLeft: `3px solid ${lastRefresh.status === 'failed' ? '#ef4444' : '#3b82f6'}`,
        }}>
          <div className="font-mono text-xs uppercase tracking-widest mb-1" style={{ color: '#4a5a70' }}>
            Last ASX Refresh
          </div>
          {lastRefresh.status === 'failed' ? (
            <div className="text-sm" style={{ color: '#ef4444' }}>
              Failed - {lastRefresh.error_message || 'Unknown error'}
            </div>
          ) : (
            <div className="text-sm" style={{ color: '#8fa3bf' }}>
              {lastRefresh.total_listings?.toLocaleString() || '?'} listings found, {lastRefresh.target_sector_count?.toLocaleString() || '?'} target sector
              {lastRefresh.new_listings > 0 && <span style={{ color: '#22c55e' }}> - {lastRefresh.new_listings} new added</span>}
              {lastRefresh.delisted_count > 0 && <span style={{ color: '#eab308' }}> - {lastRefresh.delisted_count} removed</span>}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-8">
        <StatCard label="ASX Listings" value={stats?.total_listings?.toLocaleString()} color="#8fa3bf" />
        <StatCard label="Target Sector" value={stats?.target_sector_count?.toLocaleString()} color="#8fa3bf" />
        <StatCard label="Unscreened" value={stats?.unscreened?.toLocaleString()} color="#4a5a70" />
        <StatCard label="Disqualified" value={stats?.disqualified || 0} color="#4a5a70" />
      </div>

      <div className="card mb-6">
        <div className="px-4 py-3 flex items-center justify-between" style={{ borderBottom: '1px solid #1e2530' }}>
          <span className="font-mono text-xs uppercase tracking-widest" style={{ color: '#4a5a70' }}>
            Sector Breakdown
          </span>
          <button
            onClick={() => navigate('/leads')}
            className="font-mono text-xs"
            style={{ color: '#1e6fd4', background: 'none', border: 'none', cursor: 'pointer' }}
          >
            {'View Leads ->'}
          </button>
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="w-full" style={{ minWidth: 600 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #1e2530' }}>
                {['Sector', 'Industry', 'Companies', 'In Matrix', 'Enriched', 'Avg Score'].map(h => (
                  <th key={h} className="px-4 py-2 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sectors.map((s, i) => (
                <tr key={i} className="table-row-hover" style={{ borderBottom: '1px solid #1e2530' }}
                  onClick={() => navigate(`/leads?sector=${encodeURIComponent(s.gics_sector)}`)}>
                  <td className="px-4 py-2.5 font-mono text-xs font-semibold" style={{ color: '#e2e8f0' }}>{s.gics_sector}</td>
                  <td className="px-4 py-2.5 text-xs" style={{ color: '#8fa3bf' }}>{s.gics_industry_group}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#e2e8f0' }}>{s.total_companies}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#8fa3bf' }}>{s.in_matrix}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#14b8a6' }}>{s.enriched}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: s.avg_score ? '#22c55e' : '#4a5a70' }}>
                    {s.avg_score ? Number(s.avg_score).toFixed(1) : '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
