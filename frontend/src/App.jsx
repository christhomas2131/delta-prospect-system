import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import LeadMatrix from './pages/LeadMatrix'
import DeepIntelligence from './pages/DeepIntelligence'
import Watchlist from './pages/Watchlist'
import Settings from './pages/Settings'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/leads" element={<LeadMatrix />} />
        <Route path="/deep-intelligence" element={<DeepIntelligence />} />
        <Route path="/deep-intelligence/:id" element={<DeepIntelligence />} />
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/settings" element={<Settings />} />
        {/* Redirect old routes */}
        <Route path="/prospects" element={<Navigate to="/leads" replace />} />
        <Route path="/prospects/:id" element={<Navigate to="/deep-intelligence" replace />} />
      </Routes>
    </Layout>
  )
}
