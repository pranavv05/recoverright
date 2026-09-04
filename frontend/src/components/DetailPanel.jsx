import { useEffect } from 'react'
import ReasoningChain from './ReasoningChain'
import WhyNot from './WhyNot'
import { DECISION_CLASS, RESOLUTION_CLASS } from '../lib/constants'
import { formatTime, rupees } from '../lib/format'

// An audit event's `decision` column carries an engine verdict on a
// policy_decision row and a human verdict on an escalation_resolved row. Both
// render here; keeping the two maps separate is what stops a reviewer's
// APPROVED from being styled as though the policy engine had allowed it.
function decisionClass(decision) {
  return DECISION_CLASS[decision] || RESOLUTION_CLASS[decision] || 'badge-none'
}

export default function DetailPanel({ invoiceId, invoice, trail, loading, onClose }) {
  useEffect(() => {
    if (!invoiceId) return
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [invoiceId, onClose])

  if (!invoiceId) return null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="detail-panel" onClick={(e) => e.stopPropagation()}>
        <div className="detail-header">
          <div>
            <div className="detail-title">{invoiceId}</div>
            {invoice && (
              <div className="detail-sub">
                {invoice.client_name} · {rupees(invoice.outstanding_balance_paise)} outstanding
              </div>
            )}
          </div>
          <button className="close-btn" aria-label="Close audit trail" onClick={onClose}>×</button>
        </div>

        <div className="detail-body">
          {loading && <div className="detail-loading">Loading audit trail…</div>}

          {!loading && trail && (
            <>
              <WhyNot trail={trail} />
              <ReasoningChain trail={trail} />

              <div className="audit-title">AUDIT TRAIL</div>
              <div className="audit-list">
                {trail.map((event, i) => (
                  <div className="audit-event" key={i}>
                    <div className="audit-event-head">
                      <span className="audit-event-type">{event.event_type}</span>
                      {event.decision && (
                        <span className={`badge ${decisionClass(event.decision)}`}>
                          {event.decision}
                        </span>
                      )}
                      <span className="audit-event-time">{formatTime(event.created_at)}</span>
                    </div>
                    {event.reasons?.length > 0 && (
                      <div className="audit-event-detail">reasons: {event.reasons.join('; ')}</div>
                    )}
                    {event.blocked_actions?.length > 0 && (
                      <div className="audit-event-detail">blocked: {event.blocked_actions.join(', ')}</div>
                    )}
                    {event.diagnosis && event.diagnosis.diagnosed_cause && (
                      <div className="audit-event-detail">
                        cause: {event.diagnosis.diagnosed_cause} · evidence: {event.diagnosis.evidence_tier}
                        {event.diagnosis.evidence_cited && <> · "{event.diagnosis.evidence_cited}"</>}
                      </div>
                    )}
                    {event.event_type === 'reasoning_trace' && event.diagnosis && (
                      <div className="audit-event-detail">
                        5-thought reasoning chain recorded — see REASONING CHAIN above
                      </div>
                    )}
                    {event.execution_result && (
                      <div className="audit-event-detail">
                        result: {JSON.stringify(event.execution_result)}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
