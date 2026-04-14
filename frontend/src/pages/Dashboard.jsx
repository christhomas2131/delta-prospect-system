import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'

function StatCard({ label, value, sub, color }) {
  return (
    <div className="card p-4" style={{ borderLeft: `3px solid ${color || '#1e6fd4'}` }}>
      <div className="text-xs font-mono uppercase tracking-widest mb-1" style={{ color: '#4a5a70' }}>{label}</div>
      <div className="text-3xl font-mono font-semibold" style={{ color: color || '#e2e8f0' }}>{value ?? '—'}</div>
      {sub && <div className="text-xs mt-1" style={{ color: '#8fa3bf' }}>{sub}</div>}
    </div>
  )
}

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [sectors, setSectors] = useState([])
  const [refreshing, setRefreshing] = useState(false)
  const [enriching, setEnriching] = useState(false)
  const [toast, setToast] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const navigate = useNavigate()
  const intervalRef = useRef(null)

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
      setError('Cannot reach the API — is the backend running?')
    }
    setLoading(false)
  }

  useEffect(() => {
    loadData()
    // Auto-refresh every 60 seconds
    intervalRef.current = setInterval(() => loadData(true), 60000)
    return () => clearInterval(intervalRef.current)
  }, [])

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const r = await fetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ triggered_by: 'dashboard' }) })
      const d = await r.json()
      setToast({ ok: true, msg: d.message || 'Refresh started' })
    } catch {
      setToast({ ok: false, msg: 'Refresh failed — check API' })
    }
    setRefreshing(false)
    setTimeout(() => setToast(null), 4000)
  }

  const handleBatchEnrich = async () => {
    setEnriching(true)
    try {
      const r = await fetch('/api/enrich/batch', { method: 'POST' })
      const d = await r.json()
      setToast({ ok: true, msg: d.message || 'Batch enrichment started' })
    } catch {
      setToast({ ok: false, msg: 'Batch enrichment failed — check API' })
    }
    setEnriching(false)
    setTimeout(() => setToast(null), 5000)
  }

  // Error state
  if (error && !stats) {
    return (
      <div className="p-6">
        <div className="card p-6 text-center" style={{ borderLeft: '3px solid #ef4444' }}>
          <div className="font-mono text-sm mb-2" style={{ color: '#ef4444' }}>⚠ Connection Error</div>
          <div className="text-sm mb-4" style={{ color: '#8fa3bf' }}>{error}</div>
          <button onClick={() => loadData()} className="font-mono text-xs px-4 py-2"
            style={{ background: '#1e6fd4', color: '#fff', border: 'none', cursor: 'pointer' }}>
            Retry
          </button>
        </div>
      </div>
    )
  }

  // Loading state
  if (loading && !stats) {
    return (
      <div className="p-6">
        <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>Loading dashboard...</div>
      </div>
    )
  }

  return (
    <div className="p-6">
      {/* Header */}
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
          {stats?.last_refresh && (
            <span className="font-mono text-xs" style={{ color: '#4a5a70' }}>
              Last refresh: {new Date(stats.last_refresh).toLocaleDateString()}
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
            {enriching ? 'ENRICHING...' : '⚡ ENRICH ALL'}
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
            {refreshing ? 'REFRESHING...' : '↻ REFRESH ASX DATA'}
          </button>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div className="mb-4 px-4 py-2 text-sm font-mono" style={{ background: toast.ok ? '#052e16' : '#1f0808', border: `1px solid ${toast.ok ? '#14532d' : '#7f1d1d'}`, color: toast.ok ? '#22c55e' : '#ef4444' }}>
          {toast.msg}
        </div>
      )}

      {/* Connection warning (shown when background poll fails but we have stale data) */}
      {error && stats && (
        <div className="mb-4 px-4 py-2 text-xs font-mono" style={{ background: '#1f0808', border: '1px solid #7f1d1d', color: '#ef4444' }}>
          ⚠ API unreachable — showing last loaded data.{' '}
          <button onClick={() => loadData()} style={{ textDecoration: 'underline', background: 'none', border: 'none', color: '#ef4444', cursor: 'pointer' }}>Retry</button>
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <StatCard label="Total Prospects" value={stats?.total_prospects?.toLocaleString()} color="#3b82f6" />
        <StatCard label="Enriched" value={stats?.enriched} sub={`${stats?.ready_for_outreach || 0} ready for outreach`} color="#14b8a6" />
        <StatCard label="Signals Detected" value={stats?.total_signals?.toLocaleString()} sub={`${stats?.strong_signals || 0} strong`} color="#f97316" />
        <StatCard label="Avg Score" value={stats?.avg_score ? Number(stats.avg_score).toFixed(1) : '—'} color="#22c55e" />
      </div>
      <div className="mb-6">
        <div
          className="card p-4 cursor-pointer"
          style={{ borderLeft: '3px solid #D4AF37', maxWidth: 220 }}
          onClick={() => navigate('/watchlist')}
        >
          <div className="text-xs font-mono uppercase tracking-widest mb-1" style={{ color: '#8B7120' }}>★ Watchlist</div>
          <div className="text-3xl font-mono font-semibold" style={{ color: '#D4AF37' }}>
            {stats?.watchlist_count ?? '—'}
          </div>
          <div className="text-xs mt-1" style={{ color: '#4a5a70' }}>starred companies</div>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-8">
        <StatCard label="ASX Listings" value={stats?.total_listings?.toLocaleString()} color="#8fa3bf" />
        <StatCard label="Target Sector" value={stats?.target_sector_count?.toLocaleString()} color="#8fa3bf" />
        <StatCard label="Unscreened" value={stats?.unscreened?.toLocaleString()} color="#4a5a70" />
        <StatCard label="Disqualified" value={stats?.disqualified || 0} color="#4a5a70" />
      </div>

      {/* Sector Table */}
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
            View Leads →
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
                    {s.avg_score ? Number(s.avg_score).toFixed(1) : '—'}
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
