import { describe, expect, it } from 'vitest'
import { findLastEvent, formatTime, humanize, lakhs, recoveryTone, rupees } from './format'

// Money on this dashboard is integer paise all the way from Postgres. The
// point of these tests is that the display layer never reintroduces the
// float that api/main.py went out of its way to keep out of the payload.
describe('rupees', () => {
  it('renders whole rupees without a paise fraction', () => {
    expect(rupees(4500000)).toBe('₹45,000')
  })

  it('renders a paise remainder with exactly two digits', () => {
    expect(rupees(4500007)).toBe('₹45,000.07')
    expect(rupees(4500070)).toBe('₹45,000.70')
  })

  it('groups in the Indian numbering system, not the western one', () => {
    // 1,23,45,678.90 — lakh/crore grouping, not 12,345,678.90
    expect(rupees(1234567890)).toBe('₹1,23,45,678.90')
  })

  it('is exact for a value a float division would round', () => {
    // 1234567890123 / 100 is not exactly representable as a double; integer
    // arithmetic keeps the last paise honest.
    expect(rupees(1234567890123)).toBe('₹12,34,56,78,901.23')
  })

  it('puts the sign outside the rupee symbol for a negative balance', () => {
    expect(rupees(-4500000)).toBe('-₹45,000')
    expect(rupees(-1)).toBe('-₹0.01')
  })

  it('renders zero as a real zero, not an em dash', () => {
    expect(rupees(0)).toBe('₹0')
  })

  it('renders a missing value as an em dash rather than ₹0', () => {
    // A null outstanding balance means "not recorded" and must not be
    // displayed as a settled account.
    expect(rupees(null)).toBe('—')
    expect(rupees(undefined)).toBe('—')
  })
})

describe('lakhs', () => {
  it('converts paise to a one-decimal lakh figure', () => {
    expect(lakhs(45000000)).toBe('₹4.5L')
    expect(lakhs(5200000000)).toBe('₹520.0L')
  })

  it('treats a missing value as zero, because the counter tile needs a number', () => {
    expect(lakhs(null)).toBe('₹0.0L')
    expect(lakhs(undefined)).toBe('₹0.0L')
    expect(lakhs(0)).toBe('₹0.0L')
  })

  it('rounds rather than truncating at the first decimal', () => {
    expect(lakhs(1690000)).toBe('₹0.2L')
  })
})

describe('formatTime', () => {
  it('returns an empty string for a missing timestamp', () => {
    expect(formatTime(null)).toBe('')
    expect(formatTime(undefined)).toBe('')
    expect(formatTime('')).toBe('')
  })

  it('renders day, month and a second-resolution time in local time', () => {
    // Built in local time on purpose. Asserting against a UTC ISO string
    // would make this test pass or fail depending on the machine's timezone.
    const localNoon = new Date(2026, 7, 20, 10, 16, 3).getTime()
    const out = formatTime(localNoon)
    expect(out).toContain('20')
    expect(out).toContain('Aug')
    expect(out).toMatch(/10:16:03/)
  })

  it('accepts the ISO strings the audit endpoint actually returns', () => {
    expect(formatTime('2026-08-20T10:16:03Z')).toMatch(/\d{2}:\d{2}:\d{2}/)
  })
})

describe('humanize', () => {
  it('turns a snake_case backend token into a sentence-cased label', () => {
    expect(humanize('cash_flow_delay')).toBe('Cash flow delay')
    expect(humanize('dispute_detected')).toBe('Dispute detected')
  })

  it('leaves a single word capitalised and otherwise intact', () => {
    expect(humanize('dispute')).toBe('Dispute')
  })

  it('renders an em dash for an absent value', () => {
    expect(humanize('')).toBe('—')
    expect(humanize(null)).toBe('—')
    expect(humanize(undefined)).toBe('—')
  })
})

describe('findLastEvent', () => {
  const trail = [
    { event_type: 'diagnosis', id: 1 },
    { event_type: 'policy_decision', id: 2 },
    { event_type: 'diagnosis', id: 3 },
  ]

  it('returns the most recent matching event, not the first', () => {
    // An invoice can be re-processed; the dashboard must show the current
    // diagnosis, not the superseded one.
    expect(findLastEvent(trail, 'diagnosis').id).toBe(3)
  })

  it('returns null when the event type never occurred', () => {
    expect(findLastEvent(trail, 'execution_complete')).toBeNull()
  })

  it('returns null for an empty trail', () => {
    expect(findLastEvent([], 'diagnosis')).toBeNull()
  })
})

describe('recoveryTone', () => {
  it('classifies rates into the three display bands', () => {
    expect(recoveryTone(100)).toBe('recovery-high')
    expect(recoveryTone(71)).toBe('recovery-high')
    expect(recoveryTone(70)).toBe('recovery-medium')
    expect(recoveryTone(30)).toBe('recovery-medium')
    expect(recoveryTone(29)).toBe('recovery-low')
    expect(recoveryTone(0)).toBe('recovery-low')
  })
})
