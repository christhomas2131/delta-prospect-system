import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'

function relTime(iso) {
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000)
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days}d ago`
  if (days < 30) return `${Math.floor(days / 7)}w ago`
  return `${Math.floor(days / 30)}mo ago`
}

export default function PitchBookSnapshot({ prospectId }) {
  const [snapshot, setSnapshot] = useState(null)

  useEffect(() => {
    if (!prospectId) return
    let cancelled = false
    fetch(`/api/prospects/${prospectId}/snapshot`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (!cancelled) setSnapshot(d) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [prospectId])

  return (
    <div className="card mb-4">
      <div className="px-4 py-3 flex items-center gap-3" style={{ borderBottom: '1px solid rgba(245,184,61,0.3)' }}>
        <span className="font-mono text-xs uppercase tracking-widest font-semibold" style={{ color: '#f5b83d' }}>
          PitchBook Snapshot
        </span>
        {snapshot && (
          <span className="font-mono text-xs" style={{ color: '#6b7280' }}>
            Last enriched: {relTime(snapshot.enriched_at)}
          </span>
        )}
      </div>
      {snapshot ? (
        <div className="px-4 py-3 snapshot-md">
          <ReactMarkdown>{snapshot.snapshot_markdown || ''}</ReactMarkdown>
        </div>
      ) : (
        <div className="px-4 py-6 font-mono text-xs text-center" style={{ color: '#4a5a70' }}>
          No PitchBook snapshot yet
        </div>
      )}
    </div>
  )
}
