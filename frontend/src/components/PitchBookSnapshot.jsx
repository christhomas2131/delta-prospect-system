import { useEffect, useState } from 'react'

function relTime(iso) {
  const days = Math.floor((Date.now() - new Date(iso)) / 86400000)
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days}d ago`
  if (days < 30) return `${Math.floor(days / 7)}w ago`
  return `${Math.floor(days / 30)}mo ago`
}

// Inline renderer — handles the markdown patterns used in PitchBook snapshots.
// Replaces react-markdown to avoid ESM bundling issues in production Docker builds.
function renderInline(text) {
  const parts = text.split(/(\*\*[^*]+\*\*)/)
  return parts.map((part, i) =>
    part.startsWith('**') && part.endsWith('**')
      ? <strong key={i} style={{ color: '#e2e8f0' }}>{part.slice(2, -2)}</strong>
      : part
  )
}

function MarkdownBody({ text }) {
  if (!text) return null
  const lines = text.split('\n')
  const nodes = []
  let listBuf = []

  const flushList = () => {
    if (listBuf.length) {
      nodes.push(
        <ul key={`ul-${nodes.length}`} style={{ paddingLeft: '1.2rem', margin: '0.2rem 0', color: '#c9d5e0', fontSize: '0.875rem' }}>
          {listBuf.map((item, i) => <li key={i} style={{ margin: '0.1rem 0' }}>{renderInline(item)}</li>)}
        </ul>
      )
      listBuf = []
    }
  }

  lines.forEach((line, i) => {
    if (line.startsWith('### ')) {
      flushList()
      nodes.push(<h3 key={i} style={{ color: '#e2e8f0', fontWeight: 600, fontSize: '0.875rem', margin: '0.75rem 0 0.2rem' }}>{renderInline(line.slice(4))}</h3>)
    } else if (line.startsWith('## ')) {
      flushList()
      nodes.push(<h2 key={i} style={{ color: '#e2e8f0', fontWeight: 600, fontSize: '0.95rem', margin: '0.75rem 0 0.2rem' }}>{renderInline(line.slice(3))}</h2>)
    } else if (line.startsWith('# ')) {
      flushList()
      nodes.push(<h1 key={i} style={{ color: '#e2e8f0', fontWeight: 600, fontSize: '1rem', margin: '0.75rem 0 0.2rem' }}>{renderInline(line.slice(2))}</h1>)
    } else if (/^[-*] /.test(line)) {
      listBuf.push(line.slice(2))
    } else if (/^\d+\. /.test(line)) {
      listBuf.push(line.replace(/^\d+\. /, ''))
    } else if (line.trim() === '') {
      flushList()
      nodes.push(<div key={i} style={{ height: '0.4rem' }} />)
    } else {
      flushList()
      nodes.push(<p key={i} style={{ color: '#c9d5e0', fontSize: '0.875rem', margin: '0.2rem 0', lineHeight: 1.5 }}>{renderInline(line)}</p>)
    }
  })
  flushList()
  return <>{nodes}</>
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
        <div className="px-4 py-3">
          <MarkdownBody text={snapshot.snapshot_markdown} />
        </div>
      ) : (
        <div className="px-4 py-6 font-mono text-xs text-center" style={{ color: '#4a5a70' }}>
          No PitchBook snapshot yet
        </div>
      )}
    </div>
  )
}
