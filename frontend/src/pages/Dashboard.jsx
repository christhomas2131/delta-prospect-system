import { useCallback, useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'

function StatCard({ label, value, sub, color }) {
  return (
    <div className="card p-4 h-full" style={{ borderLeft: `3px solid ${color || 'var(--accent)'}`, minHeight: 96 }}>
      <div className="text-xs font-mono uppercase tracking-widest mb-1" style={{ color: 'var(--text-muted)' }}>{label}</div>
      <div className="text-3xl font-mono font-semibold" style={{ color: color || 'var(--text-primary)', fontVariantNumeric: 'tabular-nums', lineHeight: 1.1 }}>{value ?? '-'}</div>
      {sub && <div className="text-xs mt-1" style={{ color: 'var(--text-secondary)' }}>{sub}</div>}
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

function formatDateTime(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleDateString('en-AU', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function refreshFailureMessage(refresh) {
  if (refresh?.display_error) return refresh.display_error
  const raw = String(refresh?.error_message || '').toLowerCase()
  if (raw.includes('404') || raw.includes('not found')) {
    return 'The ASX listings feed could not be found. Existing listing data was not changed.'
  }
  if (raw.includes('timeout') || raw.includes('timed out')) {
    return 'ASX did not respond in time. Existing listing data was not changed.'
  }
  return 'The ASX refresh could not complete. Current dashboard data remains available.'
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
  const [topLeads, setTopLeads] = useState([])
  const navigate = useNavigate()
  const intervalRef = useRef(null)
  const enrichPollRef = useRef(null)
  const refreshPollRef = useRef(null)
  const dataAsOf = formatDateTime(stats?.last_refresh)
  const operationsBusy = refreshing || enriching

  const loadData = useCallback(async (silent = false) => {
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
      fetch('/api/prospects?limit=5&sort_by=prospect_score&sort_dir=desc')
        .then(r => r.ok ? r.json() : null)
        .then(d => setTopLeads(d?.data || []))
        .catch(() => {})
    } catch {
      setError('Cannot reach the API - is the backend running?')
    }
    setLoading(false)
  }, [])

  const loadLastRefresh = useCallback(() => {
    fetch('/api/refresh/latest').then(r => r.json()).then(d => {
      if (d && d.started_at) setLastRefresh(d)
    }).catch(() => {})
  }, [])

  const startRefreshPolling = useCallback(() => {
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
  }, [loadData, loadLastRefresh])

  const startEnrichPolling = useCallback(() => {
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
  }, [loadData])

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
  }, [loadData, loadLastRefresh, startEnrichPolling, startRefreshPolling])

  const handleRefresh = async () => {
    if (operationsBusy) return
    setRefreshing(true)
    try {
      const r = await fetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ triggered_by: 'dashboard' }) })
      const d = await r.json()
      if (!r.ok) throw new Error(d.detail || d.message || 'Refresh request failed')
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
    if (operationsBusy) return
    setEnriching(true)
    try {
      const r = await fetch('/api/enrich/batch', { method: 'POST' })
      const d = await r.json()
      if (!r.ok) throw new Error(d.detail || d.message || 'Enrichment request failed')
      setToast({ ok: true, msg: d.message || 'Batch enrichment started' })
      setTimeout(() => setToast(null), 5000)
      startEnrichPolling()
    } catch {
      setToast({ ok: false, msg: 'Batch enrichment failed - check API' })
      setEnriching(false)
      setTimeout(() => setToast(null), 5000)
    }
  }

  const handleEnrichTopLeads = async () => {
    if (operationsBusy) return
    setEnriching(true)
    try {
      const r = await fetch('/api/enrich/batch?min_score=8&exclude_dq=true', { method: 'POST' })
      const d = await r.json()
      if (!r.ok) throw new Error(d.detail || d.message || 'Enrichment request failed')
      setToast({ ok: true, msg: d.message || 'Top leads enrichment started' })
      setTimeout(() => setToast(null), 5000)
      startEnrichPolling()
    } catch {
      setToast({ ok: false, msg: 'Enrichment failed - check API' })
      setEnriching(false)
      setTimeout(() => setToast(null), 5000)
    }
  }

  if (error && !stats) {
    return (
      <div className="p-6">
        <div className="card p-6 text-center" style={{ borderLeft: '3px solid var(--risk)' }}>
          <div className="font-mono text-sm mb-2" style={{ color: 'var(--risk)' }}>Connection Error</div>
          <div className="text-sm mb-4" style={{ color: 'var(--text-secondary)' }}>{error}</div>
          <button onClick={() => loadData()} className="font-mono text-xs px-4 py-2"
            style={{ background: 'var(--accent)', color: 'var(--on-accent)', border: 'none', cursor: 'pointer' }}>
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (loading && !stats) {
    return (
      <div className="p-6">
        <div className="card p-6 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>Loading dashboard<span className="animate-pulse">...</span></div>
      </div>
    )
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6 flex-wrap gap-3">
        <div>
          <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: 'var(--text-muted)' }}>
            DELTA PROSPECT SYSTEM
          </div>
          <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-primary)', margin: 0 }}>
            Intelligence Dashboard
          </h1>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          {dataAsOf && (
            <span className="font-mono text-xs" style={{ color: 'var(--text-muted)' }} title="Most recent successful ASX refresh">
              Data synced: {dataAsOf}
            </span>
          )}
          <button
            onClick={handleEnrichTopLeads}
            disabled={operationsBusy}
            title="Enrich scored prospects only"
            className="font-mono text-xs px-4 py-2 transition-all"
            style={{
              background: operationsBusy ? 'var(--border)' : 'var(--gold-bg)',
              color: operationsBusy ? 'var(--text-muted)' : 'var(--gold)',
              border: '1px solid',
              borderColor: operationsBusy ? 'var(--border)' : 'var(--gold-border)',
              cursor: operationsBusy ? 'not-allowed' : 'pointer',
              fontWeight: 600,
            }}
          >
            {enriching && enrichProgress ? (enrichProgress.ai_running ? `AI ${enrichProgress.ai_current}/${enrichProgress.ai_total}` : `ENRICHING ${enrichProgress.current}/${enrichProgress.total}`) : 'ENRICH TOP LEADS'}
          </button>
          <button
            onClick={handleBatchEnrich}
            disabled={operationsBusy}
            title="Enrich every prospect. This can take about 20 minutes."
            className="font-mono text-xs px-4 py-2 transition-all"
            style={{
              background: operationsBusy ? 'var(--border)' : 'var(--positive-border)',
              color: operationsBusy ? 'var(--text-muted)' : 'var(--positive)',
              border: '1px solid',
              borderColor: operationsBusy ? 'var(--border)' : 'var(--positive-border)',
              cursor: operationsBusy ? 'not-allowed' : 'pointer',
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
            disabled={operationsBusy}
            title="Sync the latest ASX company directory"
            className="font-mono text-xs px-4 py-2 transition-all"
            style={{
              background: operationsBusy ? 'var(--border)' : 'var(--accent)',
              color: operationsBusy ? 'var(--text-muted)' : 'var(--on-accent)',
              border: 'none',
              cursor: operationsBusy ? 'not-allowed' : 'pointer',
            }}
          >
            {refreshing && refreshProgress ? 'REFRESHING...' : refreshing ? 'REFRESHING...' : 'REFRESH ASX DATA'}
          </button>
        </div>
      </div>

      {toast && (
        <div role="status" aria-live="polite" className="mb-4 px-4 py-2 text-sm font-mono flex items-start justify-between gap-4" style={{ background: toast.ok ? 'var(--positive-bg)' : 'var(--risk-bg)', border: `1px solid ${toast.ok ? 'var(--positive-border)' : 'var(--risk-border)'}`, color: toast.ok ? 'var(--positive)' : 'var(--risk)' }}>
          <span>{toast.msg}</span>
          <button onClick={() => setToast(null)} aria-label="Dismiss notification" style={{ color: 'inherit', background: 'none', border: 'none', cursor: 'pointer', lineHeight: 1 }}>
            x
          </button>
        </div>
      )}

      {enrichProgress && (
        <div className="mb-4 px-4 py-3 font-mono text-xs" style={{ background: 'var(--card)', border: '1px solid var(--positive-border)' }}>
          {!enrichProgress.ai_running ? (
            <div className="flex items-center justify-between mb-2">
              <span style={{ color: 'var(--positive)' }}>
                Enriching {enrichProgress.current} of {enrichProgress.total} - {enrichProgress.ticker}
              </span>
              <span style={{ color: 'var(--text-muted)' }}>
                {enrichProgress.ok} done · {enrichProgress.skip} skipped · {enrichProgress.fail} failed
              </span>
            </div>
          ) : (
            <div className="flex items-center justify-between mb-2">
              <span style={{ color: 'var(--gold)' }}>
                AI deep analysis {enrichProgress.ai_current} of {enrichProgress.ai_total} - {enrichProgress.ai_ticker}
              </span>
              <span style={{ color: 'var(--gold-dim)' }}>
                {enrichProgress.ai_ok || 0} succeeded · {enrichProgress.ai_fail || 0} failed
              </span>
            </div>
          )}
          {enrichProgress.ai_message && (
            <div className="mb-2" style={{ color: enrichProgress.ai_fail > 0 ? 'var(--risk)' : 'var(--text-secondary)' }}>
              {enrichProgress.ai_message}
            </div>
          )}
          <div style={{ width: '100%', height: 4, background: 'var(--border)' }}>
            <div style={{
              width: `${
                enrichProgress.ai_running
                  ? (enrichProgress.ai_total > 0 ? (enrichProgress.ai_current / enrichProgress.ai_total * 100) : 0)
                  : (enrichProgress.total > 0 ? (enrichProgress.current / enrichProgress.total * 100) : 0)
              }%`,
              height: '100%',
              background: enrichProgress.ai_running ? 'var(--gold)' : 'var(--positive)',
              transition: 'width 0.3s',
            }} />
          </div>
        </div>
      )}

      {refreshProgress && (
        <div role="status" aria-live="polite" className="mb-4 px-4 py-3 font-mono text-xs" style={{
          background: refreshProgress.phase === 'Failed' ? 'var(--caution-bg)' : 'var(--card)',
          border: `1px solid ${refreshProgress.phase === 'Failed' ? 'var(--caution-border)' : 'var(--accent-border)'}`,
        }}>
          <div className="flex items-center gap-3">
            <span style={{ color: refreshProgress.phase === 'Failed' ? 'var(--caution)' : 'var(--info)' }}>{refreshProgress.phase}</span>
            {refreshProgress.detail && (
              <span style={{ color: 'var(--text-muted)' }}>{refreshProgress.detail}</span>
            )}
          </div>
          {refreshProgress.total > 0 && (
            <div className="mt-3">
              <div className="mb-2 flex items-center justify-between" style={{ color: 'var(--accent-light)' }}>
                <span>{refreshProgress.current} / {refreshProgress.total} location profiles</span>
                {refreshProgress.ticker && <span>{refreshProgress.ticker}</span>}
              </div>
              <div style={{ height: '6px', background: 'var(--surface)', border: '1px solid var(--accent-border)' }}>
                <div
                  style={{
                    height: '100%',
                    width: `${Math.max(0, Math.min(100, (refreshProgress.current / refreshProgress.total) * 100))}%`,
                    background: 'var(--info)',
                    transition: 'width 0.2s ease',
                  }}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {error && stats && (
        <div className="mb-4 px-4 py-2 text-xs font-mono" style={{ background: 'var(--risk-bg)', border: '1px solid var(--risk-border)', color: 'var(--risk)' }}>
          API unreachable - showing last loaded data.{' '}
          <button onClick={() => loadData()} style={{ textDecoration: 'underline', background: 'none', border: 'none', color: 'var(--risk)', cursor: 'pointer' }}>Retry</button>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <StatCard label="Total Prospects" value={stats?.total_prospects?.toLocaleString()} color="var(--info)" />
        <StatCard label="Enriched" value={stats?.enriched} sub={`${stats?.ready_for_outreach || 0} ready for outreach`} color="var(--teal)" />
        <StatCard label="Signals Detected" value={stats?.total_signals?.toLocaleString()} sub={`${stats?.strong_signals || 0} strong`} color="var(--ops)" />
        <StatCard label="Avg Score" value={stats?.avg_score ? Number(stats.avg_score).toFixed(1) : '-'} color="var(--positive)" />
      </div>


      {lastRefresh && (
        <div className="mb-6 card px-4 py-3" style={{
          borderLeft: `3px solid ${lastRefresh.status === 'failed' ? 'var(--caution)' : 'var(--info)'}`,
          background: lastRefresh.status === 'failed' ? 'var(--caution-bg)' : 'var(--card)',
        }}>
          {lastRefresh.status === 'failed' ? (
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <div>
                <div className="font-mono text-xs uppercase tracking-widest mb-1" style={{ color: 'var(--caution)' }}>
                  Refresh needs attention
                </div>
                <div className="text-sm" style={{ color: 'var(--text-secondary)' }}>
                  {refreshFailureMessage(lastRefresh)}
                  {dataAsOf && <span> Last successful sync: {dataAsOf}.</span>}
                </div>
              </div>
              <button
                onClick={handleRefresh}
                disabled={operationsBusy}
                className="font-mono text-xs px-3 py-2"
                style={{
                  color: operationsBusy ? 'var(--text-muted)' : 'var(--caution)',
                  background: 'transparent',
                  border: '1px solid var(--caution-border)',
                  cursor: operationsBusy ? 'not-allowed' : 'pointer',
                }}
              >
                {refreshing ? 'RETRYING...' : 'RETRY REFRESH'}
              </button>
            </div>
          ) : (
            <div>
              <div className="font-mono text-xs uppercase tracking-widest mb-1" style={{ color: 'var(--text-muted)' }}>
                Latest ASX sync complete
              </div>
              <div className="text-sm" style={{ color: 'var(--text-secondary)' }}>
                {lastRefresh.total_listings?.toLocaleString() || '?'} listings found, {lastRefresh.target_sector_count?.toLocaleString() || '?'} in target sectors
                {lastRefresh.new_listings > 0 && <span style={{ color: 'var(--positive)' }}> - {lastRefresh.new_listings} added</span>}
                {lastRefresh.delisted_count > 0 && <span style={{ color: 'var(--caution)' }}> - {lastRefresh.delisted_count} removed</span>}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
        <div className="card p-4 cursor-pointer h-full" style={{ borderLeft: '3px solid var(--gold)', minHeight: 96 }} onClick={() => navigate('/watchlist')} title="Open watchlist">
          <div className="text-xs font-mono uppercase tracking-widest mb-1" style={{ color: 'var(--gold-dim)', letterSpacing: '0.08em' }}>Watchlist</div>
          <div className="text-3xl font-mono font-semibold" style={{ color: 'var(--gold)', fontVariantNumeric: 'tabular-nums', lineHeight: 1.1 }}>{stats?.watchlist_count ?? '-'}</div>
          <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>starred</div>
        </div>
        <StatCard label="ASX Listings" value={stats?.total_listings?.toLocaleString()} color="var(--text-secondary)" />
        <StatCard label="Target Sector" value={stats?.target_sector_count?.toLocaleString()} color="var(--text-secondary)" />
        <StatCard label="Unscreened" value={stats?.unscreened?.toLocaleString()} color="var(--text-muted)" />
        <StatCard label="Disqualified" value={stats?.disqualified || 0} color="var(--text-muted)" />
      </div>

      {/* Priority Leads */}
      <div className="card mb-6">
        <div className="px-4 py-3 flex items-center justify-between" style={{ borderBottom: '1px solid var(--border)' }}>
          <span className="font-mono text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Priority Leads</span>
          <button onClick={() => navigate('/leads')} className="font-mono text-xs" style={{ color: 'var(--accent)', background: 'none', border: 'none', cursor: 'pointer' }}>{'View all ->'}</button>
        </div>
        {topLeads.length === 0 ? (
          <div className="px-4 py-6 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>No scored prospects yet. Run enrichment to rank leads.</div>
        ) : (
          <div className="table-scroll">
            <table className="w-full sticky-head" style={{ minWidth: 520 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)' }}>
                  {['Ticker','Company','Sector','Score','Signals'].map(h => <th key={h} className="px-4 py-2 text-left font-mono text-xs uppercase" style={{ color: 'var(--text-muted)' }}>{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {topLeads.map(p => {
                  const score = p.prospect_score ? Number(p.prospect_score) : 0
                  const sc = score >= 15 ? 'var(--positive)' : score >= 8 ? 'var(--caution)' : 'var(--accent)'
                  return (
                    <tr
                      key={p.prospect_id}
                      className="table-row-hover"
                      style={{ borderBottom: '1px solid var(--border)' }}
                      onClick={() => navigate(`/deep-intelligence/${p.prospect_id}`)}
                      onKeyDown={event => event.key === 'Enter' && navigate(`/deep-intelligence/${p.prospect_id}`)}
                      tabIndex={0}
                      role="link"
                    >
                      <td className="px-4 py-2.5 font-mono text-sm font-semibold" style={{ color: 'var(--accent)' }}>{p.ticker}</td>
                      <td className="px-4 py-2.5 text-sm" style={{ color: 'var(--text-primary)', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.company_name}</td>
                      <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{p.gics_sector}</td>
                      <td className="px-4 py-2.5 font-mono text-sm font-semibold" style={{ color: sc }}>{score ? score.toFixed(1) : '\u2014'}</td>
                      <td className="px-4 py-2.5 font-mono text-xs" style={{ color: p.total_signals > 0 ? 'var(--text-primary)' : 'var(--text-muted)' }}>{p.total_signals ?? 0}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card mb-6">
        <div className="px-4 py-3 flex items-center justify-between" style={{ borderBottom: '1px solid var(--border)' }}>
          <span className="font-mono text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
            Sector Breakdown
          </span>
          <button
            onClick={() => navigate('/leads')}
            className="font-mono text-xs"
            style={{ color: 'var(--accent)', background: 'none', border: 'none', cursor: 'pointer' }}
          >
            {'View Leads ->'}
          </button>
        </div>
        <div className="table-scroll">
          <table className="w-full sticky-head" style={{ minWidth: 600 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {['Sector', 'Industry', 'Companies', 'In Matrix', 'Enriched', 'Avg Score'].map(h => (
                  <th key={h} className="px-4 py-2 text-left font-mono text-xs uppercase" style={{ color: 'var(--text-muted)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sectors.map((s, i) => (
                <tr key={i} className="table-row-hover" style={{ borderBottom: '1px solid var(--border)' }}
                  onClick={() => navigate(`/leads?sector=${encodeURIComponent(s.gics_sector)}`)}
                  onKeyDown={event => event.key === 'Enter' && navigate(`/leads?sector=${encodeURIComponent(s.gics_sector)}`)}
                  tabIndex={0}
                  role="link">
                  <td className="px-4 py-2.5 font-mono text-xs font-semibold" style={{ color: 'var(--text-primary)' }}>{s.gics_sector}</td>
                  <td className="px-4 py-2.5 text-xs" style={{ color: 'var(--text-secondary)' }}>{s.gics_industry_group}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--text-primary)' }}>{s.total_companies}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>{s.in_matrix}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--teal)' }}>{s.enriched}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: s.avg_score ? 'var(--positive)' : 'var(--text-muted)' }}>
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
