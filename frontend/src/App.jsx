import { Routes, Route, Navigate, useParams } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import LeadMatrix from './pages/LeadMatrix'
import DeepIntelligence from './pages/DeepIntelligence'
import Watchlist from './pages/Watchlist'
import Settings from './pages/Settings'
import ReviewQueue from './pages/ReviewQueue'
import Ops from './pages/Ops'

// Old /prospects/:id links must land on the same company, not the landing page.
export function LegacyProspectRedirect() {
  const { id } = useParams()
  return <Navigate to={id ? `/deep-intelligence/${id}` : '/deep-intelligence'} replace />
}

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/leads" element={<LeadMatrix />} />
        <Route path="/review" element={<ReviewQueue />} />
        <Route path="/deep-intelligence" element={<DeepIntelligence />} />
        <Route path="/deep-intelligence/:id" element={<DeepIntelligence />} />
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/ops" element={<Ops />} />
        <Route path="/settings" element={<Settings />} />
        {/* Redirect old routes */}
        <Route path="/prospects" element={<Navigate to="/leads" replace />} />
        <Route path="/prospects/:id" element={<LegacyProspectRedirect />} />
      </Routes>
    </Layout>
  )
}
