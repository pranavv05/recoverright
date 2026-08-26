import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DiagnosisCell, EvidenceCell, OutcomeCell, PolicyCell } from './InvoiceCells'
import {
  allowedInvoice,
  blockedInvoice,
  failedDiagnosisInvoice,
  humanInvoice,
} from '../test/fixtures'

describe('EvidenceCell', () => {
  it('shows the evidence tier the diagnoser reported', () => {
    render(<EvidenceCell invoice={allowedInvoice} />)
    expect(screen.getByText('Known')).toBeInTheDocument()
  })

  it('renders each tier with its own badge class', () => {
    const { container } = render(<EvidenceCell invoice={humanInvoice} />)
    expect(screen.getByText('Strong Inference')).toBeInTheDocument()
    expect(container.querySelector('.badge-evidence-strong_inference')).toBeInTheDocument()
  })

  it('shows N/A rather than a fabricated tier when there is no diagnosis', () => {
    // A failed diagnosis must never read as evidence the system does not have.
    const { container } = render(<EvidenceCell invoice={failedDiagnosisInvoice} />)
    expect(screen.getByText('N/A')).toBeInTheDocument()
    expect(container.querySelector('.badge-evidence-none')).toBeInTheDocument()
  })

  it('quotes the client note beneath the tier', () => {
    render(<EvidenceCell invoice={allowedInvoice} />)
    expect(
      screen.getByText(/The payment link you sent expired before I could use it\./)
    ).toBeInTheDocument()
  })

  it('truncates a long note at 60 characters with an ellipsis', () => {
    render(<EvidenceCell invoice={blockedInvoice} />)
    const expected = blockedInvoice.client_notes.slice(0, 60)
    const node = screen.getByText(new RegExp(expected.slice(0, 40)))
    expect(node.textContent).toContain('…')
    expect(node.textContent).not.toContain('warehouse')
  })

  it('renders no note element at all when client_notes is empty', () => {
    const { container } = render(<EvidenceCell invoice={humanInvoice} />)
    expect(container.querySelector('.cell-sub')).toBeNull()
  })
})

describe('DiagnosisCell', () => {
  it('shows the diagnosed cause and the evidence it cited', () => {
    render(<DiagnosisCell invoice={allowedInvoice} />)
    expect(screen.getByText('payment_link_expired')).toBeInTheDocument()
    expect(screen.getByText('Client states the link expired')).toBeInTheDocument()
  })

  it('says the diagnosis failed rather than showing an empty cause', () => {
    render(<DiagnosisCell invoice={failedDiagnosisInvoice} />)
    expect(screen.getByText('diagnosis failed')).toBeInTheDocument()
  })

  it('distinguishes "not diagnosed yet" from "diagnosis failed"', () => {
    render(<DiagnosisCell invoice={{ ...allowedInvoice, diagnosis: null, diagnosis_failed: false }} />)
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText('diagnosis failed')).toBeNull()
  })

  it('omits the evidence line when the model cited nothing', () => {
    const { container } = render(<DiagnosisCell invoice={humanInvoice} />)
    expect(container.querySelector('.cell-sub')).toBeNull()
  })
})

describe('PolicyCell', () => {
  it.each([
    [allowedInvoice, 'ALLOW', 'badge-allow'],
    [blockedInvoice, 'BLOCK', 'badge-block'],
    [humanInvoice, 'REQUIRE_HUMAN', 'badge-human'],
  ])('renders %#: the decision with its own badge class', (invoice, decision, cls) => {
    const { container } = render(<PolicyCell invoice={invoice} />)
    expect(screen.getByText(decision)).toBeInTheDocument()
    expect(container.querySelector(`.${cls}`)).toBeInTheDocument()
  })

  it('shows the first policy reason under the badge', () => {
    render(<PolicyCell invoice={blockedInvoice} />)
    expect(screen.getByText('dispute_detected')).toBeInTheDocument()
  })

  it('shows N/A when no policy decision was recorded', () => {
    render(<PolicyCell invoice={{ ...allowedInvoice, policy_decision: null, policy_reasons: [] }} />)
    expect(screen.getByText('N/A')).toBeInTheDocument()
  })
})

describe('OutcomeCell', () => {
  it('renders the human label for a recorded execution status', () => {
    render(<OutcomeCell invoice={allowedInvoice} />)
    expect(screen.getByText('Payment link created')).toBeInTheDocument()
  })

  it('does not describe a block as an escalation', () => {
    render(<OutcomeCell invoice={blockedInvoice} />)
    expect(screen.getByText('Blocked')).toBeInTheDocument()
    expect(screen.queryByText(/Escalated/)).toBeNull()
  })

  it('renders an em dash when the invoice has no execution record yet', () => {
    render(<OutcomeCell invoice={{ ...allowedInvoice, execution_status: null }} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('falls back to the raw status for a token the frontend has no label for', () => {
    // Surfacing an unknown backend status is deliberate: a silent blank would
    // hide a new outcome the dashboard has not caught up with.
    render(<OutcomeCell invoice={{ ...allowedInvoice, execution_status: 'brand_new_status' }} />)
    expect(screen.getByText('brand_new_status')).toBeInTheDocument()
  })
})
