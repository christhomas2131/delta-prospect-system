import { NavLink } from 'react-router-dom'
import { useEffect, useState } from 'react'

const NAV = [
  { to: '/', label: 'Dashboard', icon: '\u25C6' },
  { to: '/leads', label: 'Lead Matrix', icon: '\u25A6' },
  { to: '/deep-intelligence', label: 'Deep Intelligence', icon: '\u25C8' },
  { to: '/watchlist', label: 'Watchlist', icon: '\u2605' },
  { to: '/settings', label: 'Settings', icon: '\u2699' },
]

function ThemeToggle() {
  const [theme, setTheme] = useState(() => {
    if (typeof document === 'undefined') return 'dark'
    return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
  })
  const toggle = () => {
    const next = theme === 'light' ? 'dark' : 'light'
    document.documentElement.dataset.theme = next
    if (typeof localStorage !== 'undefined') localStorage.setItem('delta-theme', next)
    setTheme(next)
  }
  const isLight = theme === 'light'
  return (
    <button
      onClick={toggle}
      aria-label={isLight ? 'Switch to dark mode' : 'Switch to light mode'}
      title={isLight ? 'Switch to dark mode' : 'Switch to light mode'}
      className="font-mono text-xs flex items-center gap-1.5 px-2 py-1 transition-all"
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        color: 'var(--text-secondary)',
        cursor: 'pointer',
      }}
    >
      <span style={{ fontSize: 12, color: isLight ? 'var(--gold)' : 'var(--accent-light)' }}>
        {isLight ? '\u2600' : '\u263D'}
      </span>
      <span>{isLight ? 'Light' : 'Dark'}</span>
    </button>
  )
}

export default function Layout({ children }) {
  const [aiActive, setAiActive] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    fetch('/api/settings/api-key/status')
      .then(r => r.json())
      .then(d => setAiActive(d.valid))
      .catch(() => {})
  }, [])

  const navStyle = (isActive, to) => ({
    color: isActive ? 'var(--text-primary)' : to === '/watchlist' || to === '/deep-intelligence' ? 'var(--gold-dim)' : 'var(--text-secondary)',
    background: isActive ? 'var(--card-hover)' : 'transparent',
    borderLeft: isActive
      ? `2px solid ${to === '/watchlist' || to === '/deep-intelligence' ? 'var(--gold)' : 'var(--accent)'}`
      : '2px solid transparent',
  })

  return (
    <div className="flex min-h-screen" style={{ background: 'var(--bg)' }}>
      <div className="md:hidden fixed top-0 left-0 right-0 z-30 flex items-center justify-between px-4 py-3"
        style={{ background: 'var(--surface)', borderBottom: '1px solid var(--border)' }}>
        <div className="font-mono text-xs font-semibold tracking-widest uppercase" style={{ color: 'var(--accent)' }}>
          DELTA
        </div>
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          aria-label="Toggle navigation"
          style={{ background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', fontSize: 20, padding: '0 4px' }}
        >
          {sidebarOpen ? '\u2715' : '\u2630'}
        </button>
      </div>

      {sidebarOpen && (
        <div className="md:hidden fixed inset-0 z-40" style={{ background: 'rgba(0,0,0,0.6)' }} onClick={() => setSidebarOpen(false)} />
      )}

      <aside
        className={`
          fixed md:static z-50 top-0 left-0 h-full w-56 flex-shrink-0 flex flex-col
          transition-transform duration-200
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}
        `}
        style={{ background: 'var(--surface)', borderRight: '1px solid var(--border)' }}
      >
        <div className="px-4 py-5" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="font-mono text-sm font-semibold tracking-widest uppercase" style={{ color: 'var(--accent)' }}>
            DELTA
          </div>
          <div className="flex items-center gap-1.5 mt-0.5">
            <div className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>
              Prospect System v2.0
            </div>
            {aiActive && (
              <span className="font-mono px-1 py-0.5 leading-none"
                style={{ background: 'var(--gold-bg)', border: '1px solid var(--gold-border)', color: 'var(--gold)', fontSize: 9 }}>
                PRO
              </span>
            )}
          </div>
        </div>

        <nav className="flex-1 py-4" aria-label="Primary" onClick={() => setSidebarOpen(false)}>
          {NAV.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-4 py-2.5 text-sm transition-colors ${isActive ? 'font-medium' : ''}`
              }
              style={({ isActive }) => navStyle(isActive, to)}
            >
              <span className="font-mono text-xs" style={{ color: to === '/watchlist' || to === '/deep-intelligence' ? 'var(--gold)' : 'var(--text-muted)' }}>{icon}</span>
              <span className="flex-1">{label}</span>
              {to === '/settings' && aiActive && (
                <span className="font-mono leading-none" style={{ color: 'var(--gold)', fontSize: 10 }}>{'◆'}</span>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="px-4 py-3 flex items-center justify-between" style={{ borderTop: '1px solid var(--border)' }}>
          <span className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>v2.0</span>
          <ThemeToggle />
        </div>
      </aside>

      <main className="flex-1 overflow-auto pt-12 md:pt-0">
        {children}
      </main>
    </div>
  )
}
