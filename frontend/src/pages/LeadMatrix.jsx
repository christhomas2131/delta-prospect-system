import { useEffect, useState, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { LeadTierBadge } from '../components/Badges'

const SECTORS = ['Energy', 'Materials', 'Industrials', 'Utilities']
const LEAD_TIERS = [
  { value: '', label: 'All Tiers' },
  { value: 'hot', label: 'Hot' },
  { value: 'warm', label: 'Warm' },
  { value: 'watch', label: 'Watch' },
  { value: 'not_qualified', label: 'Not Qualified' },
]
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
          {count}
        </span>
      )}
    </button>
  )
}

function pillarColor(value) {
  if (value === 0 || value == null) return '#2d3a4d'
  if (value <= 2) return '#8fa3bf'
  return '#e2e8f0'
}

export default function LeadMatrix({ watchlistOnly = false }) {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const [data, setData] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Per-row watchlist state (for optimistic updates)
  const [watchlisted, setWatchlisted] = useState({})

  // Chip counts from /api/stats
  const [counts, setCounts] = useState({})

  // Filters
  const [search, setSearch] = useState(searchParams.get('search') || '')
  const [sector, setSector] = useState(searchParams.get('sector') || '')
  const [leadTier, setLeadTier] = useState(searchParams.get('lead_tier') || '')
  const [hasSignals, setHasSignals] = useState(false)
  const [wlFilter, setWlFilter] = useState(false)
  const [hotFilter, setHotFilter] = useState(false)
  const [warmFilter, setWarmFilter] = useState(false)

  const [sort, setSort] = useState('total_signals')
  const [dir, setDir] = useState('desc')
  const [page, setPage] = useState(0)
  const PAGE_SIZE = 50

  // Load chip counts once
  useEffect(() => {
    fetch('/api/stats').then(r => r.json()).then(d => setCounts({
      hot_leads: d.hot_leads,
      warm_leads: d.warm_leads,
      has_signals: d.has_signals_count,
      watchlist: d.watchlist_count,
      total: d.total_prospects,
    })).catch(() => {})
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    const p = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE, sort_by: sort, sort_dir: dir })
    if (search) p.set('search', search)
    if (sector) p.set('sector', sector)
    if (hasSignals) p.set('has_signals', 'true')
    if (wlFilter || watchlistOnly) p.set('watchlist', 'true')

    // Chip-based tier overrides
    if (hotFilter) {
      p.set('lead_tier', 'hot')
    } else if (warmFilter) {
      p.set('lead_tier', 'warm')
    } else if (leadTier) {
      p.set('lead_tier', leadTier)
    }

    try {
      const r = await fetch(`/api/prospects?${p}`)
      if (!r.ok) throw new Error()
      const d = await r.json()
      setData(d.data || [])
      setTotal(d.total || 0)
    } catch {
      setData([])
      setError('Cannot reach the API — is the backend running?')
    }
    setLoading(false)
  }, [search, sector, leadTier, hasSignals, wlFilter, hotFilter, warmFilter, watchlistOnly, sort, dir, page])

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
      if (watchlistOnly && !newVal) setTimeout(load, 300)
      setCounts(c => ({ ...c, watchlist: (c.watchlist || 0) + (newVal ? 1 : -1) }))
    } catch {
      setWatchlisted(prev => ({ ...prev, [prospectId]: !newVal }))
    }
  }

  const getExportUrl = () => {
    const p = new URLSearchParams()
    if (search) p.set('search', search)
    if (sector) p.set('sector', sector)
    if (hasSignals) p.set('has_signals', 'true')
    if (wlFilter || watchlistOnly) p.set('watchlist', 'true')
    if (hotFilter) {
      p.set('lead_tier', 'hot')
    } else if (warmFilter) {
      p.set('lead_tier', 'warm')
    } else if (leadTier) {
      p.set('lead_tier', leadTier)
    }
    return `/api/prospects/export/csv?${p}`
  }

  const clearAll = () => {
    setSearch(''); setSector(''); setLeadTier('')
    setHasSignals(false); setWlFilter(false)
    setHotFilter(false); setWarmFilter(false)
    setPage(0)
  }

  const hasFilters = search || sector || leadTier || hasSignals || wlFilter || hotFilter || warmFilter

  const totalPages = Math.ceil(total / PAGE_SIZE)

  // Chip definitions
  const chips = [
    {
      id: 'hot', label: 'Hot Leads', count: counts.hot_leads,
      active: hotFilter,
      toggle: () => { setHotFilter(h => !h); setWarmFilter(false); setLeadTier(''); setPage(0) },
    },
    {
      id: 'warm', label: 'Warm Leads', count: counts.warm_leads,
      active: warmFilter,
      toggle: () => { setWarmFilter(w => !w); setHotFilter(false); setLeadTier(''); setPage(0) },
    },
    {
      id: 'signals', label: 'Has Signals', count: counts.has_signals,
      active: hasSignals,
      toggle: () => { setHasSignals(s => !s); setPage(0) },
    },
    ...(!watchlistOnly ? [{
      id: 'watchlist', label: '★ Watchlist', count: counts.watchlist,
      active: wlFilter,
      toggle: () => { setWlFilter(s => !s); setPage(0) },
    }] : []),
  ]

  return (
    <div className="p-6" style={{ background: '#0a0c0f', minHeight: '100vh' }}>
      {/* Header */}
      <div className="mb-4">
        <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: '#4a5a70' }}>
          {watchlistOnly ? 'Starred Companies' : 'Intelligence Platform'}
        </div>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold" style={{ color: '#e2e8f0', margin: 0 }}>
              {watchlistOnly ? 'Watchlist' : 'Lead Matrix'}
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
        {!watchlistOnly && (
          <select value={sector} onChange={e => { setSector(e.target.value); setPage(0) }}
            className="px-3 py-1.5 text-sm" style={{ minWidth: 130 }}>
            <option value="">All Sectors</option>
            {SECTORS.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        )}
        <select value={leadTier} onChange={e => { setLeadTier(e.target.value); setHotFilter(false); setWarmFilter(false); setPage(0) }}
          className="px-3 py-1.5 text-sm" style={{ minWidth: 140 }}>
          {LEAD_TIERS.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
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

      {/* Error banner */}
      {error && (
        <div className="mb-4 px-4 py-2 text-sm font-mono" style={{ background: '#1f0808', border: '1px solid #7f1d1d', color: '#ef4444' }}>
          ⚠ {error}
        </div>
      )}

      {/* Empty state */}
      {!loading && data.length === 0 && !error && (
        <div style={{ background: '#111418', border: '1px solid #1e2530', padding: '48px 24px', textAlign: 'center' }}>
          <div className="font-mono text-sm mb-3" style={{ color: '#8fa3bf' }}>No prospects match your filters</div>
          <button
            onClick={clearAll}
            className="font-mono text-xs px-4 py-2"
            style={{ background: 'none', border: '1px solid #1e2530', color: '#8fa3bf', cursor: 'pointer' }}
          >
            Reset Filters
          </button>
        </div>
      )}

      {/* Table */}
      {(loading || data.length > 0) && (
        <div style={{ background: '#111418', border: '1px solid #1e2530' }}>
          <div style={{ overflowX: 'auto' }}>
            <table className="w-full" style={{ minWidth: 1100 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #1e2530' }}>
                  <th className="px-3 py-2.5" style={{ width: 36 }} />
                  <SortHeader label="Ticker" field="ticker" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Company" field="company_name" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Sector" field="gics_sector" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Tier" field="lead_tier" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Prod" field="sig_production" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="LTO" field="sig_license" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Cost" field="sig_cost" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="People" field="sig_people" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Qual" field="sig_quality" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Future" field="sig_future" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Signals" field="total_signals" sort={sort} dir={dir} onSort={handleSort} />
                  <th className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70', whiteSpace: 'nowrap' }}>Top Signal</th>
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td colSpan={13} className="px-4 py-8 text-center font-mono text-xs" style={{ color: '#4a5a70' }}>Loading...</td></tr>
                )}
                {!loading && data.map(p => {
                  const topSig = p.top_signal || ''
                  const topSigTruncated = topSig.length > 60 ? topSig.slice(0, 60) + '...' : topSig
                  return (
                    <tr
                      key={p.prospect_id}
                      style={{ borderBottom: '1px solid #1e2530', cursor: 'pointer' }}
                      onMouseEnter={e => { e.currentTarget.style.background = '#161b24' }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'none' }}
                      onClick={() => navigate(`/deep-intelligence/${p.prospect_id}`)}
                    >
                      {/* Star */}
                      <td className="px-3 py-2.5 text-center" onClick={e => e.stopPropagation()}>
                        <StarButton
                          on={!!watchlisted[p.prospect_id]}
                          onClick={e => toggleWatchlist(e, p.prospect_id)}
                        />
                      </td>
                      {/* Ticker */}
                      <td className="px-4 py-2.5 font-mono text-sm font-semibold" style={{ color: '#1e6fd4' }}>
                        {p.ticker}
                      </td>
                      {/* Company */}
                      <td className="px-4 py-2.5 text-sm" style={{ color: '#e2e8f0', maxWidth: 200 }}>
                        <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={p.company_name}>
                          {p.company_name}
                        </div>
                      </td>
                      {/* Sector */}
                      <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#8fa3bf' }}>{p.gics_sector}</td>
                      {/* Lead Tier */}
                      <td className="px-4 py-2.5"><LeadTierBadge tier={p.lead_tier} /></td>
                      {/* Production */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_production) }}>
                        {p.sig_production > 0 ? p.sig_production : '—'}
                      </td>
                      {/* License to Operate */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_license) }}>
                        {p.sig_license > 0 ? p.sig_license : '—'}
                      </td>
                      {/* Cost */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_cost) }}>
                        {p.sig_cost > 0 ? p.sig_cost : '—'}
                      </td>
                      {/* People */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_people) }}>
                        {p.sig_people > 0 ? p.sig_people : '—'}
                      </td>
                      {/* Quality */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_quality) }}>
                        {p.sig_quality > 0 ? p.sig_quality : '—'}
                      </td>
                      {/* Future Readiness */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_future) }}>
                        {p.sig_future > 0 ? p.sig_future : '—'}
                      </td>
                      {/* Total Signals */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: p.total_signals > 0 ? '#e2e8f0' : '#2d3a4d' }}>
                        {p.total_signals > 0 ? p.total_signals : '—'}
                      </td>
                      {/* Top Signal */}
                      <td className="px-4 py-2.5 text-xs" style={{ color: '#8fa3bf', maxWidth: 240 }}>
                        {topSig ? (
                          <span title={topSig.length > 60 ? topSig : undefined}>
                            {topSigTruncated}
                          </span>
                        ) : '—'}
                      </td>
                    </tr>
                  )
                })}
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
      )}
    </div>
  )
}
