import { useEffect, useRef, useState } from 'react'
import { loadViews, storeViews } from '../lib/savedViews'

/** Save / apply named Lead Matrix filter sets (stored in the browser; the URL is the view). */
export default function SavedViews({ currentSearch, onApply }) {
  const [views, setViews] = useState(loadViews)
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const close = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  const save = () => {
    const name = window.prompt('Name this view (e.g. "Hot Materials, Perth")')
    if (!name) return
    const next = [...views.filter(v => v.name !== name), { name, search: currentSearch, saved_at: new Date().toISOString() }]
    storeViews(next); setViews(next); setOpen(false)
  }
  const remove = (name) => {
    const next = views.filter(v => v.name !== name)
    storeViews(next); setViews(next)
  }

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button onClick={() => setOpen(o => !o)} aria-haspopup="menu" aria-expanded={open}
        className="font-mono text-xs px-3 py-1.5"
        style={{ background: 'none', border: '1px solid var(--border-strong)', color: 'var(--text-secondary)', cursor: 'pointer' }}>
        Views {views.length ? `(${views.length})` : ''} ▾
      </button>
      {open && (
        <div role="menu" className="card p-2" style={{ position: 'absolute', right: 0, top: '110%', zIndex: 20, minWidth: 240 }}>
          <button role="menuitem" onClick={save} className="font-mono text-xs w-full text-left px-2 py-1.5"
            style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer' }}>
            + Save current filters as a view
          </button>
          {views.length === 0 && <div className="font-mono text-xs px-2 py-1" style={{ color: 'var(--text-muted)' }}>No saved views yet.</div>}
          {views.map(v => (
            <div key={v.name} className="flex items-center gap-2 px-2 py-1" style={{ borderTop: '1px solid var(--border)' }}>
              <button role="menuitem" onClick={() => { onApply(v.search); setOpen(false) }} className="font-mono text-xs text-left flex-1"
                style={{ background: 'none', border: 'none', color: 'var(--text-primary)', cursor: 'pointer' }} title={v.search}>
                {v.name}
              </button>
              <button onClick={() => remove(v.name)} aria-label={`Delete view ${v.name}`} className="font-mono text-xs"
                style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer' }}>✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
