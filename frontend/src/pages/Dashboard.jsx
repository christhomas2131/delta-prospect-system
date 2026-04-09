import { useEffect, useState } from 'react'
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
  const [toast, setToast] = useState(null)
  const navigate = useNavigate()

  useEffect(() => {
    fetch('/api/stats').then(r => r.json()).then(setStats)
    fetch('/api/sectors').then(r => r.json()).then(setSectors)
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

  return (
    <div className="p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: '#4a5a70' }}>
            DELTA PROSPECT SYSTEM
          </div>
          <h1 className="text-2xl font-semibold" style={{ color: '#e2e8f0', margin: 0 }}>
            Intelligence Dashboard
          </h1>
        </div>
        <div className="flex items-center gap-3">
          {stats?.last_refresh && (
            <span className="font-mono text-xs" style={{ color: '#4a5a70' }}>
              Last refresh: {new Date(stats.last_refresh).toLocaleDateString()}
            </span>
          )}
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
            onClick={() => navigate('/prospects')}
            className="font-mono text-xs"
            style={{ color: '#1e6fd4', background: 'none', border: 'none', cursor: 'pointer' }}
          >
            View Matrix →
          </button>
        </div>
        <table className="w-full">
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
                onClick={() => navigate(`/prospects?sector=${encodeURIComponent(s.gics_sector)}`)}>
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
  )
}
