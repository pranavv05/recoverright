import { DECISION_TONE_CLASS } from '../lib/constants'
import { findLastEvent } from '../lib/format'

const THOUGHT_FIELDS = [
  ['Thought 1 — Engagement', 'thought_1_engagement'],
  ['Thought 2 — Content', 'thought_2_content'],
  ['Thought 3 — Payment history', 'thought_3_payment_history'],
  ['Thought 4 — Evidence quality', 'thought_4_evidence_quality'],
  ['Thought 5 — Cause and action', 'thought_5_cause_and_action'],
]

// Renders the ReAct trace the diagnoser recorded: call 1's extracted signals,
// then call 2's five thoughts. Tinted by the policy decision so a chain that
// ended in a BLOCK reads as one at a glance.
export default function ReasoningChain({ trail }) {
  const reasoningEvent = findLastEvent(trail, 'reasoning_trace')
  if (!reasoningEvent || !reasoningEvent.diagnosis) return null

  const data = reasoningEvent.diagnosis
  const signals = data.signals || {}
  const policyEvent = findLastEvent(trail, 'policy_decision')
  const toneClass = DECISION_TONE_CLASS[policyEvent?.decision] || 'reasoning-tone-none'

  return (
    <div className="reasoning-chain">
      <div className="reasoning-chain-title">REASONING CHAIN</div>

      <div className="reasoning-signals">
        <div className="reasoning-signals-title">Signal extraction:</div>
        <div className="reasoning-signal-row">
          <span className="reasoning-signal-label">Engagement:</span> {signals.engagement_signal || '—'}
        </div>
        <div className="reasoning-signal-row">
          <span className="reasoning-signal-label">Payment history:</span> {signals.payment_history_pattern || '—'}
        </div>
        <div className="reasoning-signal-row">
          <span className="reasoning-signal-label">Content:</span> {signals.content_signal || '—'}
          {signals.content_detail ? ` — ${signals.content_detail}` : ''}
        </div>
        <div className="reasoning-signal-row">
          <span className="reasoning-signal-label">Evidence available:</span> {signals.evidence_available ? 'yes' : 'no'}
        </div>
      </div>

      {THOUGHT_FIELDS.map(([label, key]) => (
        <div className={`reasoning-thought ${toneClass}`} key={label}>
          <div className="reasoning-thought-label">{label}</div>
          <div className="reasoning-thought-text">{data[key] || '—'}</div>
        </div>
      ))}
    </div>
  )
}
