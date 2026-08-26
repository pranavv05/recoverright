import { DECISION_CLASS, EVIDENCE_LABELS, OUTCOME_LABELS } from '../lib/constants'

// How much of a client note fits in a table cell before it is elided.
const NOTE_PREVIEW_CHARS = 60

export function EvidenceCell({ invoice }) {
  const tier = invoice.diagnosis?.evidence_tier
  const notes = (invoice.client_notes || '').slice(0, NOTE_PREVIEW_CHARS)
  return (
    <div className="cell-stacked">
      <span className={`badge badge-evidence-${tier || 'none'}`}>
        {EVIDENCE_LABELS[tier] || 'N/A'}
      </span>
      {notes && (
        <div className="cell-sub">
          "{notes}{invoice.client_notes.length > NOTE_PREVIEW_CHARS ? '…' : ''}"
        </div>
      )}
    </div>
  )
}

export function DiagnosisCell({ invoice }) {
  if (invoice.diagnosis_failed) {
    return <div className="cell-stacked"><span className="cell-main">diagnosis failed</span></div>
  }
  if (!invoice.diagnosis) {
    return <div className="cell-stacked"><span className="cell-main">—</span></div>
  }
  return (
    <div className="cell-stacked">
      <span className="cell-main">{invoice.diagnosis.diagnosed_cause}</span>
      {invoice.diagnosis.evidence_cited && (
        <div className="cell-sub">{invoice.diagnosis.evidence_cited}</div>
      )}
    </div>
  )
}

export function PolicyCell({ invoice }) {
  const cls = DECISION_CLASS[invoice.policy_decision] || 'badge-none'
  return (
    <div className="cell-stacked">
      <span className={`badge ${cls}`}>{invoice.policy_decision || 'N/A'}</span>
      {invoice.policy_reasons?.[0] && (
        <div className="cell-sub">{invoice.policy_reasons[0]}</div>
      )}
    </div>
  )
}

export function OutcomeCell({ invoice }) {
  const status = invoice.execution_status
  if (!status) return <span className="cell-main">—</span>
  // An unmapped status renders raw rather than as a blank or an "unknown" —
  // a new backend status should be visible on the dashboard, not swallowed.
  return <span className="cell-main">{OUTCOME_LABELS[status] || status}</span>
}
