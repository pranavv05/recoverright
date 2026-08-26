import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import AnalyticsTab from './AnalyticsTab'
import { analytics, emptyAnalytics } from '../test/fixtures'

describe('AnalyticsTab', () => {
  it('shows a loading message rather than empty panels', () => {
    render(<AnalyticsTab analytics={null} loading />)
    expect(screen.getByText('Loading analytics…')).toBeInTheDocument()
  })

  it('tells the user how to populate an empty batch', () => {
    render(<AnalyticsTab analytics={emptyAnalytics} loading={false} />)
    expect(screen.getByText('Run a batch to generate analytics.')).toBeInTheDocument()
  })

  it('does not crash when the analytics payload is missing entirely', () => {
    render(<AnalyticsTab analytics={null} loading={false} />)
    expect(screen.getByText('Run a batch to generate analytics.')).toBeInTheDocument()
  })

  it('summarises the batch in the counter row', () => {
    render(<AnalyticsTab analytics={analytics} loading={false} />)
    expect(screen.getByText('Total Processed')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('₹547.4L')).toBeInTheDocument() // outstanding
    expect(screen.getByText('₹4.5L')).toBeInTheDocument() // recovered
    expect(screen.getByText('8.2%')).toBeInTheDocument()
  })

  it('tints the recovery rate green only when something was actually recovered', () => {
    const { container, rerender } = render(<AnalyticsTab analytics={analytics} loading={false} />)
    expect(container.querySelectorAll('.tone-green')).toHaveLength(1)

    rerender(
      <AnalyticsTab
        analytics={{
          ...analytics,
          batch_summary: { ...analytics.batch_summary, overall_recovery_rate: 0 },
        }}
        loading={false}
      />
    )
    expect(container.querySelectorAll('.tone-green')).toHaveLength(0)
  })

  it('humanises the snake_case scenario names the backend emits', () => {
    render(<AnalyticsTab analytics={analytics} loading={false} />)
    // Scoped to the scenario panel: the same token also names a diagnosed
    // cause in the fourth panel, and both are humanised the same way.
    const panel = screen.getByText('Recovery by Scenario').closest('.analytics-panel')
    expect(within(panel).getByText('Payment link expired')).toBeInTheDocument()
    expect(within(panel).getByText('Cash flow delay')).toBeInTheDocument()
    expect(screen.queryByText('payment_link_expired')).toBeNull()
  })

  it('humanises policy rule names too', () => {
    render(<AnalyticsTab analytics={analytics} loading={false} />)
    expect(screen.getByText('Dispute detected')).toBeInTheDocument()
    expect(screen.getByText('Strategic account requires approval')).toBeInTheDocument()
  })

  it('ranks the policy rules in the order the backend returned them', () => {
    render(<AnalyticsTab analytics={analytics} loading={false} />)
    const ranks = screen.getAllByText(/^[12]$/, { selector: '.rank-cell' })
    expect(ranks.map((n) => n.textContent)).toEqual(['1', '2'])
  })

  it('draws each recovery bar to the width of its rate', () => {
    const { container } = render(<AnalyticsTab analytics={analytics} loading={false} />)
    const widths = [...container.querySelectorAll('.recovery-bar-fill')].map((el) => el.style.width)
    expect(widths).toEqual(['100%', '45%', '0%'])
  })

  it('bands the recovery bars by rate', () => {
    const { container } = render(<AnalyticsTab analytics={analytics} loading={false} />)
    expect(container.querySelector('.recovery-high')).toBeInTheDocument()
    expect(container.querySelector('.recovery-medium')).toBeInTheDocument()
    expect(container.querySelector('.recovery-low')).toBeInTheDocument()
  })

  it('clamps an out-of-range recovery rate to a drawable width', () => {
    const { container } = render(
      <AnalyticsTab
        analytics={{
          ...analytics,
          recovery_by_scenario: [
            { scenario_type: 'over', recovery_rate: 140, total_invoices: 1 },
            { scenario_type: 'under', recovery_rate: -20, total_invoices: 1 },
          ],
        }}
        loading={false}
      />
    )
    const widths = [...container.querySelectorAll('.recovery-bar-fill')].map((el) => el.style.width)
    expect(widths).toEqual(['100%', '0%'])
  })

  it('lists multi-invoice clients with every invoice id', () => {
    render(<AnalyticsTab analytics={analytics} loading={false} />)
    expect(screen.getByText('INV-1042, INV-1043')).toBeInTheDocument()
  })

  it('reports average overdue days by diagnosed cause', () => {
    render(<AnalyticsTab analytics={analytics} loading={false} />)
    const panel = screen.getByText('Average Overdue by Cause').closest('.analytics-panel')
    expect(within(panel).getByText('Dispute')).toBeInTheDocument()
    expect(within(panel).getByText('64')).toBeInTheDocument()
  })

  it('shows a per-panel empty state rather than hiding the panel', () => {
    // The panels are the shape of the analysis. A missing panel reads as a
    // feature that does not exist; an empty one reads as no data yet.
    render(
      <AnalyticsTab
        analytics={{
          ...analytics,
          recovery_by_scenario: [],
          top_policy_rules: [],
          multi_invoice_clients: [],
          avg_overdue_by_cause: [],
        }}
        loading={false}
      />
    )
    expect(screen.getByText('Recovery by Scenario')).toBeInTheDocument()
    expect(screen.getByText('No scenario data yet.')).toBeInTheDocument()
    expect(screen.getByText('No policy decisions yet.')).toBeInTheDocument()
    expect(screen.getByText('No clients with 2+ invoices in this batch.')).toBeInTheDocument()
    expect(screen.getByText('No diagnosis data yet.')).toBeInTheDocument()
  })
})
