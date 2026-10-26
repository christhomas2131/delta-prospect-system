import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { LegacyProspectRedirect } from '../App'
import ScoreBreakdown from '../components/ScoreBreakdown'
import { freshnessLabel } from '../lib/format'
import { backoffDelay } from '../lib/usePoll'

vi.mock('../components/Layout', () => ({ default: ({ children }) => <div>{children}</div> }))

describe('legacy redirect', () => {
  it('keeps the company id when redirecting /prospects/:id', () => {
    render(
      <MemoryRouter initialEntries={['/prospects/abc-123']}>
        <Routes>
          <Route path="/prospects/:id" element={<LegacyProspectRedirect />} />
          <Route path="/deep-intelligence/:id" element={<div>detail for {'abc-123'}</div>} />
          <Route path="/deep-intelligence" element={<div>landing</div>} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('detail for abc-123')).toBeInTheDocument()
    expect(screen.queryByText('landing')).toBeNull()
  })
})

describe('ScoreBreakdown', () => {
  it('shows policy, freshness, pillar contributions and the prize range with assumptions', () => {
    render(<ScoreBreakdown prospect={{
      prospect_score: 12.5,
      score_policy_version: 'v1',
      score_breakdown: {
        displayed_policy: 'v1', score_v1: 12.5, score_v2: 9.1, fit_factor: 1.0, likelihood: 5,
        signals_used: 3, signals_excluded: 1, days_since_latest_evidence: 400,
        pillars: { production: { contribution: 8.2, signals: 2, strong: 1, latest_evidence: '2025-09-01' } },
      },
      prize_breakdown: {
        currency: 'AUD', range: { low: 500000, base: 1000000, high: 2000000 },
        fee_estimate: { low: 80000, high: 200000 }, confidence_label: 'High confidence',
        assumptions: ['Directional heuristics only'],
      },
    }} />)
    expect(screen.getByText(/policy v1/)).toBeInTheDocument()
    expect(screen.getByText(/400d old — stale/)).toBeInTheDocument()
    expect(screen.getByText('Production')).toBeInTheDocument()
    expect(screen.getByText(/1 excluded/)).toBeInTheDocument()
    expect(screen.getByText(/\$1\.0M/)).toBeInTheDocument()
    expect(screen.getByText('Directional heuristics only')).toBeInTheDocument()
  })

  it('labels missing dates as unknown rather than fresh', () => {
    expect(freshnessLabel(null).text).toMatch(/no dated evidence/)
    expect(freshnessLabel(10).text).toBe('10d old')
  })
})

describe('polling backoff', () => {
  it('doubles the delay on consecutive failures and caps it', () => {
    expect(backoffDelay(3000, 0)).toBe(3000)
    expect(backoffDelay(3000, 1)).toBe(6000)
    expect(backoffDelay(3000, 5)).toBe(30000)
  })
})
