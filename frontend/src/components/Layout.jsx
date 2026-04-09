import { NavLink } from 'react-router-dom'
import { useEffect, useState } from 'react'

const NAV = [
  { to: '/', label: 'Dashboard', icon: '◈' },
  { to: '/watchlist', label: 'Watchlist', icon: '★' },
  { to: '/prospects', label: 'Prospect Matrix', icon: '▦' },
  { to: '/settings', label: 'Settings', icon: '⚙' },
]

export default function Layout({ children }) {
  const [aiActive, setAiActive] = useState(false)

  useEffect(() => {
    fetch('/api/settings/api-key/status')
      .then(r => r.json())
      .then(d => setAiActive(d.valid))
      .catch(() => {})
  }, [])

  return (
    <div className="flex min-h-screen" style={{ background: '#0a0c0f' }}>
      {/* Sidebar */}
      <aside className="w-52 flex-shrink-0 flex flex-col" style={{ background: '#0d1017', borderRight: '1px solid #1e2530' }}>
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
                color: isActive ? '#e2e8f0' : to === '/watchlist' ? '#8B7120' : '#8fa3bf',
                background: isActive ? '#161b24' : 'transparent',
                borderLeft: isActive
                  ? `2px solid ${to === '/watchlist' ? '#D4AF37' : '#1e6fd4'}`
                  : '2px solid transparent',
              })}
            >
              <span className="font-mono text-xs" style={to === '/watchlist' ? { color: '#D4AF37' } : {}}>{icon}</span>
              <span className="flex-1">{label}</span>
              {to === '/settings' && aiActive && (
                <span className="font-mono leading-none" style={{ color: '#D4AF37', fontSize: 10 }}>◆</span>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Footer */}
        <div className="px-4 py-3 font-mono text-xs" style={{ color: '#2d3a4d', borderTop: '1px solid #1e2530' }}>
          {aiActive ? (
            <span style={{ color: '#8B7120' }}>◆ AI Enhanced</span>
          ) : (
            'ASX Free Edition'
          )}
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto">
        {children}
      </main>
    </div>
  )
}
