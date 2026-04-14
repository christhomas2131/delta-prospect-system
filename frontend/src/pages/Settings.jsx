import { useEffect, useState } from 'react'

const GOLD = '#D4AF37'
const GOLD_DIM = '#8B7120'

function StatusDot({ configured, valid }) {
  if (!configured) return (
    <span className="font-mono text-xs" style={{ color: '#4a5a70' }}>● Not configured</span>
  )
  if (valid) return (
    <span className="font-mono text-xs" style={{ color: '#22c55e' }}>● Valid</span>
  )
  return (
    <span className="font-mono text-xs" style={{ color: '#ef4444' }}>● Invalid</span>
  )
}

export default function Settings() {
  const [apiKey, setApiKey] = useState('')
  const [status, setStatus] = useState({ configured: false, valid: false })
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState(null)

  useEffect(() => {
    fetch('/api/settings/api-key/status')
      .then(r => r.json())
      .then(setStatus)
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
      setStatus({ configured: d.configured, valid: d.valid })
      showToast(d.valid, d.message)
      if (d.valid) setApiKey('')
    } catch {
      showToast(false, 'Request failed — is the API running?')
    }
    setSaving(false)
  }

  return (
    <div className="p-6 max-w-2xl">
      {/* Header */}
      <div className="mb-6">
        <div className="font-mono text-xs tracking-widest uppercase mb-1" style={{ color: '#4a5a70' }}>
          Configuration
        </div>
        <h1 className="text-2xl font-semibold" style={{ color: '#e2e8f0', margin: 0 }}>
          Settings
        </h1>
      </div>

      {/* Toast */}
      {toast && (
        <div className="mb-4 px-4 py-2 text-sm font-mono"
          style={{
            background: toast.ok ? '#052e16' : '#1f0808',
            border: `1px solid ${toast.ok ? '#14532d' : '#7f1d1d'}`,
            color: toast.ok ? '#22c55e' : '#ef4444',
          }}>
          {toast.msg}
        </div>
      )}

      {/* API Key Card */}
      <div className="card p-5 mb-4">
        {/* Card header */}
        <div className="flex items-center justify-between mb-4">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <span className="font-mono text-xs font-semibold" style={{ color: GOLD }}>◆ DEEP ANALYSIS</span>
              <span className="font-mono text-xs px-1.5 py-0.5"
                style={{ background: '#1a1508', border: `1px solid ${GOLD_DIM}`, color: GOLD }}>
                PREMIUM
              </span>
            </div>
            <div className="font-mono text-xs" style={{ color: '#8fa3bf' }}>Anthropic API Key</div>
          </div>
          <StatusDot {...status} />
        </div>

        {/* Input */}
        <input
          type="password"
          value={apiKey}
          onChange={e => setApiKey(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSave()}
          placeholder={status.valid && status.source === 'env' ? 'sk-ant-...configured via environment' : status.valid ? 'sk-ant-...key saved (manual)' : 'sk-ant-api03-...'}
          className="w-full px-3 py-2 text-sm font-mono mb-3"
          style={{ background: '#0d1017', border: '1px solid #1e2530', color: '#e2e8f0' }}
        />

        {/* Actions */}
        <div className="flex gap-2">
          <button
            onClick={handleSave}
            disabled={saving || !apiKey.trim()}
            className="font-mono text-xs px-4 py-2 transition-all"
            style={{
              background: saving || !apiKey.trim() ? '#1e2530' : GOLD,
              color: saving || !apiKey.trim() ? '#4a5a70' : '#0a0c0f',
              border: 'none',
              cursor: saving || !apiKey.trim() ? 'not-allowed' : 'pointer',
              fontWeight: 600,
            }}
          >
            {saving ? 'Validating...' : 'Save & Validate'}
          </button>
        </div>

        {/* Info note */}
        <div className="mt-4 pt-4 text-xs" style={{ borderTop: '1px solid #1e2530', color: '#4a5a70', lineHeight: 1.6 }}>
          <strong style={{ color: '#8fa3bf' }}>Optional.</strong> Enables Deep Analysis — Claude AI validates rule-based signals,
          detects missed pressures, and generates refined strategic profiles.
          Costs approximately <strong style={{ color: '#8fa3bf' }}>$0.01–0.03 per company</strong> analysis.
          The key is stored in server memory only and is never saved to the database.
        </div>
      </div>

      {/* Status card */}
      <div className="card p-4">
        <div className="font-mono text-xs uppercase tracking-widest mb-3" style={{ color: '#4a5a70' }}>
          Feature Status
        </div>
        <div className="flex items-center justify-between py-2" style={{ borderBottom: '1px solid #1e2530' }}>
          <span className="text-sm" style={{ color: '#8fa3bf' }}>Rule-based enrichment</span>
          <span className="font-mono text-xs" style={{ color: '#22c55e' }}>● Active (free)</span>
        </div>
        <div className="flex items-center justify-between py-2">
          <span className="text-sm" style={{ color: '#8fa3bf' }}>AI Deep Analysis</span>
          <span className="flex items-center gap-2">
            <StatusDot {...status} />
            {status.valid && status.source === 'env' && (
              <span className="font-mono text-xs" style={{ color: '#4a5a70' }}>(env var)</span>
            )}
          </span>
        </div>
      </div>
    </div>
  )
}
