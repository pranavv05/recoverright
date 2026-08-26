// Pure display formatters. No React, no network — everything here is a
// function of its arguments alone, which is why they live outside the
// components that use them and are unit-tested directly.

// Paise are integers from the API. Split with integer arithmetic rather than
// dividing by 100 and formatting, so the rupee figure is never a rounded float.
export function rupees(paise) {
  if (paise === null || paise === undefined) return '—'
  const negative = paise < 0
  const abs = Math.abs(paise)
  const whole = Math.trunc(abs / 100)
  const fraction = abs % 100
  const formatted = whole.toLocaleString('en-IN')
  const withPaise = fraction ? `${formatted}.${String(fraction).padStart(2, '0')}` : formatted
  return `${negative ? '-₹' : '₹'}${withPaise}`
}

// Counter tiles show portfolio-scale money, where a full rupee figure is
// unreadable. One decimal place of a lakh is the summary unit; `rupees()`
// stays the exact one for per-invoice figures.
export function lakhs(paise) {
  const value = (paise || 0) / 100 / 100000
  return `₹${value.toFixed(1)}L`
}

export function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

// "cash_flow_delay" -> "Cash flow delay". Used for policy reasons, scenario
// types, and diagnosed causes alike — all three are underscore-joined
// machine strings from the backend with no separate display label.
export function humanize(text) {
  if (!text) return '—'
  const spaced = text.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

// The audit trail arrives in chronological order and an invoice can be
// re-processed, so "the diagnosis" means the most recent one — scan backwards.
export function findLastEvent(trail, eventType) {
  for (let i = trail.length - 1; i >= 0; i--) {
    if (trail[i].event_type === eventType) return trail[i]
  }
  return null
}

export function recoveryTone(rate) {
  if (rate > 70) return 'recovery-high'
  if (rate >= 30) return 'recovery-medium'
  return 'recovery-low'
}
