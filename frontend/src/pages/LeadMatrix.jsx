import { useEffect, useState, useCallback, useRef } from 'react'
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

function fmtPrize(dollars) {
  if (!dollars || dollars === 0) return null
  if (dollars >= 1_000_000) return `$${(dollars / 1_000_000).toFixed(1)}M`
  if (dollars >= 1_000) return `$${Math.round(dollars / 1_000)}K`
  return `$${dollars}`
}

function DealFitBadge({ prize }) {
  if (!prize) return <span style={{ color: '#2d3a4d', fontFamily: 'monospace', fontSize: 9 }}>—</span>
  if (prize >= 50_000_000) return (
    <span className="font-mono" style={{ fontSize: 9, padding: '2px 5px', background: '#431407', border: '1px solid #c2410c', color: '#fb923c' }}>
      ENTERPRISE
    </span>
  )
  if (prize >= 5_000_000) return (
    <span className="font-mono" style={{ fontSize: 9, padding: '2px 5px', background: '#052e16', border: '1px solid #14532d', color: '#22c55e' }}>
      SWEET SPOT
    </span>
  )
  return (
    <span className="font-mono" style={{ fontSize: 9, padding: '2px 5px', background: '#111418', border: '1px solid #1e2530', color: '#6b7280' }}>
      SMALL
    </span>
  )
}

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
        background: active ? '#1e3a5f' : 'transparent',
        border: `1px solid ${active ? '#3b82f6' : '#3d4f63'}`,
        color: active ? '#e2e8f0' : '#8fa3bf',
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      {label}
      {count != null && (
        <span style={{
          background: active ? 'rgba(59,130,246,0.3)' : 'rgba(30,37,48,0.8)',
          color: active ? '#e2e8f0' : '#8fa3bf',
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
  const [enriching, setEnriching] = useState(false)
  const [enrichProgress, setEnrichProgress] = useState(null)
  const [toast, setToast] = useState(null)
  const enrichPollRef = useRef(null)

  // Poll enrichment progress while running
  const startPolling = () => {
    if (enrichPollRef.current) return
    enrichPollRef.current = setInterval(async () => {
      try {
        const r = await fetch('/api/enrich/status')
        const d = await r.json()
        setEnrichProgress(d)
        // Keep polling while rule-based enrichment OR AI analysis is running
        if (!d.running && !d.ai_running) {
          clearInterval(enrichPollRef.current)
          enrichPollRef.current = null
          setEnriching(false)
          const aiNote = d.ai_total > 0 ? ` + AI analysis on ${d.ai_total} top prospects` : ''
          setToast({ ok: true, msg: `Enrichment complete — ${d.ok} enriched, ${d.skip} skipped, ${d.fail} failed${aiNote}` })
          setTimeout(() => setToast(null), 10000)
          setTimeout(() => setEnrichProgress(null), 10000)
          load() // refresh the table
        }
      } catch { /* ignore poll errors */ }
    }, 3000)
  }

  // Check if enrichment is already running on mount
  useEffect(() => {
    fetch('/api/enrich/status').then(r => r.json()).then(d => {
      if (d.running || d.ai_running) { setEnriching(true); setEnrichProgress(d); startPolling() }
    }).catch(() => {})
    return () => { if (enrichPollRef.current) clearInterval(enrichPollRef.current) }
  }, [])

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

  const [prize10mFilter, setPrize10mFilter] = useState(false)
  const [prize1mFilter, setPrize1mFilter] = useState(false)
  const [australiaOnly, setAustraliaOnly] = useState(false)
  const [selectedCities, setSelectedCities] = useState([])
  const [cityDropdownOpen, setCityDropdownOpen] = useState(false)
  const [showPillars, setShowPillars] = useState(false)

  const CITY_OPTIONS = ['Brisbane', 'Perth', 'Sydney', 'Melbourne', 'Adelaide', 'Darwin', 'Hobart', 'Canberra', 'Gold Coast', 'Townsville']

  const toggleCity = (city) => {
    setSelectedCities(prev =>
      prev.includes(city) ? prev.filter(c => c !== city) : [...prev, city]
    )
    setPage(0)
  }

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
    if (prize10mFilter) p.set('min_prize', '10000000')
    else if (prize1mFilter) p.set('min_prize', '1000000')
    if (australiaOnly) p.set('australia_only', 'true')
    if (selectedCities.length > 0) p.set('city', selectedCities.join(','))

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
  }, [search, sector, leadTier, hasSignals, wlFilter, hotFilter, warmFilter, prize10mFilter, prize1mFilter, australiaOnly, selectedCities, watchlistOnly, sort, dir, page])

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
    setPrize10mFilter(false); setPrize1mFilter(false)
    setAustraliaOnly(false); setSelectedCities([])
    setPage(0)
  }

  const hasFilters = search || sector || leadTier || hasSignals || wlFilter || hotFilter || warmFilter || prize10mFilter || prize1mFilter || australiaOnly || selectedCities.length > 0

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
    {
      id: 'prize10m', label: '$10M+ Impact',
      active: prize10mFilter,
      toggle: () => { setPrize10mFilter(f => !f); setPrize1mFilter(false); setPage(0) },
    },
    {
      id: 'prize1m', label: '$1M–$10M',
      active: prize1mFilter,
      toggle: () => { setPrize1mFilter(f => !f); setPrize10mFilter(false); setPage(0) },
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
          <div className="flex items-center gap-2 mt-1">
            <button
              onClick={async () => {
                setEnriching(true)
                try {
                  const r = await fetch('/api/enrich/batch', { method: 'POST' })
                  const d = await r.json()
                  setToast({ ok: true, msg: d.message || 'Batch enrichment started' })
                  setTimeout(() => setToast(null), 5000)
                  startPolling()
                } catch {
                  setToast({ ok: false, msg: 'Batch enrichment failed' })
                  setEnriching(false)
                  setTimeout(() => setToast(null), 5000)
                }
              }}
              disabled={enriching}
              className="font-mono text-xs px-4 py-1.5 transition-all"
              style={{
                background: enriching ? '#1e2530' : '#14532d',
                color: enriching ? '#4a5a70' : '#22c55e',
                border: '1px solid',
                borderColor: enriching ? '#1e2530' : '#166534',
                cursor: enriching ? 'not-allowed' : 'pointer',
              }}
            >
              {enriching && enrichProgress ? `ENRICHING ${enrichProgress.current}/${enrichProgress.total}` : enriching ? 'ENRICHING...' : 'ENRICH ALL'}
            </button>
            <a
              href={getExportUrl()}
              className="font-mono text-xs px-3 py-1.5 flex items-center gap-1.5"
              style={{
                background: 'none',
                border: '1px solid #2d3a4d',
                color: '#8fa3bf',
                textDecoration: 'none',
                cursor: 'pointer',
              }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = '#4a5a70'; e.currentTarget.style.color = '#e2e8f0' }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = '#2d3a4d'; e.currentTarget.style.color = '#8fa3bf' }}
            >
              ↓ Export CSV
            </a>
          </div>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div className="mb-4 px-4 py-2 text-sm font-mono" style={{ background: toast.ok ? '#052e16' : '#1f0808', border: `1px solid ${toast.ok ? '#14532d' : '#7f1d1d'}`, color: toast.ok ? '#22c55e' : '#ef4444' }}>
          {toast.msg}
        </div>
      )}

      {/* Enrichment Progress */}
      {enrichProgress && (
        <div className="mb-4 px-4 py-3 font-mono text-xs" style={{ background: '#111418', border: '1px solid #14532d' }}>
          {/* Phase 1: Rule-based enrichment */}
          {!enrichProgress.ai_running && (
            <div className="flex items-center justify-between mb-2">
              <span style={{ color: '#22c55e' }}>
                Enriching {enrichProgress.current} of {enrichProgress.total} — {enrichProgress.ticker}
              </span>
              <span style={{ color: '#4a5a70' }}>
                {enrichProgress.ok} done · {enrichProgress.skip} skipped · {enrichProgress.fail} failed
              </span>
            </div>
          )}
          {/* Phase 2: AI deep analysis */}
          {enrichProgress.ai_running && (
            <div className="flex items-center justify-between mb-2">
              <span style={{ color: '#D4AF37' }}>
                ◆ AI analysis on top prospects — {enrichProgress.ai_ticker}
              </span>
              <span style={{ color: '#8B7120' }}>
                {enrichProgress.ai_current} of {enrichProgress.ai_total}
              </span>
            </div>
          )}
          {/* Post-enrichment: AI analysis queued */}
          {!enrichProgress.ai_running && enrichProgress.ai_total > 0 && enrichProgress.ai_current === 0 && (
            <div className="mb-2" style={{ color: '#8B7120' }}>
              ◆ AI analysis starting...
            </div>
          )}
          {/* Progress bar */}
          <div style={{ width: '100%', height: 4, background: '#1e2530', display: 'flex', gap: 2 }}>
            {/* Enrichment bar */}
            <div style={{
              flex: enrichProgress.ai_total > 0 ? '3 3 0' : '1 1 0',
              height: '100%',
              background: '#1e2530',
              position: 'relative',
            }}>
              <div style={{
                position: 'absolute', top: 0, left: 0, bottom: 0,
                width: `${enrichProgress.total > 0 ? (enrichProgress.current / enrichProgress.total * 100) : 0}%`,
                background: '#22c55e',
                transition: 'width 0.3s',
              }} />
            </div>
            {/* AI bar (only shown when AI phase is active or complete) */}
            {enrichProgress.ai_total > 0 && (
              <div style={{ flex: '1 1 0', height: '100%', background: '#1a1508', position: 'relative' }}>
                <div style={{
                  position: 'absolute', top: 0, left: 0, bottom: 0,
                  width: `${enrichProgress.ai_total > 0 ? (enrichProgress.ai_current / enrichProgress.ai_total * 100) : 0}%`,
                  background: '#D4AF37',
                  transition: 'width 0.3s',
                }} />
              </div>
            )}
          </div>
          {enrichProgress.ai_total > 0 && (
            <div className="flex justify-between mt-1">
              <span style={{ color: '#22c55e', fontSize: 8 }}>RULE-BASED</span>
              <span style={{ color: '#8B7120', fontSize: 8 }}>◆ AI DEEP ANALYSIS</span>
            </div>
          )}
        </div>
      )}

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

        {/* Australia Only toggle */}
        <button
          onClick={() => { setAustraliaOnly(a => !a); setPage(0) }}
          className="font-mono text-xs px-3 py-1.5 flex items-center gap-1.5"
          style={{
            background: australiaOnly ? '#1e3a5f' : 'transparent',
            border: `1px solid ${australiaOnly ? '#3b82f6' : '#3d4f63'}`,
            color: australiaOnly ? '#e2e8f0' : '#8fa3bf',
            cursor: 'pointer',
          }}
        >
          🇦🇺 AU Only
        </button>

        {/* City multi-select dropdown */}
        <div style={{ position: 'relative' }}>
          <button
            onClick={() => setCityDropdownOpen(o => !o)}
            className="font-mono text-xs px-3 py-1.5 flex items-center gap-1.5"
            style={{
              background: selectedCities.length > 0 ? '#1e3a5f' : 'transparent',
              border: `1px solid ${selectedCities.length > 0 ? '#3b82f6' : '#3d4f63'}`,
              color: selectedCities.length > 0 ? '#e2e8f0' : '#8fa3bf',
              cursor: 'pointer',
            }}
          >
            {selectedCities.length > 0 ? `${selectedCities.length} cit${selectedCities.length > 1 ? 'ies' : 'y'}` : 'City ▾'}
          </button>
          {cityDropdownOpen && (
            <div
              style={{
                position: 'absolute', top: '100%', left: 0, zIndex: 50,
                background: '#111418', border: '1px solid #2d3a4d',
                minWidth: 160, marginTop: 2,
              }}
              onMouseLeave={() => setCityDropdownOpen(false)}
            >
              {CITY_OPTIONS.map(city => (
                <label key={city}
                  className="flex items-center gap-2 px-3 py-1.5 cursor-pointer font-mono text-xs"
                  style={{
                    color: selectedCities.includes(city) ? '#e2e8f0' : '#8fa3bf',
                    background: selectedCities.includes(city) ? '#1e2c3d' : 'transparent',
                  }}
                  onMouseEnter={e => { if (!selectedCities.includes(city)) e.currentTarget.style.background = '#161b24' }}
                  onMouseLeave={e => { if (!selectedCities.includes(city)) e.currentTarget.style.background = 'transparent' }}
                >
                  <input
                    type="checkbox"
                    checked={selectedCities.includes(city)}
                    onChange={() => toggleCity(city)}
                    style={{ accentColor: '#3b82f6' }}
                  />
                  {city}
                </label>
              ))}
              {selectedCities.length > 0 && (
                <button
                  onClick={() => { setSelectedCities([]); setCityDropdownOpen(false) }}
                  className="w-full font-mono text-xs px-3 py-1.5 text-left"
                  style={{ color: '#ef4444', borderTop: '1px solid #2d3a4d', background: 'none', cursor: 'pointer' }}
                >
                  ✕ Clear cities
                </button>
              )}
            </div>
          )}
        </div>

        {hasFilters && (
          <button onClick={clearAll}
            className="px-3 py-1.5 text-xs font-mono"
            style={{ background: 'none', border: '1px solid #2d3a4d', color: '#8fa3bf', cursor: 'pointer' }}>
            ✕ Clear all
          </button>
        )}
      </div>

      {/* Pillar toggle + Quick Filter Chips */}
      <div className="flex items-center gap-1.5 flex-wrap mb-4">
        {/* Pillar toggle */}
        <button
          onClick={() => setShowPillars(s => !s)}
          className="font-mono text-xs px-3 py-1.5 flex items-center gap-1.5"
          style={{
            background: showPillars ? '#1a2535' : 'transparent',
            border: '1px solid #3d4f63',
            color: showPillars ? '#93c5fd' : '#6b7f96',
            cursor: 'pointer',
          }}
        >
          {showPillars ? '▼' : '▶'} Pillar Detail
        </button>

        <div style={{ width: 1, height: 18, background: '#1e2530', margin: '0 2px' }} />

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
            <table className="w-full" style={{ minWidth: showPillars ? 1400 : 900 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #1e2530' }}>
                  <th className="px-3 py-2.5" style={{ width: 36 }} />
                  <SortHeader label="Ticker" field="ticker" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Company" field="company_name" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Sector" field="gics_sector" sort={sort} dir={dir} onSort={handleSort} />
                  <SortHeader label="Tier" field="lead_tier" sort={sort} dir={dir} onSort={handleSort} />
                  {showPillars && <>
                    <SortHeader label="Prod" field="sig_production" sort={sort} dir={dir} onSort={handleSort} />
                    <SortHeader label="LTO" field="sig_license" sort={sort} dir={dir} onSort={handleSort} />
                    <SortHeader label="Cost" field="sig_cost" sort={sort} dir={dir} onSort={handleSort} />
                    <SortHeader label="People" field="sig_people" sort={sort} dir={dir} onSort={handleSort} />
                    <SortHeader label="Qual" field="sig_quality" sort={sort} dir={dir} onSort={handleSort} />
                    <SortHeader label="Future" field="sig_future" sort={sort} dir={dir} onSort={handleSort} />
                  </>}
                  <SortHeader label="Signals" field="total_signals" sort={sort} dir={dir} onSort={handleSort} />
                  <th className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70', whiteSpace: 'nowrap' }}>Location</th>
                  <SortHeader label="Est. Impact" field="size_of_prize" sort={sort} dir={dir} onSort={handleSort} />
                  <th className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70', whiteSpace: 'nowrap' }}>Deal Fit</th>
                  <th className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70', whiteSpace: 'nowrap' }}>Top Signal</th>
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td colSpan={showPillars ? 17 : 11} className="px-4 py-8 text-center font-mono text-xs" style={{ color: '#4a5a70' }}>Loading...</td></tr>
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
                      {/* Pillar columns (collapsible) */}
                      {showPillars && <>
                        <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_production) }}>
                          {p.sig_production > 0 ? p.sig_production : '—'}
                        </td>
                        <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_license) }}>
                          {p.sig_license > 0 ? p.sig_license : '—'}
                        </td>
                        <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_cost) }}>
                          {p.sig_cost > 0 ? p.sig_cost : '—'}
                        </td>
                        <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_people) }}>
                          {p.sig_people > 0 ? p.sig_people : '—'}
                        </td>
                        <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_quality) }}>
                          {p.sig_quality > 0 ? p.sig_quality : '—'}
                        </td>
                        <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: pillarColor(p.sig_future) }}>
                          {p.sig_future > 0 ? p.sig_future : '—'}
                        </td>
                      </>}
                      {/* Total Signals */}
                      <td className="px-4 py-2.5 font-mono text-xs text-center" style={{ color: p.total_signals > 0 ? '#e2e8f0' : '#2d3a4d' }}>
                        {p.total_signals > 0 ? p.total_signals : '—'}
                      </td>
                      {/* Location */}
                      <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#8fa3bf', whiteSpace: 'nowrap' }}>
                        {p.registered_city && p.registered_state
                          ? `${p.registered_city}, ${p.registered_state}`
                          : p.registered_state || 'Australia'}
                      </td>
                      {/* Est. Impact */}
                      <td className="px-4 py-2.5 font-mono text-xs text-right" style={{ color: p.size_of_prize >= 5_000_000 ? '#22c55e' : p.size_of_prize > 0 ? '#8fa3bf' : '#2d3a4d' }}>
                        {fmtPrize(p.size_of_prize) || '—'}
                      </td>
                      {/* Deal Fit */}
                      <td className="px-4 py-2.5">
                        <DealFitBadge prize={p.size_of_prize} />
                      </td>
                      {/* Top Signal */}
                      <td className="px-4 py-2.5 text-xs" style={{ color: '#8fa3bf', maxWidth: 260 }}>
                        {topSig ? (
                          <div className="flex items-center gap-1.5">
                            <span title={topSig.length > 60 ? topSig : undefined} style={{ flex: 1 }}>
                              {topSigTruncated}
                            </span>
                            {p.top_signal_url && !p.top_signal_url.startsWith('claude-deep://') && (
                              <a href={p.top_signal_url} target="_blank" rel="noopener noreferrer"
                                onClick={e => e.stopPropagation()}
                                title="Open source announcement"
                                className="font-mono text-xs"
                                style={{ color: '#1e6fd4', textDecoration: 'none', flexShrink: 0 }}>↗</a>
                            )}
                          </div>
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
