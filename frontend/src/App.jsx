import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import ProspectMatrix from './pages/ProspectMatrix'
import ProspectDetail from './pages/ProspectDetail'
import Watchlist from './pages/Watchlist'
import Settings from './pages/Settings'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/prospects" element={<ProspectMatrix />} />
        <Route path="/prospects/:id" element={<ProspectDetail />} />
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </Layout>
  )
}
