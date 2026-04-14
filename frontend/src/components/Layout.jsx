import { NavLink, useLocation } from 'react-router-dom'
import { useEffect, useState } from 'react'

const NAV = [
  { to: '/', label: 'Dashboard', icon: '◈' },
  { to: '/leads', label: 'Lead Matrix', icon: '▦' },
  { to: '/deep-intelligence', label: 'Deep Intelligence', icon: '◆' },
  { to: '/watchlist', label: 'Watchlist', icon: '★' },
  { to: '/settings', label: 'Settings', icon: '⚙' },
]

export default function Layout({ children }) {
  const [aiActive, setAiActive] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const location = useLocation()

  useEffect(() => {
    fetch('/api/settings/api-key/status')
      .then(r => r.json())
      .then(d => setAiActive(d.valid))
      .catch(() => {})
  }, [])

  // Close sidebar on navigation (mobile)
  useEffect(() => {
    setSidebarOpen(false)
  }, [location.pathname])

  return (
    <div className="flex min-h-screen" style={{ background: '#0a0c0f' }}>
      {/* Mobile header bar */}
      <div className="md:hidden fixed top-0 left-0 right-0 z-30 flex items-center justify-between px-4 py-3"
        style={{ background: '#0d1017', borderBottom: '1px solid #1e2530' }}>
        <div className="font-mono text-xs font-semibold tracking-widest uppercase" style={{ color: '#1e6fd4' }}>
          DELTA
        </div>
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          style={{ background: 'none', border: 'none', color: '#8fa3bf', cursor: 'pointer', fontSize: 20, padding: '0 4px' }}
        >
          {sidebarOpen ? '\u2715' : '\u2630'}
        </button>
      </div>

      {/* Sidebar overlay (mobile) */}
      {sidebarOpen && (
        <div
          className="md:hidden fixed inset-0 z-40"
          style={{ background: 'rgba(0,0,0,0.6)' }}
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`
          fixed md:static z-50 top-0 left-0 h-full w-52 flex-shrink-0 flex flex-col
          transition-transform duration-200
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}
        `}
        style={{ background: '#0d1017', borderRight: '1px solid #1e2530' }}
      >
        {/* Logo */}
        <div className="px-4 py-5" style={{ borderBottom: '1px solid #1e2530' }}>
          <div className="font-mono text-xs font-semibold tracking-widest uppercase" style={{ color: '#1e6fd4' }}>
            DELTA
          </div>
          <div className="flex items-center gap-1.5">
            <div className="font-mono text-xs" style={{ color: '#4a5a70' }}>
              Prospect System v2.0
            </div>
            {aiActive && (
              <span className="font-mono text-xs px-1 py-0.5 leading-none"
                style={{ background: '#1a1508', border: '1px solid #8B7120', color: '#D4AF37', fontSize: 9 }}>
                PRO
              </span>
            )}
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 py-4">
          {NAV.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-2 px-4 py-2.5 text-sm transition-all ${
                  isActive ? 'font-medium' : 'hover:opacity-100'
                }`
              }
              style={({ isActive }) => ({
                color: isActive ? '#e2e8f0'
                  : to === '/watchlist' ? '#8B7120'
                  : to === '/deep-intelligence' ? '#D4AF37'
                  : '#8fa3bf',
                background: isActive ? '#161b24' : 'transparent',
                borderLeft: isActive
                  ? `2px solid ${to === '/watchlist' ? '#D4AF37' : to === '/deep-intelligence' ? '#D4AF37' : '#1e6fd4'}`
                  : '2px solid transparent',
              })}
            >
              <span className="font-mono text-xs"
                style={to === '/watchlist' ? { color: '#D4AF37' } : to === '/deep-intelligence' ? { color: '#D4AF37' } : {}}
              >{icon}</span>
              <span className="flex-1">{label}</span>
              {to === '/settings' && aiActive && (
                <span className="font-mono leading-none" style={{ color: '#D4AF37', fontSize: 10 }}>&#9670;</span>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Footer */}
        <div className="px-4 py-3 font-mono text-xs" style={{ color: '#2d3a4d', borderTop: '1px solid #1e2530' }}>
          v2.0
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto pt-12 md:pt-0">
        {children}
      </main>
    </div>
  )
}
