import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import WhyNot from './WhyNot'
import { allowedTrail, blockedTrail } from '../test/fixtures'

// WhyNot is the dashboard's whole safety argument in one paragraph: it shows
// that the model wanted one thing and the deterministic engine did another.
// If it renders the model's recommendation as if it were the outcome, the
// dashboard misrepresents what the system did.
describe('WhyNot', () => {
  it('names the action the agent recommended', () => {
    render(<WhyNot trail={blockedTrail} />)
    expect(screen.getByText('send_reminder')).toBeInTheDocument()
  })

  it('says the engine blocked it, and lists every reason', () => {
    const { container } = render(<WhyNot trail={blockedTrail} />)
    expect(screen.getByText('blocked')).toBeInTheDocument()
    expect(container.textContent).toContain('dispute_detected; automated_chasing_forbidden')
  })

  it('says "escalated", not "blocked", for a REQUIRE_HUMAN decision', () => {
    const trail = blockedTrail.map((e) =>
      e.event_type === 'policy_decision'
        ? { ...e, decision: 'REQUIRE_HUMAN', reasons: ['strategic_account_requires_approval'] }
        : e
    )
    render(<WhyNot trail={trail} />)
    expect(screen.getByText('escalated')).toBeInTheDocument()
    expect(screen.queryByText('blocked')).toBeNull()
  })

  it('renders nothing for an ALLOW — there is no refusal to explain', () => {
    const { container } = render(<WhyNot trail={allowedTrail} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when no policy decision was recorded', () => {
    const trail = blockedTrail.filter((e) => e.event_type !== 'policy_decision')
    const { container } = render(<WhyNot trail={trail} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for an empty trail', () => {
    const { container } = render(<WhyNot trail={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('explains the refusal even when the diagnosis event is missing', () => {
    // A fail-closed escalation has a policy decision but no diagnosis. The
    // refusal must still be explained rather than the panel going blank.
    const trail = blockedTrail.filter((e) => e.event_type !== 'diagnosis')
    render(<WhyNot trail={trail} />)
    expect(screen.getByText('an action')).toBeInTheDocument()
    expect(screen.getByText('blocked')).toBeInTheDocument()
  })

  it('says so explicitly when the engine recorded no reason', () => {
    const trail = blockedTrail.map((e) =>
      e.event_type === 'policy_decision' ? { ...e, reasons: [] } : e
    )
    const { container } = render(<WhyNot trail={trail} />)
    expect(container.textContent).toContain('no reason recorded')
  })

  it('reads the latest policy decision when an invoice was re-processed', () => {
    const trail = [
      ...blockedTrail,
      {
        event_type: 'policy_decision',
        created_at: '2026-08-21T09:00:00Z',
        decision: 'ALLOW',
        reasons: ['dispute_resolved'],
      },
    ]
    const { container } = render(<WhyNot trail={trail} />)
    expect(container).toBeEmptyDOMElement()
  })
})
