import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ReasoningChain from './ReasoningChain'
import { allowedTrail, blockedTrail } from '../test/fixtures'

describe('ReasoningChain', () => {
  it('renders all five thoughts in order', () => {
    render(<ReasoningChain trail={blockedTrail} />)
    for (const n of [1, 2, 3, 4, 5]) {
      expect(screen.getByText(new RegExp(`^Thought ${n} —`))).toBeInTheDocument()
    }
    expect(
      screen.getByText('The note raises a billing dispute in explicit terms.')
    ).toBeInTheDocument()
  })

  it('renders the signals call 1 extracted', () => {
    const { container } = render(<ReasoningChain trail={blockedTrail} />)
    expect(container.textContent).toContain('responsive')
    expect(container.textContent).toContain('consistently_on_time')
    expect(container.textContent).toContain('dispute')
    expect(container.textContent).toContain('quantities billed do not match delivery')
  })

  it('renders evidence availability as yes/no rather than a raw boolean', () => {
    const { container } = render(<ReasoningChain trail={blockedTrail} />)
    expect(container.textContent).toContain('Evidence available: yes')
  })

  it('renders "no" when the diagnoser found no evidence', () => {
    const trail = blockedTrail.map((e) =>
      e.event_type === 'reasoning_trace'
        ? {
            ...e,
            diagnosis: { ...e.diagnosis, signals: { ...e.diagnosis.signals, evidence_available: false } },
          }
        : e
    )
    const { container } = render(<ReasoningChain trail={trail} />)
    expect(container.textContent).toContain('Evidence available: no')
  })

  it('tints the chain with the decision the reasoning led to', () => {
    const { container } = render(<ReasoningChain trail={blockedTrail} />)
    expect(container.querySelectorAll('.reasoning-tone-block')).toHaveLength(5)
  })

  it('uses a neutral tone when no policy decision was recorded', () => {
    const trail = blockedTrail.filter((e) => e.event_type !== 'policy_decision')
    const { container } = render(<ReasoningChain trail={trail} />)
    expect(container.querySelectorAll('.reasoning-tone-none')).toHaveLength(5)
  })

  it('renders nothing when the trail has no reasoning trace', () => {
    // An ALLOW written before the ReAct refactor has no reasoning_trace
    // event; the panel must simply omit the section.
    const { container } = render(<ReasoningChain trail={allowedTrail} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the reasoning trace carries no diagnosis payload', () => {
    const { container } = render(
      <ReasoningChain trail={[{ event_type: 'reasoning_trace', diagnosis: null }]} />
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders em dashes for thoughts the model left out, not "undefined"', () => {
    const trail = [
      {
        event_type: 'reasoning_trace',
        diagnosis: { thought_1_engagement: 'Only the first thought came back.' },
      },
    ]
    const { container } = render(<ReasoningChain trail={trail} />)
    expect(container.textContent).not.toContain('undefined')
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(4)
  })

  it('omits the trailing content detail when the model supplied none', () => {
    const trail = blockedTrail.map((e) =>
      e.event_type === 'reasoning_trace'
        ? {
            ...e,
            diagnosis: { ...e.diagnosis, signals: { ...e.diagnosis.signals, content_detail: '' } },
          }
        : e
    )
    const { container } = render(<ReasoningChain trail={trail} />)
    expect(container.textContent).not.toContain('quantities billed do not match delivery')
    expect(container.textContent).not.toContain('— undefined')
  })
})
