import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import DetailPanel from './DetailPanel'
import { blockedInvoice, blockedTrail, allowedTrail, allowedInvoice } from '../test/fixtures'

describe('DetailPanel', () => {
  it('renders nothing when no invoice is open', () => {
    const { container } = render(<DetailPanel invoiceId={null} onClose={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the invoice id, client and outstanding balance in the header', () => {
    render(
      <DetailPanel
        invoiceId={blockedInvoice.invoice_id}
        invoice={blockedInvoice}
        trail={blockedTrail}
        loading={false}
        onClose={vi.fn()}
      />
    )
    expect(screen.getByText('INV-1042')).toBeInTheDocument()
    expect(screen.getByText(/Nilgiri Textiles · ₹12,80,000 outstanding/)).toBeInTheDocument()
  })

  it('shows the header before the invoice row has loaded', () => {
    // Clicking a row sets the id immediately; the row object may not be
    // resolved yet. The panel must not blank out or crash in that window.
    render(<DetailPanel invoiceId="INV-1042" invoice={undefined} loading onClose={vi.fn()} />)
    expect(screen.getByText('INV-1042')).toBeInTheDocument()
    expect(screen.getByText('Loading audit trail…')).toBeInTheDocument()
  })

  it('renders no trail while it is still loading', () => {
    render(<DetailPanel invoiceId="INV-1042" trail={blockedTrail} loading onClose={vi.fn()} />)
    expect(screen.queryByText('AUDIT TRAIL')).toBeNull()
  })

  it('renders every audit event in the trail', () => {
    render(
      <DetailPanel
        invoiceId="INV-1042"
        invoice={blockedInvoice}
        trail={blockedTrail}
        loading={false}
        onClose={vi.fn()}
      />
    )
    expect(screen.getByText('AUDIT TRAIL')).toBeInTheDocument()
    expect(screen.getByText('reasoning_trace')).toBeInTheDocument()
    expect(screen.getByText('diagnosis')).toBeInTheDocument()
    expect(screen.getByText('policy_decision')).toBeInTheDocument()
    expect(screen.getByText('execution_complete')).toBeInTheDocument()
  })

  it('shows the reasons and the blocked actions the engine recorded', () => {
    const { container } = render(
      <DetailPanel
        invoiceId="INV-1042"
        invoice={blockedInvoice}
        trail={blockedTrail}
        loading={false}
        onClose={vi.fn()}
      />
    )
    expect(container.textContent).toContain('reasons: dispute_detected; automated_chasing_forbidden')
    expect(container.textContent).toContain('blocked: send_reminder, create_payment_link')
  })

  it('shows the diagnosed cause, the tier and the cited evidence together', () => {
    const { container } = render(
      <DetailPanel
        invoiceId="INV-1042"
        invoice={blockedInvoice}
        trail={blockedTrail}
        loading={false}
        onClose={vi.fn()}
      />
    )
    expect(container.textContent).toContain('cause: dispute · evidence: known')
    expect(container.textContent).toContain('Client disputes the billed quantities')
  })

  it('serialises the execution result so the outcome is auditable', () => {
    const { container } = render(
      <DetailPanel
        invoiceId="INV-1001"
        invoice={allowedInvoice}
        trail={allowedTrail}
        loading={false}
        onClose={vi.fn()}
      />
    )
    expect(container.textContent).toContain('plink_test_123')
    expect(container.textContent).toContain('link_created')
  })

  it('includes the WHY NOT explanation for a refused invoice', () => {
    render(
      <DetailPanel
        invoiceId="INV-1042"
        invoice={blockedInvoice}
        trail={blockedTrail}
        loading={false}
        onClose={vi.fn()}
      />
    )
    expect(screen.getByText('WHY NOT')).toBeInTheDocument()
    expect(screen.getByText('REASONING CHAIN')).toBeInTheDocument()
  })

  it('omits WHY NOT for an allowed invoice but still shows its trail', () => {
    render(
      <DetailPanel
        invoiceId="INV-1001"
        invoice={allowedInvoice}
        trail={allowedTrail}
        loading={false}
        onClose={vi.fn()}
      />
    )
    expect(screen.queryByText('WHY NOT')).toBeNull()
    expect(screen.getByText('AUDIT TRAIL')).toBeInTheDocument()
  })

  it('closes on the close button', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<DetailPanel invoiceId="INV-1042" trail={blockedTrail} loading={false} onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: 'Close audit trail' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes when the overlay behind the modal is clicked', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    const { container } = render(
      <DetailPanel invoiceId="INV-1042" trail={blockedTrail} loading={false} onClose={onClose} />
    )
    await user.click(container.querySelector('.modal-overlay'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not close when the modal content itself is clicked', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<DetailPanel invoiceId="INV-1042" trail={blockedTrail} loading={false} onClose={onClose} />)
    await user.click(screen.getByText('AUDIT TRAIL'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes when Escape is pressed', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<DetailPanel invoiceId="INV-1042" trail={blockedTrail} loading={false} onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('renders an empty trail without claiming anything happened', () => {
    render(<DetailPanel invoiceId="INV-1042" trail={[]} loading={false} onClose={vi.fn()} />)
    expect(screen.getByText('AUDIT TRAIL')).toBeInTheDocument()
    expect(screen.queryByText('WHY NOT')).toBeNull()
    expect(screen.queryByText('REASONING CHAIN')).toBeNull()
  })
})
