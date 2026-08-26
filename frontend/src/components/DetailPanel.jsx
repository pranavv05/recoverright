import ReasoningChain from './ReasoningChain'
import WhyNot from './WhyNot'
import { DECISION_CLASS } from '../lib/constants'
import { formatTime, rupees } from '../lib/format'

export default function DetailPanel({ invoiceId, invoice, trail, loading, onClose }) {
  if (!invoiceId) {
    return (
      <div className="detail-panel detail-empty">
        Select an invoice to see its full audit trail.
      </div>
    )
  }

  return (
    <div className="detail-panel">
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
                    <span className={`badge ${DECISION_CLASS[event.decision] || 'badge-none'}`}>
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
  )
}
