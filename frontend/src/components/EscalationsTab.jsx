import { useState } from 'react'
import api, { errorMessage } from '../api'
import { PRIORITY_CLASS, PRIORITY_LABELS, PRIORITY_ORDER } from '../lib/constants'
import { humanize, rupees } from '../lib/format'

// The queue renders one button per action the *server* said is approvable for
// that invoice, and sends back exactly that string. There is no client-side
// list of permitted actions to drift out of step with the policy engine —
// if the engine permits nothing, the card shows no Approve control at all.
function EscalationCard({ case_, actor, onResolved, onError }) {
  const [action, setAction] = useState(case_.approvable_actions[0] || '')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(null)

  const resolve = async (resolution) => {
    if (!actor.trim()) {
      onError('Enter your name or email before recording a decision')
      return
    }
    setBusy(resolution)
    onError(null)
    try {
      await api.post(`/api/escalations/${encodeURIComponent(case_.invoice_id)}/resolve`, {
        resolution,
        actor,
        note,
        action: resolution === 'approved' ? action : null,
      })
      await onResolved()
    } catch (e) {
      onError(errorMessage(e, `Could not resolve ${case_.invoice_id}`))
    } finally {
      setBusy(null)
    }
  }

  const canApprove = case_.approvable_actions.length > 0

  return (
    <div className="esc-card">
      <div className="esc-card-head">
        <span className={`badge ${PRIORITY_CLASS[case_.priority] || 'badge-none'}`}>
          {PRIORITY_LABELS[case_.priority] || case_.priority}
        </span>
        <span className="esc-card-invoice">{case_.invoice_id}</span>
        <span className="esc-card-client">{case_.client_name}</span>
        <span className="esc-card-amount">{rupees(case_.outstanding_balance_paise)}</span>
        <span className="esc-card-overdue">{case_.days_overdue} days overdue</span>
      </div>

      <div className="esc-card-body">
        <div className="esc-field">
          <div className="esc-field-label">Why it was escalated</div>
          <ul className="esc-reasons">
            {case_.reasons.map((reason) => (
              <li key={reason}>{humanize(reason)}</li>
            ))}
          </ul>
        </div>

        <div className="esc-field">
          <div className="esc-field-label">What the agent proposed</div>
          <div className="esc-field-value">
            {case_.recommended_action ? humanize(case_.recommended_action) : '—'}
            {case_.diagnosed_cause && (
              <span className="esc-field-sub">
                {' '}· diagnosed as {humanize(case_.diagnosed_cause)}
                {case_.evidence_tier ? ` (${humanize(case_.evidence_tier)})` : ''}
              </span>
            )}
          </div>
          {case_.evidence_cited && (
            <div className="esc-field-sub">Evidence: {case_.evidence_cited}</div>
          )}
        </div>

        {case_.client_notes && (
          <div className="esc-field">
            <div className="esc-field-label">Client note</div>
            <div className="esc-note">"{case_.client_notes}"</div>
          </div>
        )}
      </div>

      <div className="esc-card-actions">
        <div className="esc-card-reviewer">Reviewing as: {actor.trim() || '—'}</div>
        {canApprove ? (
          <>
            <select
              className="esc-action-select"
              aria-label={`Action to authorise for ${case_.invoice_id}`}
              value={action}
              onChange={(e) => setAction(e.target.value)}
              disabled={busy !== null}
            >
              {case_.approvable_actions.map((a) => (
                <option key={a} value={a}>{humanize(a)}</option>
              ))}
            </select>
            <button
              className="esc-btn esc-btn-approve"
              onClick={() => resolve('approved')}
              disabled={busy !== null}
            >
              {busy === 'approved' ? 'Approving…' : 'Approve'}
            </button>
          </>
        ) : (
          // Not a disabled button: there is nothing to enable. The engine
          // permits only handoffs to a person here, which is what this review
          // already is — so the honest control is a sentence, not a control.
          <div className="esc-no-action">
            No automated action is permitted on this invoice — the policy engine
            allows only {case_.allowed_actions.map(humanize).join(', ') || 'nothing'}.
            Handle it directly and record your decision.
          </div>
        )}

        <input
          className="esc-note-input"
          type="text"
          placeholder="Note (optional)"
          aria-label={`Note for ${case_.invoice_id}`}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={busy !== null}
        />
        <button
          className="esc-btn esc-btn-reject"
          onClick={() => resolve('rejected')}
          disabled={busy !== null}
        >
          {busy === 'rejected' ? 'Recording…' : 'Reject'}
        </button>
        <button
          className="esc-btn esc-btn-defer"
          onClick={() => resolve('deferred')}
          disabled={busy !== null}
        >
          {busy === 'deferred' ? 'Recording…' : 'Defer'}
        </button>
      </div>
    </div>
  )
}

export default function EscalationsTab({ escalations, loading, onResolved }) {
  const [actor, setActor] = useState('Priya (Finance Lead)')
  const [error, setError] = useState(null)

  if (loading) {
    return <div className="loading">Loading escalations…</div>
  }

  if (escalations.length === 0) {
    return (
      <div className="health-empty">
        Nothing awaiting review. Every escalated invoice has been resolved.
      </div>
    )
  }

  const sorted = [...escalations].sort(
    (a, b) =>
      (PRIORITY_ORDER[a.priority] ?? 99) - (PRIORITY_ORDER[b.priority] ?? 99) ||
      (b.outstanding_balance_paise || 0) - (a.outstanding_balance_paise || 0)
  )

  const underReview = sorted.reduce(
    (total, c) => total + (c.outstanding_balance_paise || 0), 0
  )

  return (
    <div className="esc-tab">
      <div className="esc-toolbar">
        <div className="esc-toolbar-summary">
          <strong>{sorted.length}</strong> awaiting review ·{' '}
          <strong>{rupees(underReview)}</strong> outstanding
        </div>
        <div className="esc-actor-group">
          <label className="esc-actor">
            Reviewing as
            <input
              type="text"
              placeholder="you@company.com"
              aria-label="Reviewer name or email"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
            />
          </label>
          {error && <div className="esc-error">{error}</div>}
        </div>
      </div>

      <div className="esc-list">
        {sorted.map((case_) => (
          <EscalationCard
            key={case_.invoice_id}
            case_={case_}
            actor={actor}
            onResolved={onResolved}
            onError={setError}
          />
        ))}
      </div>
    </div>
  )
}
