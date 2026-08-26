import { findLastEvent } from '../lib/format'

// The headline explanation for a refusal: what the agent wanted, and the
// deterministic reason it did not get it. Renders nothing for an ALLOW —
// there is no refusal to explain.
export default function WhyNot({ trail }) {
  const diagnosisEvent = findLastEvent(trail, 'diagnosis')
  const policyEvent = findLastEvent(trail, 'policy_decision')
  const decision = policyEvent?.decision
  if (!policyEvent || (decision !== 'BLOCK' && decision !== 'REQUIRE_HUMAN')) return null

  const recommended = diagnosisEvent?.diagnosis?.recommended_action || 'an action'
  const verb = decision === 'BLOCK' ? 'blocked' : 'escalated'
  const reasons = (policyEvent.reasons || []).join('; ') || 'no reason recorded'

  return (
    <div className="why-not">
      <div className="why-not-title">WHY NOT</div>
      <p>
        The agent recommended <strong>{recommended}</strong>.
        The policy engine <strong>{verb}</strong> it because: {reasons}
      </p>
    </div>
  )
}
