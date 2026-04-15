import { useEffect, useState, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { PillarBadge, StrengthBadge, StatusBadge, LeadTierBadge, PILLAR_COLORS, PILLAR_LABELS } from '../components/Badges'

const GOLD = '#D4AF37'
const GOLD_BG = '#1a1508'
const GOLD_BORDER = '#8B7120'

const STATUS_STEPS = [
  { value: 'unscreened', label: 'Unscreened' },
  { value: 'qualified', label: 'Qualified' },
  { value: 'enriched', label: 'Enriched' },
  { value: 'ready_for_outreach', label: 'Ready for Outreach' },
]

const PILLAR_WEIGHTS = {
  production: '40%',
  license_to_operate: '20%',
  cost: '15%',
  people: '15%',
  quality: '5%',
  future_readiness: '5%',
}

const PILLAR_KEYS = ['production', 'license_to_operate', 'cost', 'people', 'quality', 'future_readiness']

function fmtMarketCap(cents) {
  if (!cents) return null
  const aud = cents / 100
  if (aud >= 1e9) return `$${(aud / 1e9).toFixed(1)}B`
  if (aud >= 1e6) return `$${(aud / 1e6).toFixed(0)}M`
  return `$${(aud / 1e3).toFixed(0)}K`
}

// Compute a 0-10 score per pillar from grouped signals
function pillarScore(signals) {
  if (!signals || signals.length === 0) return 0
  const strengths = signals.map(s => s.strength)
  const hasStrong = strengths.includes('strong')
  const allStrong = strengths.every(s => s === 'strong')
  const count = signals.length

  if (count >= 3 && allStrong) return 10
  if (count >= 3 && hasStrong) return 8
  if (count >= 2) return 6
  // single signal
  if (hasStrong) return 5
  if (strengths.includes('moderate')) return 3
  return 2 // weak
}

// SVG Radar chart for 6 pillars
function RadarChart({ signals }) {
  const size = 260
  const cx = size / 2
  const cy = size / 2
  const maxR = 100
  const levels = [2, 4, 6, 8, 10]

  // Group signals by pressure_type
  const grouped = {}
  PILLAR_KEYS.forEach(k => { grouped[k] = [] })
  ;(signals || []).forEach(s => {
    if (grouped[s.pressure_type]) {
      grouped[s.pressure_type].push(s)
    }
  })

  const scores = PILLAR_KEYS.map(k => pillarScore(grouped[k]))
  const n = PILLAR_KEYS.length
  const angleStep = (2 * Math.PI) / n
  // Start from top (-PI/2)
  const startAngle = -Math.PI / 2

  function polarToXY(angle, r) {
    return {
      x: cx + r * Math.cos(angle),
      y: cy + r * Math.sin(angle),
    }
  }

  // Grid lines (pentagons at each level)
  const gridPaths = levels.map(level => {
    const r = (level / 10) * maxR
    const pts = []
    for (let i = 0; i < n; i++) {
      const angle = startAngle + i * angleStep
      const { x, y } = polarToXY(angle, r)
      pts.push(`${x},${y}`)
    }
    return pts.join(' ')
  })

  // Axis lines
  const axes = PILLAR_KEYS.map((_, i) => {
    const angle = startAngle + i * angleStep
    return polarToXY(angle, maxR)
  })

  // Data polygon
  const dataPoints = scores.map((score, i) => {
    const angle = startAngle + i * angleStep
    const r = (score / 10) * maxR
    return polarToXY(angle, r)
  })
  const dataPath = dataPoints.map(p => `${p.x},${p.y}`).join(' ')

  // Labels positioned outside the chart
  const labelPositions = PILLAR_KEYS.map((key, i) => {
    const angle = startAngle + i * angleStep
    const { x, y } = polarToXY(angle, maxR + 28)
    return { key, x, y, score: scores[i] }
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ overflow: 'visible' }}>
        {/* Grid polygons */}
        {gridPaths.map((pts, i) => (
          <polygon
            key={i}
            points={pts}
            fill="none"
            stroke="#1e2530"
            strokeWidth={i === gridPaths.length - 1 ? 1.5 : 0.5}
          />
        ))}

        {/* Axis lines */}
        {axes.map((pt, i) => (
          <line key={i} x1={cx} y1={cy} x2={pt.x} y2={pt.y} stroke="#1e2530" strokeWidth={0.5} />
        ))}

        {/* Data polygon fill */}
        <polygon
          points={dataPath}
          fill="rgba(30, 111, 212, 0.15)"
          stroke="#1e6fd4"
          strokeWidth={2}
        />

        {/* Data points */}
        {dataPoints.map((pt, i) => (
          <circle
            key={i}
            cx={pt.x}
            cy={pt.y}
            r={3}
            fill={PILLAR_COLORS[PILLAR_KEYS[i]]?.text || '#1e6fd4'}
            stroke="#0a0c0f"
            strokeWidth={1}
          />
        ))}

        {/* Labels */}
        {labelPositions.map(({ key, x, y, score }) => {
          const color = PILLAR_COLORS[key]?.text || '#8fa3bf'
          // Determine text-anchor based on position
          let anchor = 'middle'
          if (x < cx - 10) anchor = 'end'
          else if (x > cx + 10) anchor = 'start'
          return (
            <g key={key}>
              <text
                x={x}
                y={y - 6}
                textAnchor={anchor}
                fill={color}
                fontSize={9}
                fontFamily="ui-monospace, monospace"
                fontWeight={600}
              >
                {PILLAR_LABELS[key]}
              </text>
              <text
                x={x}
                y={y + 6}
                textAnchor={anchor}
                fill="#4a5a70"
                fontSize={8}
                fontFamily="ui-monospace, monospace"
              >
                {score}/10
              </text>
            </g>
          )
        })}
      </svg>

      {/* Pillar weights legend */}
      <div style={{ marginTop: 16, display: 'flex', flexWrap: 'wrap', gap: '4px 12px', justifyContent: 'center' }}>
        {PILLAR_KEYS.map(key => (
          <span key={key} className="font-mono" style={{ fontSize: 9, color: '#4a5a70' }}>
            <span style={{ color: PILLAR_COLORS[key]?.text || '#8fa3bf' }}>
              {PILLAR_LABELS[key]}
            </span>
            {' '}{PILLAR_WEIGHTS[key]}
          </span>
        ))}
      </div>
    </div>
  )
}

// Source badge for signal origin
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

// Status workflow stepper
function StatusStepper({ current, onChangeStatus }) {
  const currentIdx = STATUS_STEPS.findIndex(s => s.value === current)

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 0, overflow: 'auto' }}>
      {STATUS_STEPS.map((step, i) => {
        const isCurrent = step.value === current
        const isCompleted = i < currentIdx
        const isFuture = i > currentIdx

        let bg = '#111418'
        let color = '#4a5a70'
        let borderColor = '#1e2530'
        let icon = null

        if (isCurrent) {
          bg = '#0a1e3d'
          color = '#e2e8f0'
          borderColor = '#1e6fd4'
        } else if (isCompleted) {
          bg = '#052e16'
          color = '#22c55e'
          borderColor = '#14532d'
          icon = '\u2713'
        }

        return (
          <div key={step.value} style={{ display: 'flex', alignItems: 'center' }}>
            <button
              onClick={() => onChangeStatus(step.value)}
              className="font-mono text-xs px-3 py-2 transition-all"
              style={{
                background: bg,
                border: `1px solid ${borderColor}`,
                color,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
                fontWeight: isCurrent ? 600 : 400,
              }}
            >
              {icon && <span style={{ marginRight: 4 }}>{icon}</span>}
              {step.label}
            </button>
            {i < STATUS_STEPS.length - 1 && (
              <div style={{
                width: 20,
                height: 1,
                background: isCompleted ? '#14532d' : '#1e2530',
              }} />
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function DeepIntelligence() {
  const { id } = useParams()
  const navigate = useNavigate()

  // Search state
  const [query, setQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searching, setSearching] = useState(false)
  const searchTimeout = useRef(null)

  // Top prospects (shown when no company selected)
  const [topProspects, setTopProspects] = useState([])
  const [topLoading, setTopLoading] = useState(false)

  // Company detail state
  const [prospect, setProspect] = useState(null)
  const [signals, setSignals] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [notes, setNotes] = useState('')
  const [saving, setSaving] = useState(false)
  const [enriching, setEnriching] = useState(false)
  const [deepAnalysing, setDeepAnalysing] = useState(false)
  const [toast, setToast] = useState(null)
  const [deepAvailable, setDeepAvailable] = useState(false)
  const [lastDeepAt, setLastDeepAt] = useState(null)
  const [isWatchlisted, setIsWatchlisted] = useState(false)
  const [pdfLoading, setPdfLoading] = useState(null) // signal ID being exported

  const showToast = (ok, msg) => {
    setToast({ ok, msg })
    setTimeout(() => setToast(null), 5000)
  }

  // Load top prospects for the landing view
  useEffect(() => {
    if (!id) {
      setTopLoading(true)
      fetch('/api/prospects?limit=10&sort_by=prospect_score&sort_dir=desc')
        .then(r => r.json())
        .then(d => {
          setTopProspects(d.prospects || d.data || d || [])
          setTopLoading(false)
        })
        .catch(() => setTopLoading(false))
    }
  }, [id])

  // Search with debounce
  useEffect(() => {
    if (!query.trim()) {
      setSearchResults([])
      return
    }
    setSearching(true)
    if (searchTimeout.current) clearTimeout(searchTimeout.current)
    searchTimeout.current = setTimeout(async () => {
      try {
        const r = await fetch(`/api/search?q=${encodeURIComponent(query.trim())}&limit=10`)
        if (r.ok) {
          const d = await r.json()
          setSearchResults(d)
        }
      } catch { /* ignore */ }
      setSearching(false)
    }, 300)
    return () => { if (searchTimeout.current) clearTimeout(searchTimeout.current) }
  }, [query])

  // Load prospect detail
  const loadProspect = async () => {
    if (!id) return
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
      if (e.message === 'not_found') {
        setProspect(null)
        setError('Prospect not found.')
      } else {
        setError('Cannot reach the API \u2014 is the backend running?')
      }
    }
    setLoading(false)
  }

  useEffect(() => {
    if (id) {
      loadProspect()
    } else {
      // Clear detail state when navigating back
      setProspect(null)
      setSignals([])
      setError(null)
    }
  }, [id])

  const saveNotes = async () => {
    setSaving(true)
    try {
      const r = await fetch(`/api/prospects/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ analyst_notes: notes }),
      })
      if (r.ok) showToast(true, 'Notes saved')
      else showToast(false, 'Save failed')
    } catch {
      showToast(false, 'Save failed')
    }
    setSaving(false)
  }

  const updateStatus = async (newStatus) => {
    try {
      const r = await fetch(`/api/prospects/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus }),
      })
      if (r.ok) {
        showToast(true, `Status \u2192 ${newStatus.replace(/_/g, ' ')}`)
        loadProspect()
      } else {
        showToast(false, 'Update failed')
      }
    } catch {
      showToast(false, 'Update failed')
    }
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
    if (!prospect) return
    setEnriching(true)
    try {
      const r = await fetch(`/api/enrich/${prospect.ticker}`, { method: 'POST' })
      const d = await r.json()
      showToast(r.ok, d.message || 'Enrichment triggered')
      if (r.ok) loadProspect()
    } catch {
      showToast(false, 'Enrichment request failed')
    }
    setEnriching(false)
  }

  const triggerDeepAnalysis = async () => {
    setDeepAnalysing(true)
    try {
      const r = await fetch(`/api/prospects/${id}/deep-analysis`, { method: 'POST' })
      const d = await r.json()
      if (!r.ok) {
        showToast(false, d.detail || 'Deep analysis failed')
      } else {
        showToast(true,
          `Deep analysis complete \u2014 ${d.new_signals_count} new signals, ` +
          `${d.confirmed_count} confirmed, ${d.disputed_count} disputed ` +
          `(${d.tokens_used?.toLocaleString() || 0} tokens)`
        )
        loadProspect()
      }
    } catch (err) {
      showToast(false, `Request failed: ${err.message || 'network error'}`)
    }
    setDeepAnalysing(false)
  }

  const copyBrief = () => {
    if (!prospect) return
    const topSignals = signals
      .filter(s => s.strength === 'strong')
      .slice(0, 3)
      .map(s => `  \u2022 ${s.pressure_type}: ${s.summary}`)
      .join('\n')

    const brief = [
      `${prospect.ticker} \u2014 ${prospect.company_name}`,
      `${prospect.gics_sector} | Score: ${prospect.prospect_score ? Number(prospect.prospect_score).toFixed(1) : 'N/A'} | Likelihood: ${prospect.likelihood_score || 'N/A'}/10`,
      prospect.primary_headwind ? `Headwind: ${prospect.primary_headwind}` : null,
      topSignals ? `Key signals:\n${topSignals}` : null,
    ].filter(Boolean).join('\n')

    navigator.clipboard.writeText(brief)
    showToast(true, 'Brief copied to clipboard')
  }

  const downloadSources = () => {
    if (!prospect || signals.length === 0) return
    const rows = [['Ticker', 'Signal', 'Pillar', 'Strength', 'Source Title', 'Source URL', 'Date']]
    signals.forEach(s => {
      rows.push([
        prospect.ticker,
        (s.summary || '').replace(/"/g, '""'),
        s.pressure_type,
        s.strength,
        (s.source_title || '').replace(/"/g, '""'),
        s.source_url || '',
        s.source_date || '',
      ])
    })
    const csv = rows.map(r => r.map(c => `"${c}"`).join(',')).join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${prospect.ticker}_sources.csv`
    a.click()
    URL.revokeObjectURL(url)
    showToast(true, 'Sources CSV downloaded')
  }

  const exportSignalPdf = async (signalId) => {
    setPdfLoading(signalId)
    try {
      const r = await fetch(`/api/signals/${signalId}/source-pdf`)
      if (!r.ok) {
        const d = await r.json().catch(() => ({ detail: 'PDF export failed' }))
        showToast(false, d.detail || 'PDF export failed')
        setPdfLoading(null)
        return
      }
      const blob = await r.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'announcement.pdf'
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      showToast(false, 'PDF export failed — network error')
    }
    setPdfLoading(null)
  }

  const hasAISignals = signals.some(
    s => s.model_version === 'claude-deep-v1' || s.validated_by === 'claude-deep-v1'
  )

  // --- Search bar (shared between landing and detail views) ---
  const searchBar = (
    <div style={{ position: 'relative', marginBottom: id ? 16 : 24 }}>
      <input
        type="text"
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder="Search company or ticker..."
        className="w-full font-mono text-sm px-4 py-3"
        style={{
          background: '#0d1017',
          border: '1px solid #1e2530',
          color: '#e2e8f0',
          outline: 'none',
        }}
        onFocus={e => { e.currentTarget.style.borderColor = '#1e6fd4' }}
        onBlur={e => { setTimeout(() => { e.currentTarget.style.borderColor = '#1e2530' }, 200) }}
      />
      {searching && (
        <div className="font-mono text-xs" style={{
          position: 'absolute', right: 12, top: '50%', transform: 'translateY(-50%)',
          color: '#4a5a70',
        }}>
          searching...
        </div>
      )}

      {/* Search results dropdown */}
      {query.trim() && searchResults.length > 0 && (
        <div style={{
          position: 'absolute',
          top: '100%',
          left: 0,
          right: 0,
          zIndex: 50,
          background: '#111418',
          border: '1px solid #1e2530',
          borderTop: 'none',
          maxHeight: 320,
          overflowY: 'auto',
        }}>
          {searchResults.map(r => (
            <div
              key={r.id}
              className="px-4 py-2.5 cursor-pointer transition-all"
              style={{ borderBottom: '1px solid #1e2530' }}
              onMouseEnter={e => { e.currentTarget.style.background = '#161b24' }}
              onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
              onClick={async () => {
                setQuery('')
                setSearchResults([])
                // Search API returns listing id, but we need prospect_id
                // Look it up via the prospects endpoint
                try {
                  const res = await fetch(`/api/prospects?search=${encodeURIComponent(r.ticker)}&limit=1`)
                  const d = await res.json()
                  if (d.data && d.data.length > 0) {
                    navigate(`/deep-intelligence/${d.data[0].prospect_id}`)
                  }
                } catch {
                  // fallback — navigate and let the detail view show an error
                  navigate(`/deep-intelligence/${r.id}`)
                }
              }}
            >
              <div className="flex items-center gap-3">
                <span className="font-mono text-sm font-bold" style={{ color: '#1e6fd4' }}>
                  {r.ticker}
                </span>
                <span className="text-sm" style={{ color: '#e2e8f0' }}>
                  {r.company_name}
                </span>
                <span className="font-mono text-xs" style={{ color: '#4a5a70' }}>
                  {r.gics_sector}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {query.trim() && searchResults.length === 0 && !searching && (
        <div style={{
          position: 'absolute',
          top: '100%',
          left: 0,
          right: 0,
          zIndex: 50,
          background: '#111418',
          border: '1px solid #1e2530',
          borderTop: 'none',
          padding: '12px 16px',
        }}>
          <span className="font-mono text-xs" style={{ color: '#4a5a70' }}>No results found</span>
        </div>
      )}
    </div>
  )

  // --- Toast ---
  const toastEl = toast && (
    <div className="mb-4 px-4 py-2 text-sm font-mono"
      style={{
        background: toast.ok ? '#052e16' : '#1f0808',
        border: `1px solid ${toast.ok ? '#14532d' : '#7f1d1d'}`,
        color: toast.ok ? '#22c55e' : '#ef4444',
      }}>
      {toast.msg}
    </div>
  )

  // ========== LANDING VIEW (no company selected) ==========
  if (!id) {
    return (
      <div className="p-6 max-w-5xl">
        {/* Header */}
        <div className="mb-6">
          <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: GOLD_BORDER }}>
            DELTA PROSPECT SYSTEM
          </div>
          <h1 className="text-2xl font-semibold" style={{ color: '#e2e8f0', margin: 0 }}>
            Deep Intelligence
          </h1>
          <div className="font-mono text-xs mt-1" style={{ color: '#4a5a70' }}>
            Select a company to view full prospect profile and pressure analysis
          </div>
        </div>

        {toastEl}

        {/* Search */}
        {searchBar}

        {/* Top prospects */}
        <div className="mb-4">
          <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: '#4a5a70' }}>
            Top Prospects by Score
          </div>
          {topLoading ? (
            <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>Loading top prospects...</div>
          ) : topProspects.length === 0 ? (
            <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>
              No prospects found. Run ASX refresh and enrichment first.
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {topProspects.map(p => {
                const score = p.prospect_score ? Number(p.prospect_score) : 0
                const scoreColor = score >= 15 ? '#22c55e' : score >= 8 ? '#eab308' : '#3b82f6'
                return (
                  <div
                    key={p.prospect_id}
                    className="card p-4 cursor-pointer transition-all"
                    style={{ borderLeft: `3px solid ${scoreColor}` }}
                    onClick={() => navigate(`/deep-intelligence/${p.prospect_id}`)}
                    onMouseEnter={e => { e.currentTarget.style.background = '#161b24' }}
                    onMouseLeave={e => { e.currentTarget.style.background = '#111418' }}
                  >
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-sm font-bold" style={{ color: '#1e6fd4' }}>
                          {p.ticker}
                        </span>
                        {p.lead_tier && <LeadTierBadge tier={p.lead_tier} />}
                      </div>
                      <span className="font-mono text-lg font-bold" style={{ color: scoreColor }}>
                        {score ? score.toFixed(1) : '\u2014'}
                      </span>
                    </div>
                    <div className="text-sm mb-1" style={{ color: '#e2e8f0' }}>{p.company_name}</div>
                    <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>
                      {p.gics_sector}
                      {p.signal_count != null && <span> · {p.total_signals} signals</span>}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    )
  }

  // ========== DETAIL VIEW (company selected) ==========

  if (loading) {
    return (
      <div className="p-6 max-w-5xl">
        <button onClick={() => navigate('/deep-intelligence')} className="font-mono text-xs mb-4 flex items-center gap-1"
          style={{ background: 'none', border: 'none', color: '#8fa3bf', cursor: 'pointer' }}>
          ← Back to Deep Intelligence
        </button>
        {searchBar}
        <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>Loading prospect...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-6 max-w-5xl">
        <button onClick={() => navigate('/deep-intelligence')} className="font-mono text-xs mb-4 flex items-center gap-1"
          style={{ background: 'none', border: 'none', color: '#8fa3bf', cursor: 'pointer' }}>
          ← Back to Deep Intelligence
        </button>
        {searchBar}
        <div className="card p-6 text-center" style={{ borderLeft: '3px solid #ef4444' }}>
          <div className="font-mono text-sm mb-2" style={{ color: '#ef4444' }}>{error}</div>
          <button onClick={loadProspect} className="font-mono text-xs px-4 py-2"
            style={{ background: '#1e6fd4', color: '#fff', border: 'none', cursor: 'pointer' }}>
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (!prospect) {
    return (
      <div className="p-6 max-w-5xl">
        <button onClick={() => navigate('/deep-intelligence')} className="font-mono text-xs mb-4 flex items-center gap-1"
          style={{ background: 'none', border: 'none', color: '#8fa3bf', cursor: 'pointer' }}>
          ← Back to Deep Intelligence
        </button>
        {searchBar}
        <div className="font-mono text-xs" style={{ color: '#ef4444' }}>Prospect not found.</div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-5xl">
      {/* Back button */}
      <button onClick={() => navigate('/deep-intelligence')} className="font-mono text-xs mb-4 flex items-center gap-1"
        style={{ background: 'none', border: 'none', color: '#8fa3bf', cursor: 'pointer' }}>
        ← Back to Deep Intelligence
      </button>

      {/* Search bar */}
      {searchBar}

      {/* Toast */}
      {toastEl}

      {/* 1. Header card */}
      <div className="card p-5 mb-4">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <div className="flex items-center gap-3 mb-1 flex-wrap">
              <span className="font-mono text-2xl font-bold" style={{ color: '#1e6fd4' }}>{prospect.ticker}</span>
              <button
                onClick={toggleWatchlist}
                title={isWatchlisted ? 'Remove from watchlist' : 'Add to watchlist'}
                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 2px', lineHeight: 1 }}
              >
                <span style={{ fontSize: 22, color: isWatchlisted ? GOLD : '#555', transition: 'color 0.15s' }}>
                  {isWatchlisted ? '\u2605' : '\u2606'}
                </span>
              </button>
              {prospect.lead_tier && <LeadTierBadge tier={prospect.lead_tier} />}
              <StatusBadge status={prospect.status} />
            </div>
            <div className="text-lg font-semibold mb-1" style={{ color: '#e2e8f0' }}>{prospect.company_name}</div>
            <div className="font-mono text-xs" style={{ color: '#8fa3bf' }}>
              {prospect.gics_sector}
              {prospect.gics_industry_group && <span> · {prospect.gics_industry_group}</span>}
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
            <div className="font-mono text-3xl font-bold" style={{
              color: prospect.prospect_score >= 15 ? '#22c55e' : prospect.prospect_score >= 8 ? '#eab308' : '#3b82f6',
            }}>
              {prospect.prospect_score ? Number(prospect.prospect_score).toFixed(1) : '\u2014'}
            </div>
            {prospect.likelihood_score != null && (
              <div className="font-mono text-xs mt-1" style={{ color: '#8fa3bf' }}>
                likelihood {prospect.likelihood_score}/10
              </div>
            )}
            {hasAISignals && (
              <div className="font-mono text-xs mt-1" style={{ color: GOLD }}>{'\u25C6'} AI Enhanced</div>
            )}
            <div className="flex gap-2 mt-2">
              <button onClick={copyBrief}
                className="font-mono text-xs px-3 py-1.5"
                style={{ background: 'none', border: '1px solid #1e2530', color: '#8fa3bf', cursor: 'pointer' }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = '#2d3a4d'; e.currentTarget.style.color = '#e2e8f0' }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = '#1e2530'; e.currentTarget.style.color = '#8fa3bf' }}
              >
                Copy Brief
              </button>
              {signals.length > 0 && (
                <button onClick={downloadSources}
                  className="font-mono text-xs px-3 py-1.5"
                  style={{ background: 'none', border: '1px solid #1e2530', color: '#8fa3bf', cursor: 'pointer' }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = '#2d3a4d'; e.currentTarget.style.color = '#e2e8f0' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = '#1e2530'; e.currentTarget.style.color = '#8fa3bf' }}
                >
                  Download Sources
                </button>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* 2. Status workflow stepper */}
      <div className="card p-4 mb-4">
        <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: '#4a5a70' }}>
          Status Workflow
        </div>
        <StatusStepper current={prospect.status} onChangeStatus={updateStatus} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
        {/* 3. Strategic Profile */}
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
              <div className="font-mono text-xs mb-1" style={{ color: '#22c55e' }}>{'\u2191'} TAILWIND</div>
              <div className="text-sm" style={{ color: '#e2e8f0' }}>{prospect.primary_tailwind}</div>
            </div>
          )}
          {prospect.primary_headwind && (
            <div>
              <div className="font-mono text-xs mb-1" style={{ color: '#ef4444' }}>{'\u2193'} HEADWIND</div>
              <div className="text-sm" style={{ color: '#e2e8f0' }}>{prospect.primary_headwind}</div>
            </div>
          )}
          {!prospect.strategic_direction && !prospect.primary_tailwind && !prospect.primary_headwind && (
            <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>
              No strategic profile yet. Run enrichment to generate.
            </div>
          )}
        </div>

        {/* 4. Radar Chart */}
        <div className="card p-4">
          <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: '#4a5a70' }}>
            Pillar Analysis
          </div>
          {signals.length === 0 ? (
            <div className="font-mono text-xs text-center py-8" style={{ color: '#4a5a70' }}>
              No signals detected. Run enrichment to populate the radar chart.
            </div>
          ) : (
            <RadarChart signals={signals} />
          )}
        </div>
      </div>

      {/* 5. Actions card */}
      <div className="card p-4 mb-4">
        <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: '#4a5a70' }}>
          Actions
        </div>
        <div className="flex flex-wrap gap-2">
          {/* Rule-based enrich */}
          <button onClick={triggerEnrich} disabled={enriching}
            className="font-mono text-xs px-4 py-2"
            style={{
              background: enriching ? '#1e2530' : '#1558a8',
              color: enriching ? '#4a5a70' : '#e2e8f0',
              border: '1px solid #1e3a5f',
              cursor: enriching ? 'not-allowed' : 'pointer',
            }}>
            {enriching ? 'Enriching...' : '\u27F3 Enrich'}
          </button>

          {/* Deep Analysis */}
          {deepAvailable ? (
            <button onClick={triggerDeepAnalysis} disabled={deepAnalysing}
              className="font-mono text-xs px-4 py-2"
              style={{
                background: deepAnalysing ? '#1e2530' : GOLD_BG,
                color: deepAnalysing ? '#4a5a70' : GOLD,
                border: `1px solid ${deepAnalysing ? '#1e2530' : GOLD_BORDER}`,
                cursor: deepAnalysing ? 'not-allowed' : 'pointer',
                fontWeight: 600,
              }}>
              {deepAnalysing ? '\u25C6 Analysing...' : '\u25C6 Deep Analysis'}
            </button>
          ) : (
            <button
              disabled
              title="Configure Anthropic API key in Settings to enable Deep Analysis"
              className="font-mono text-xs px-4 py-2"
              style={{
                background: 'none',
                color: '#2d3a4d',
                border: '1px solid #1e2530',
                cursor: 'not-allowed',
              }}>
              {'\u25C6'} Deep Analysis
            </button>
          )}
        </div>

        {lastDeepAt && (
          <div className="font-mono text-xs mt-2" style={{ color: GOLD_BORDER }}>
            Last AI analysis: {new Date(lastDeepAt).toLocaleDateString()}
          </div>
        )}

        {!deepAvailable && (
          <div className="font-mono text-xs mt-2" style={{ color: '#2d3a4d' }}>
            Configure API key in{' '}
            <a href="/settings" style={{ color: '#4a5a70', textDecoration: 'underline' }}>Settings</a>
            {' '}to enable Deep Analysis
          </div>
        )}
      </div>

      {/* 6. Pressure Signals table */}
      <div className="card mb-4">
        <div className="px-4 py-3 flex items-center gap-3" style={{ borderBottom: '1px solid #1e2530' }}>
          <span className="font-mono text-xs uppercase tracking-widest" style={{ color: '#4a5a70' }}>
            Pressure Signals ({signals.length})
          </span>
          {hasAISignals && (
            <span className="font-mono text-xs" style={{ color: GOLD }}>{'\u25C6'} includes AI signals</span>
          )}
        </div>
        {signals.length === 0 ? (
          <div className="px-4 py-6 font-mono text-xs text-center" style={{ color: '#4a5a70' }}>
            No signals detected — run enrichment to analyse ASX announcements
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="w-full" style={{ minWidth: 700 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #1e2530' }}>
                  {['Pillar', 'Strength', 'Summary', 'Source', 'Confidence', 'Date', ''].map(h => (
                    <th key={h || 'actions'} className="px-4 py-2.5 text-left font-mono text-xs uppercase" style={{ color: '#4a5a70' }}>{h}</th>
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
                      <td className="px-4 py-2.5"><PillarBadge type={s.pressure_type} /></td>
                      <td className="px-4 py-2.5"><StrengthBadge strength={s.strength} /></td>
                      <td className="px-4 py-2.5 text-sm" style={{ color: disputed ? '#6b7280' : '#e2e8f0' }}>
                        <span style={{ textDecoration: disputed ? 'line-through' : 'none' }}>
                          {s.summary}
                        </span>
                        {s.source_title && (
                          <div className="font-mono text-xs mt-0.5 truncate" style={{ color: '#4a5a70' }}>
                            {s.source_url ? (
                              <a href={s.source_url} target="_blank" rel="noopener noreferrer"
                                style={{ color: '#1e6fd4', textDecoration: 'none' }}
                                onMouseEnter={e => { e.currentTarget.style.textDecoration = 'underline' }}
                                onMouseLeave={e => { e.currentTarget.style.textDecoration = 'none' }}
                                title={s.source_title}>
                                {s.source_title}
                              </a>
                            ) : s.source_title}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-2.5">
                        <SourceBadge signal={s} />
                      </td>
                      <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#8fa3bf' }}>
                        {s.confidence_score ? `${Math.round(s.confidence_score * 100)}%` : '\u2014'}
                      </td>
                      <td className="px-4 py-2.5 font-mono text-xs" style={{ color: '#4a5a70' }}>
                        {s.source_date || '\u2014'}
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-1">
                          {/* Open source in new tab */}
                          {s.source_url && !s.source_url.startsWith('claude-deep://') ? (
                            <a href={s.source_url} target="_blank" rel="noopener noreferrer"
                              title="Open source announcement"
                              className="font-mono text-xs px-1.5 py-0.5"
                              style={{ color: '#1e6fd4', border: '1px solid #1e2530', cursor: 'pointer', textDecoration: 'none' }}
                              onMouseEnter={e => { e.currentTarget.style.borderColor = '#1e6fd4' }}
                              onMouseLeave={e => { e.currentTarget.style.borderColor = '#1e2530' }}
                            >↗</a>
                          ) : (
                            <span className="font-mono text-xs px-1.5 py-0.5" style={{ color: '#2d3a4d', border: '1px solid #1e2530' }} title="Source not available">↗</span>
                          )}
                          {/* Export as PDF */}
                          {s.source_url && !s.source_url.startsWith('claude-deep://') ? (
                            <button
                              onClick={() => exportSignalPdf(s.id)}
                              disabled={pdfLoading === s.id}
                              title="Export source as PDF"
                              className="font-mono text-xs px-1.5 py-0.5"
                              style={{ color: pdfLoading === s.id ? '#4a5a70' : '#8fa3bf', border: '1px solid #1e2530', background: 'none', cursor: pdfLoading === s.id ? 'wait' : 'pointer' }}
                              onMouseEnter={e => { if (pdfLoading !== s.id) e.currentTarget.style.borderColor = '#8fa3bf' }}
                              onMouseLeave={e => { e.currentTarget.style.borderColor = '#1e2530' }}
                            >{pdfLoading === s.id ? '...' : 'PDF'}</button>
                          ) : (
                            <span className="font-mono text-xs px-1.5 py-0.5" style={{ color: '#2d3a4d', border: '1px solid #1e2530' }} title="No source URL">PDF</span>
                          )}
                          {/* Copy source URL */}
                          {s.source_url && !s.source_url.startsWith('claude-deep://') && (
                            <button
                              onClick={() => { navigator.clipboard.writeText(s.source_url); showToast(true, 'Source URL copied') }}
                              title="Copy source URL"
                              className="font-mono text-xs px-1.5 py-0.5"
                              style={{ color: '#8fa3bf', border: '1px solid #1e2530', background: 'none', cursor: 'pointer' }}
                              onMouseEnter={e => { e.currentTarget.style.borderColor = '#8fa3bf' }}
                              onMouseLeave={e => { e.currentTarget.style.borderColor = '#1e2530' }}
                            >URL</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 7. Analyst Notes */}
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
          style={{ resize: 'vertical', background: '#0d1017', border: '1px solid #1e2530', color: '#e2e8f0', outline: 'none' }}
          onFocus={e => { e.currentTarget.style.borderColor = '#1e6fd4' }}
          onBlur={e => { e.currentTarget.style.borderColor = '#1e2530' }}
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
