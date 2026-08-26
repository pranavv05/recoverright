import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import ClientHealthTab from './ClientHealthTab'
import { healthScores } from '../test/fixtures'

function renderTab(props = {}) {
  const onSelectClient = vi.fn()
  const utils = render(
    <ClientHealthTab
      healthScores={healthScores}
      loading={false}
      onSelectClient={onSelectClient}
      {...props}
    />
  )
  return { ...utils, onSelectClient }
}

function clientColumn() {
  return screen
    .getAllByRole('row')
    .slice(1)
    .map((row) => row.querySelector('.cell-main').textContent)
}

describe('ClientHealthTab', () => {
  it('shows a loading message rather than an empty table', () => {
    renderTab({ loading: true })
    expect(screen.getByText('Loading client health scores…')).toBeInTheDocument()
  })

  it('tells the user how to populate an empty table', () => {
    renderTab({ healthScores: [] })
    expect(screen.getByText('Run a batch first to generate client health scores.')).toBeInTheDocument()
  })

  it('defaults to worst risk first', () => {
    renderTab()
    expect(clientColumn()).toEqual([
      'Nilgiri Textiles', // critical
      'Konkan Marine Logistics', // watch
      'Sahyadri Precision Auto', // healthy
    ])
  })

  it('renders each tier with its own label and badge class', () => {
    const { container } = renderTab()
    expect(screen.getByText('Critical')).toBeInTheDocument()
    expect(screen.getByText('Watch')).toBeInTheDocument()
    expect(screen.getByText('Healthy')).toBeInTheDocument()
    expect(container.querySelector('.badge-risk-critical')).toBeInTheDocument()
  })

  it('formats outstanding balances as exact rupees, not lakhs', () => {
    renderTab()
    expect(screen.getByText('₹5,20,00,000')).toBeInTheDocument()
  })

  it('labels the payment trend in words', () => {
    renderTab()
    expect(screen.getByText('Deteriorating')).toBeInTheDocument()
    expect(screen.getByText('Improving')).toBeInTheDocument()
  })

  it('flags a working-capital candidate and explains why on hover', () => {
    renderTab()
    const flag = screen.getByText('CANDIDATE')
    expect(flag).toBeInTheDocument()
    expect(flag).toHaveAttribute('title', 'Large stable receivable — working-capital candidate')
    // Exactly one of the three fixture clients carries the flag.
    expect(screen.getAllByText('CANDIDATE')).toHaveLength(1)
  })

  it('sorts by outstanding amount, largest first, when that header is clicked', async () => {
    const user = userEvent.setup()
    renderTab()
    await user.click(screen.getByText(/Outstanding/))
    expect(clientColumn()).toEqual([
      'Sahyadri Precision Auto', // ₹4.5L
      'Nilgiri Textiles', // ₹12.8L
      'Konkan Marine Logistics', // ₹520L
    ])
  })

  it('reverses the sort when the same header is clicked twice', async () => {
    const user = userEvent.setup()
    renderTab()
    await user.click(screen.getByText(/Outstanding/))
    await user.click(screen.getByText(/Outstanding/))
    expect(clientColumn()).toEqual([
      'Konkan Marine Logistics',
      'Nilgiri Textiles',
      'Sahyadri Precision Auto',
    ])
  })

  it('resets to ascending when the sort moves to a different column', async () => {
    const user = userEvent.setup()
    renderTab()
    await user.click(screen.getByText(/Risk Tier/)) // asc (already the key) -> desc
    await user.click(screen.getByText(/Avg Days Overdue/)) // new key -> back to asc
    expect(clientColumn()).toEqual([
      'Konkan Marine Logistics', // 22
      'Sahyadri Precision Auto', // 31
      'Nilgiri Textiles', // 64
    ])
  })

  it('marks the active sort column with a direction arrow', async () => {
    const user = userEvent.setup()
    renderTab()
    expect(screen.getByText(/Risk Tier ▲/)).toBeInTheDocument()
    await user.click(screen.getByText(/Risk Tier/))
    expect(screen.getByText(/Risk Tier ▼/)).toBeInTheDocument()
  })

  it('does not mutate the rows it was handed while sorting', async () => {
    const user = userEvent.setup()
    const original = [...healthScores]
    renderTab()
    await user.click(screen.getByText(/Outstanding/))
    expect(healthScores).toEqual(original)
  })

  it('reports the clicked client to its parent so the invoice tab can filter', async () => {
    const user = userEvent.setup()
    const { onSelectClient } = renderTab()
    await user.click(screen.getByText('Nilgiri Textiles'))
    expect(onSelectClient).toHaveBeenCalledWith('Nilgiri Textiles')
  })

  it('renders an unrecognised tier as its raw value rather than blank', () => {
    renderTab({
      healthScores: [{ ...healthScores[0], risk_tier: 'newly_added_tier', payment_trend: 'unknown' }],
    })
    expect(screen.getByText('newly_added_tier')).toBeInTheDocument()
    expect(screen.getByText('unknown')).toBeInTheDocument()
  })

  it('sorts a row with a missing average overdue last instead of crashing', async () => {
    const user = userEvent.setup()
    renderTab({
      healthScores: [
        { ...healthScores[0], client_name: 'No Data Co', days_overdue_avg: null },
        { ...healthScores[1], client_name: 'Has Data Co', days_overdue_avg: 5 },
      ],
    })
    await user.click(screen.getByText(/Avg Days Overdue/))
    expect(clientColumn()).toEqual(['No Data Co', 'Has Data Co'])
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
