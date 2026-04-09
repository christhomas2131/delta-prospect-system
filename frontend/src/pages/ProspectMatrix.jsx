import { useEffect, useState, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { PressureBadge, StatusBadge, ScoreBar } from '../components/Badges'

const STATUSES = ['unscreened', 'qualified', 'enriched', 'ready_for_outreach', 'suggested_dq', 'disqualified', 'archived']
const SECTORS = ['Energy', 'Materials', 'Industrials', 'Utilities']
const GOLD = '#D4AF37'
const GOLD_DIM = '#555'

function SortHeader({ label, field, sort, dir, onSort }) {
  const active = sort === field
  return (
    <th
      className="px-4 py-2.5 text-left font-mono text-xs uppercase cursor-pointer select-none"
      style={{ color: active ? '#e2e8f0' : '#4a5a70', whiteSpace: 'nowrap' }}
      onClick={() => onSort(field)}
    >
      {label} {active ? (dir === 'desc' ? '↓' : '↑') : ''}
    </th>
  )
}

function StarButton({ on, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px', lineHeight: 1 }}
      title={on ? 'Remove from watchlist' : 'Add to watchlist'}
    >
      <span style={{ fontSize: 15, color: on ? GOLD : GOLD_DIM, transition: 'color 0.1s' }}>
        {on ? '★' : '☆'}
      </span>
    </button>
  )
}

function Chip({ label, count, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className="font-mono text-xs flex items-center gap-1.5 transition-all"
      style={{
        padding: '3px 10px',
        background: active ? '#1e3a5f' : 'none',
        border: `1px solid ${active ? '#1e6fd4' : '#1e2530'}`,
        color: active ? '#93c5fd' : '#4a5a70',
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
      {count != null && (
        <span style={{
          background: active ? 'rgba(30,111,212,0.3)' : '#1e2530',
          color: active ? '#93c5fd' : '#4a5a70',
          fontSize: 9,
          padding: '1px 5px',
          minWidth: 20,
          textAlign: 'center',
          display: 'inline-block',
        }}>
          {count.toLocaleString()}
        </span>
      )}
    </button>
  )
}

export default function ProspectMatrix({ watchlistOnly = false }) {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const [data, setData] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)

  // Per-row watchlist state (for optimistic updates)
  const [watchlisted, setWatchlisted] = useState({})

  // Chip counts (fetched once from /api/stats)
  const [counts, setCounts] = useState({})

  // Filters
  const [search, setSearch] = useState(searchParams.get('search') || '')
  const [status, setStatus] = useState(searchParams.get('status') || '')
  const [sector, setSector] = useState(searchParams.get('sector') || '')
  const [minScore, setMinScore] = useState(null)      // for High Score chip
  const [minStrong, setMinStrong] = useState(null)    // for Strong Signals chip
  const [wlFilter, setWlFilter] = useState(false)     // for Watchlist chip

  const [sort, setSort] = useState('prospect_score')
  const [dir, setDir] = useState('desc')
  const [page, setPage] = useState(0)
  const PAGE_SIZE = 50

  // Load chip counts once
  useEffect(() => {
    fetch('/api/stats').then(r => r.json()).then(d => setCounts({
      unscreened: d.unscreened,
      enriched: d.enriched,
      ready: d.ready_for_outreach,
      watchlist: d.watchlist_count,
      strong: d.strong_signal_companies,
      highscore: d.high_score_count,
    })).catch(() => {})
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    const p = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE, sort_by: sort, sort_dir: dir })
    if (search) p.set('search', search)
    if (status) p.set('status', status)
    if (sector) p.set('sector', sector)
    if (minScore != null) p.set('min_score', minScore)
    if (minStrong != null) p.set('min_strong_signals', minStrong)
    if (wlFilter || watchlistOnly) p.set('watchlist', 'true')
    try {
      const r = await fetch(`/api/prospects?${p}`)
      const d = await r.json()
      setData(d.data || [])
      setTotal(d.total || 0)
    } catch { setData([]) }
    setLoading(false)
  }, [search, status, sector, minScore, minStrong, wlFilter, watchlistOnly, sort, dir, page])

  useEffect(() => { load() }, [load])

  // Sync watchlisted map from freshly loaded rows
  useEffect(() => {
    setWatchlisted(prev => {
      const next = { ...prev }
      data.forEach(p => { next[p.prospect_id] = !!p.is_watchlisted })
      return next
    })
  }, [data])

  const handleSort = (field) => {
    if (sort === field) setDir(d => d === 'desc' ? 'asc' : 'desc')
    else { setSort(field); setDir('desc') }
    setPage(0)
  }

  const toggleWatchlist = async (e, prospectId) => {
    e.stopPropagation()
    const newVal = !watchlisted[prospectId]
    setWatchlisted(prev => ({ ...prev, [prospectId]: newVal }))
    try {
      const r = await fetch(`/api/prospects/${prospectId}/watchlist`, { method: 'PATCH' })
      if (!r.ok) throw new Error()
      // In watchlist-only mode, unstarring should remove the row
      if (watchlistOnly && !newVal) setTimeout(load, 300)
      // Update chip count optimistically
      setCounts(c => ({ ...c, watchlist: (c.watchlist || 0) + (newVal ? 1 : -1) }))
    } catch {
      setWatchlisted(prev => ({ ...prev, [prospectId]: !newVal }))
    }
  }

  const getExportUrl = () => {
    const p = new URLSearchParams()
    if (search) p.set('search', search)
    if (status) p.set('status', status)
    if (sector) p.set('sector', sector)
    if (minScore != null) p.set('min_score', minScore)
    if (minStrong != null) p.set('min_strong_signals', minStrong)
    if (wlFilter || watchlistOnly) p.set('watchlist', 'true')
    return `/api/prospects/export/csv?${p}`
  }

  const clearAll = () => {
    setSearch(''); setStatus(''); setSector('')
    setMinScore(null); setMinStrong(null); setWlFilter(false)
    setPage(0)
  }

  const hasFilters = search || status || sector || minScore != null || minStrong != null || wlFilter

  const totalPages = Math.ceil(total / PAGE_SIZE)

  // Chip definitions — each toggles the corresponding filter state
  const chips = [
    {
      id: 'strong', label: 'Strong Signals', count: counts.strong,
      active: minStrong != null,
      toggle: () => { setMinStrong(s => s != null ? null : 1); setPage(0) },
    },
    {
      id: 'highscore', label: 'Score 7+', count: counts.highscore,
      active: minScore != null,
      toggle: () => { setMinScore(s => s != null ? null : 7); setPage(0) },
    },
    {
      id: 'unscreened', label: 'Unscreened', count: counts.unscreened,
      active: status === 'unscreened',
      toggle: () => { setStatus(s => s === 'unscreened' ? '' : 'unscreened'); setPage(0) },
    },
    {
      id: 'enriched', label: 'Enriched', count: counts.enriched,
      active: status === 'enriched',
      toggle: () => { setStatus(s => s === 'enriched' ? '' : 'enriched'); setPage(0) },
    },
    {
      id: 'ready', label: 'Ready', count: counts.ready,
      active: status === 'ready_for_outreach',
      toggle: () => { setStatus(s => s === 'ready_for_outreach' ? '' : 'ready_for_outreach'); setPage(0) },
    },
    // Hide Watchlist chip when already in watchlist-only mode
    ...(!watchlistOnly ? [{
      id: 'watchlist', label: '★ Watchlist', count: counts.watchlist,
      active: wlFilter,
      toggle: () => { setWlFilter(s => !s); setPage(0) },
    }] : []),
  ]

  return (
    <div className="p-6">
      {/* Header */}
      <div className="mb-4">
        <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: '#4a5a70' }}>
          {watchlistOnly ? 'Starred Companies' : 'Intelligence Platform'}
        </div>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold" style={{ color: '#e2e8f0', margin: 0 }}>
              {watchlistOnly ? 'Watchlist' : 'Prospect Matrix'}
            </h1>
            <div className="text-sm mt-1" style={{ color: '#8fa3bf' }}>
              {total.toLocaleString()} {watchlistOnly ? 'starred' : 'prospects'}
            </div>
          </div>
          {/* CSV Export */}
          <a
            href={getExportUrl()}
            className="font-mono text-xs px-3 py-1.5 flex items-center gap-1.5 mt-1"
            style={{
              background: 'none',
              border: '1px solid #1e2530',
              color: '#4a5a70',
              textDecoration: 'none',
              cursor: 'pointer',
            }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = '#2d3a4d'; e.currentTarget.style.color = '#8fa3bf' }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = '#1e2530'; e.currentTarget.style.color = '#4a5a70' }}
          >
            ↓ Export CSV
          </a>
        </div>
      </div>

      {/* Search + dropdowns */}
      <div className="flex gap-2 mb-3 flex-wrap">
        <input
          type="text"
          placeholder="Search company or ticker..."
          value={search}
          onChange={e => { setSearch(e.target.value); setPage(0) }}
          className="px-3 py-1.5 text-sm"
          style={{ width: 220 }}
        />
        <select value={status} onChange={e => { setStatus(e.target.value); setPage(0) }}
          className="px-3 py-1.5 text-sm" style={{ minWidth: 140 }}>
          <option value="">All Statuses</option>
          {STATUSES.map(s => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
        </select>
        {!watchlistOnly && (
          <select value={sector} onChange={e => { setSector(e.target.value); setPage(0) }}
            className="px-3 py-1.5 text-sm" style={{ minWidth: 130 }}>
            <option value="">All Sectors</option>
            {SECTORS.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        )}
        {hasFilters && (
          <button onClick={clearAll}
            className="px-3 py-1.5 text-xs font-mono"
            style={{ background: 'none', border: '1px solid #1e2530', color: '#8fa3bf', cursor: 'pointer' }}>
            ✕ Clear
          </button>
        )}
      </div>

      {/* Quick Filter Chips */}
      <div className="flex gap-1.5 flex-wrap mb-4">
        {chips.map(c => (
          <Chip key={c.id} label={c.label} count={c.count} active={c.active} onClick={c.toggle} />
        ))}
      </div>

      {/* Table */}
      <div className="card">
        <div style={{ overflowX: 'auto' }}>
          <table className="w-full" style={{ minWidth: 920 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #1e2530' }}>
                <th className="px-3 py-2.5" style={{ width: 36 }} />
                <SortHeader label="Ticker" field="ticker" sort={sort} dir={dir} onSort={handleSort} />
                <SortHeader label="Company" field="company_name" sort={sort} dir={dir} onSort={handleSort} />
                <SortHeader label="Sector" field="gics_sector" sort={sort} dir={dir} onSort={handleSort} />
                <SortHeader label="Status" field="status" sort={sort} dir={dir} onSort={handleSort} />
                <SortHeader label="Score" field="prospect_score" sort={sort} dir={dir} onSort={handleSort} />
                <th className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70' }}>Signals</th>
                <th className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70' }}>Dominant</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={8} className="px-4 py-8 text-center font-mono text-xs" style={{ color: '#4a5a70' }}>Loading...</td></tr>
              )}
              {!loading && data.length === 0 && (
                <tr><td colSpan={8} className="px-4 py-8 text-center font-mono text-xs" style={{ color: '#4a5a70' }}>No prospects found</td></tr>
              )}
              {!loading && data.map(p => (
                <tr
                  key={p.prospect_id}
                  className="table-row-hover"
                  style={{ borderBottom: '1px solid #1e2530' }}
                  onClick={() => navigate(`/prospects/${p.prospect_id}`)}
                >
                  {/* Star — stop propagation so row click doesn't navigate */}
                  <td className="px-3 py-2.5 text-center" onClick={e => e.stopPropagation()}>
                    <StarButton
                      on={!!watchlisted[p.prospect_id]}
                      onClick={e => toggleWatchlist(e, p.prospect_id)}
                    />
                  </td>
                  <td className="px-4 py-2.5 font-mono text-sm font-semibold" style={{ color: '#1e6fd4' }}>{p.ticker}</td>
                  <td className="px-4 py-2.5 text-sm" style={{ color: '#e2e8f0', maxWidth: 220 }}>
                    <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {p.company_name}
                    </div>
                  </td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#8fa3bf' }}>{p.gics_sector}</td>
                  <td className="px-4 py-2.5"><StatusBadge status={p.status} /></td>
                  <td className="px-4 py-2.5"><ScoreBar score={p.prospect_score} /></td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#8fa3bf' }}>
                    {p.total_signals > 0 ? (
                      <span>
                        <span style={{ color: '#e2e8f0' }}>{p.total_signals}</span>
                        {p.strong_signals > 0 && <span style={{ color: '#f97316' }}> ({p.strong_signals}★)</span>}
                      </span>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-2.5">
                    {p.primary_headwind
                      ? <span className="text-xs" style={{ color: '#8fa3bf' }}>{p.primary_headwind.slice(0, 30)}…</span>
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-4 py-3" style={{ borderTop: '1px solid #1e2530' }}>
            <span className="font-mono text-xs" style={{ color: '#4a5a70' }}>
              Page {page + 1} of {totalPages} ({total.toLocaleString()} total)
            </span>
            <div className="flex gap-2">
              <button onClick={() => setPage(p => p - 1)} disabled={page === 0}
                className="font-mono text-xs px-3 py-1.5"
                style={{ background: 'none', border: '1px solid #1e2530', color: page === 0 ? '#2d3a4d' : '#8fa3bf', cursor: page === 0 ? 'not-allowed' : 'pointer' }}>
                ← Prev
              </button>
              <button onClick={() => setPage(p => p + 1)} disabled={page >= totalPages - 1}
                className="font-mono text-xs px-3 py-1.5"
                style={{ background: 'none', border: '1px solid #1e2530', color: page >= totalPages - 1 ? '#2d3a4d' : '#8fa3bf', cursor: page >= totalPages - 1 ? 'not-allowed' : 'pointer' }}>
                Next →
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
