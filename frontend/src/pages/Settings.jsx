import { useEffect, useState } from 'react'

const GOLD = 'var(--gold)'
const GOLD_DIM = 'var(--gold-dim)'

function StatusDot({ configured, valid }) {
  if (!configured) {
    return <span className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>● Not configured</span>
  }
  if (valid) {
    return <span className="font-mono text-xs" style={{ color: 'var(--positive)' }}>● Valid</span>
  }
  return <span className="font-mono text-xs" style={{ color: 'var(--risk)' }}>● Invalid</span>
}

export default function Settings() {
  const [apiKey, setApiKey] = useState('')
  const [status, setStatus] = useState({ configured: false, valid: false, source: null })
  const [firecrawlStatus, setFirecrawlStatus] = useState({ configured: false, valid: false, source: null })
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState(null)

  useEffect(() => {
    fetch('/api/settings/api-key/status')
      .then(r => r.json())
      .then(setStatus)
      .catch(() => {})

    fetch('/api/settings/firecrawl/status')
      .then(r => r.json())
      .then(setFirecrawlStatus)
      .catch(() => {})
  }, [])

  const showToast = (ok, msg) => {
    setToast({ ok, msg })
    setTimeout(() => setToast(null), 5000)
  }

  const handleSave = async () => {
    if (!apiKey.trim()) return
    setSaving(true)
    try {
      const r = await fetch('/api/settings/api-key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey.trim() }),
      })
      const d = await r.json()
      setStatus({
        configured: d.configured,
        valid: d.valid,
        source: d.valid ? 'manual' : null,
      })
      showToast(d.valid, d.message)
      if (d.valid) setApiKey('')
    } catch {
      showToast(false, 'Request failed - is the API running?')
    }
    setSaving(false)
  }

  return (
    <div className="p-6 max-w-2xl">
      <div className="mb-6">
        <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: 'var(--text-muted)' }}>
          Configuration
        </div>
        <h1 className="text-2xl font-semibold" style={{ color: 'var(--text-primary)', margin: 0 }}>
          Settings
        </h1>
      </div>

      {toast && (
        <div className="mb-4 px-4 py-2 text-sm font-mono"
          style={{
            background: toast.ok ? 'var(--positive-bg)' : 'var(--risk-bg)',
            border: `1px solid ${toast.ok ? 'var(--positive-border)' : 'var(--risk-border)'}`,
            color: toast.ok ? 'var(--positive)' : 'var(--risk)',
          }}>
          {toast.msg}
        </div>
      )}

      <div className="card p-5 mb-4">
        <div className="flex items-center justify-between mb-4">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="font-mono text-xs font-semibold" style={{ color: GOLD }}>◆ DEEP ANALYSIS</span>
              <span className="font-mono text-xs px-1.5 py-0.5"
                style={{ background: 'var(--gold-bg)', border: `1px solid ${GOLD_DIM}`, color: GOLD }}>
                PREMIUM
              </span>
            </div>
            <div className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>Anthropic API Key</div>
          </div>
          <StatusDot {...status} />
        </div>

        <input
          type="password"
          value={apiKey}
          onChange={e => setApiKey(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSave()}
          placeholder={status.valid && status.source === 'env' ? 'sk-ant-...configured via environment' : status.valid ? 'sk-ant-...key saved for this session' : 'sk-ant-api03-...'}
          className="w-full px-3 py-2 text-sm font-mono mb-3"
          style={{ background: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
        />

        <div className="flex gap-2">
          <button
            onClick={handleSave}
            disabled={saving || !apiKey.trim()}
            className="font-mono text-xs px-4 py-2 transition-all"
            style={{
              background: saving || !apiKey.trim() ? 'var(--border)' : GOLD,
              color: saving || !apiKey.trim() ? 'var(--text-muted)' : 'var(--on-gold)',
              border: 'none',
              cursor: saving || !apiKey.trim() ? 'not-allowed' : 'pointer',
              fontWeight: 600,
            }}
          >
            {saving ? 'Validating...' : 'Save & Validate'}
          </button>
        </div>

        <div className="mt-4 pt-4 text-xs" style={{ borderTop: '1px solid var(--border)', color: 'var(--text-muted)', lineHeight: 1.6 }}>
          <strong style={{ color: 'var(--text-secondary)' }}>Optional.</strong> Enables Deep Analysis - Claude AI validates rule-based signals,
          detects missed pressures, and generates refined strategic profiles.
          Costs approximately <strong style={{ color: 'var(--text-secondary)' }}>$0.01-$0.03 per company</strong> analysis.
        </div>

        <div className="mt-3 text-xs" style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>
          Manual keys are stored in server memory for the current process only. For Railway auto-analysis after deploys/restarts,
          set <span className="font-mono">ANTHROPIC_API_KEY</span> in the environment.
        </div>
      </div>

      <div className="card p-4">
        <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: 'var(--text-muted)' }}>
          Feature Status
        </div>
        <div className="flex items-center justify-between py-2" style={{ borderBottom: '1px solid var(--border)' }}>
          <span className="text-sm" style={{ color: 'var(--text-secondary)' }}>Rule-based enrichment</span>
          <span className="font-mono text-xs" style={{ color: 'var(--positive)' }}>● Active (free)</span>
        </div>
        <div className="flex items-center justify-between py-2">
          <span className="text-sm" style={{ color: 'var(--text-secondary)' }}>AI Deep Analysis</span>
          <span className="flex items-center gap-2">
            <StatusDot {...status} />
            {status.valid && status.source === 'env' && (
              <span className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>(env var)</span>
            )}
            {status.valid && status.source === 'manual' && (
              <span className="font-mono text-xs" style={{ color: 'var(--gold-dim)' }}>(session only)</span>
            )}
          </span>
        </div>
        <div className="flex items-center justify-between py-2" style={{ borderTop: '1px solid var(--border)' }}>
          <span className="text-sm" style={{ color: 'var(--text-secondary)' }}>V3 Document Intelligence</span>
          <span className="flex items-center gap-2">
            <StatusDot {...firecrawlStatus} />
            {firecrawlStatus.valid && firecrawlStatus.source === 'env' && (
              <span className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>(FIRECRAWL_API_KEY)</span>
            )}
          </span>
        </div>
        <div className="mt-3 text-xs" style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>
          Firecrawl powers the new full-document layer for V3. Set <span className="font-mono">FIRECRAWL_API_KEY</span> in the
          environment to let Deep Analysis read full filings and reports instead of just headlines.
        </div>
      </div>
    </div>
  )
}
