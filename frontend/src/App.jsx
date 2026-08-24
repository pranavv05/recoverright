import { useEffect, useState, useCallback } from 'react'
import axios from 'axios'
import './App.css'

const api = axios.create({ baseURL: 'http://localhost:8000' })

const EVIDENCE_LABELS = {
  known: 'Known',
  strong_inference: 'Strong Inference',
  unknown: 'Unknown',
}

const DECISION_CLASS = {
  ALLOW: 'badge-allow',
  BLOCK: 'badge-block',
  REQUIRE_HUMAN: 'badge-human',
}

// Paise are integers from the API. Split with integer arithmetic rather than
// dividing by 100 and formatting, so the rupee figure is never a rounded float.
function rupees(paise) {
  if (paise === null || paise === undefined) return '—'
  const negative = paise < 0
  const abs = Math.abs(paise)
  const whole = Math.trunc(abs / 100)
  const fraction = abs % 100
  const formatted = whole.toLocaleString('en-IN')
  const withPaise = fraction ? `${formatted}.${String(fraction).padStart(2, '0')}` : formatted
  return `${negative ? '-₹' : '₹'}${withPaise}`
}

function lakhs(paise) {
  const value = (paise || 0) / 100 / 100000
  return `₹${value.toFixed(1)}L`
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function findLastEvent(trail, eventType) {
  for (let i = trail.length - 1; i >= 0; i--) {
    if (trail[i].event_type === eventType) return trail[i]
  }
  return null
}

function CsvUpload({ onUploaded }) {
  const [file, setFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [rowCount, setRowCount] = useState(null)

  const handleFileChange = (e) => {
    const picked = e.target.files?.[0] || null
    setError(null)
    setResult(null)
    if (picked && !picked.name.toLowerCase().endsWith('.csv')) {
      setFile(null)
      setError('Please upload a CSV file')
      return
    }
    setFile(picked)
  }

  const handleUpload = async () => {
    if (!file) {
      setError('Please choose a CSV file first')
      return
    }
    setError(null)
    setResult(null)

    try {
      const text = await file.text()
      const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0)
      setRowCount(Math.max(lines.length - 1, 0))
    } catch {
      setRowCount(null)
    }

    setUploading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const res = await api.post('/api/upload-csv', formData)
      setResult(res.data)
      await onUploaded()
    } catch (e) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setUploading(false)
    }
  }

  const downloadSample = () => {
    window.location.href = `${api.defaults.baseURL}/api/sample-csv`
  }

  return (
    <div className="csv-upload">
      <div className="csv-upload-head">
        <div className="csv-upload-title">Upload Invoice CSV</div>
        <div className="csv-upload-sub">
          Export overdue invoices from Tally, Zoho Books, or Excel and upload the CSV directly.
        </div>
      </div>
      <div className="csv-upload-controls">
        <input
          type="file"
          accept=".csv"
          onChange={handleFileChange}
          disabled={uploading}
          className="csv-file-input"
        />
        <button className="csv-upload-btn" onClick={handleUpload} disabled={uploading || !file}>
          {uploading ? 'Processing…' : 'Upload & Process'}
        </button>
        <button className="csv-sample-btn" onClick={downloadSample} disabled={uploading}>
          Download Sample CSV
        </button>
      </div>

      {uploading && (
        <div className="csv-processing">
          Processing {rowCount !== null ? `${rowCount} invoices` : 'invoices'}…
        </div>
      )}

      {error && <div className="csv-error">{error}</div>}

      {result && !uploading && (
        <div className="csv-result">
          <span><strong>{result.processed}</strong> processed</span>
          <span className="csv-result-allow"><strong>{result.allow}</strong> ALLOW</span>
          <span className="csv-result-block"><strong>{result.block}</strong> BLOCK</span>
          <span className="csv-result-human"><strong>{result.require_human}</strong> REQUIRE_HUMAN</span>
        </div>
      )}

      {result && !uploading && result.warnings?.length > 0 && (
        <div className="csv-warning-box">
          <div className="csv-warning-box-head">
            ⚠ {result.warnings.length} invoice{result.warnings.length === 1 ? '' : 's'} skipped (already processed):
          </div>
          <ul className="csv-warning-box-list">
            {result.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function Counter({ label, value, tone }) {
  return (
    <div className={`counter ${tone || ''}`}>
      <div className="counter-value">{value}</div>
      <div className="counter-label">{label}</div>
    </div>
  )
}

function EvidenceCell({ invoice }) {
  const tier = invoice.diagnosis?.evidence_tier
  const notes = (invoice.client_notes || '').slice(0, 60)
  return (
    <div className="cell-stacked">
      <span className={`badge badge-evidence-${tier || 'none'}`}>
        {EVIDENCE_LABELS[tier] || 'N/A'}
      </span>
      {notes && <div className="cell-sub">"{notes}{invoice.client_notes.length > 60 ? '…' : ''}"</div>}
    </div>
  )
}

function DiagnosisCell({ invoice }) {
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

function PolicyCell({ invoice }) {
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

// The outcome comes from the recorded execution event, not from re-deriving it
// from the decision — every terminal state now writes one, including BLOCK and
// REQUIRE_HUMAN, so the cell shows what was logged rather than what we assume.
const OUTCOME_LABELS = {
  // Statuses returned by the orchestrator-workers execution layer.
  escalated: 'Escalated to human',
  blocked: 'Blocked',
  waiting: 'Waiting — within pattern',
  draft_created: 'Draft created — not sent',
  simulated: 'Simulated',
  link_created: 'Payment link created',
  duplicate_link_prevented: 'Duplicate link prevented',
  failed_max_retries: 'Failed — retries exhausted',
  failed_server_error: 'Failed — Razorpay 5xx',
  failed_client_error: 'Failed — Razorpay 4xx',
  failed: 'Failed — unexpected error',

  // Pre-refactor statuses, still produced by the demo scripts and present in
  // any audit_log written before the refactor.
  escalated_to_human: 'Escalated to human',
  reminder_simulated: 'Reminder simulated',
  information_gathering_queued: 'Info gathering queued',
  no_action_within_pattern: 'No action — within pattern',
  internal_error: 'Internal error',
  diagnosis_failed: 'Diagnosis failed',
  skipped: 'Skipped',
}

function OutcomeCell({ invoice }) {
  const status = invoice.execution_status
  if (!status) return <span className="cell-main">—</span>
  return <span className="cell-main">{OUTCOME_LABELS[status] || status}</span>
}

function WhyNot({ trail }) {
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

const DECISION_TONE_CLASS = {
  ALLOW: 'reasoning-tone-allow',
  BLOCK: 'reasoning-tone-block',
  REQUIRE_HUMAN: 'reasoning-tone-human',
}

function ReasoningChain({ trail }) {
  const reasoningEvent = findLastEvent(trail, 'reasoning_trace')
  if (!reasoningEvent || !reasoningEvent.diagnosis) return null

  const data = reasoningEvent.diagnosis
  const signals = data.signals || {}
  const policyEvent = findLastEvent(trail, 'policy_decision')
  const toneClass = DECISION_TONE_CLASS[policyEvent?.decision] || 'reasoning-tone-none'

  const thoughts = [
    ['Thought 1 — Engagement', data.thought_1_engagement],
    ['Thought 2 — Content', data.thought_2_content],
    ['Thought 3 — Payment history', data.thought_3_payment_history],
    ['Thought 4 — Evidence quality', data.thought_4_evidence_quality],
    ['Thought 5 — Cause and action', data.thought_5_cause_and_action],
  ]

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

      {thoughts.map(([label, text]) => (
        <div className={`reasoning-thought ${toneClass}`} key={label}>
          <div className="reasoning-thought-label">{label}</div>
          <div className="reasoning-thought-text">{text || '—'}</div>
        </div>
      ))}
    </div>
  )
}

function DetailPanel({ invoiceId, invoice, trail, loading, onClose }) {
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
          {invoice && <div className="detail-sub">{invoice.client_name} · {rupees(invoice.outstanding_balance_paise)} outstanding</div>}
        </div>
        <button className="close-btn" onClick={onClose}>×</button>
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

export default function App() {
  const [invoices, setInvoices] = useState([])
  const [metrics, setMetrics] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [runningBatch, setRunningBatch] = useState(false)

  const [selectedId, setSelectedId] = useState(null)
  const [auditTrail, setAuditTrail] = useState(null)
  const [auditLoading, setAuditLoading] = useState(false)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [invRes, metRes] = await Promise.all([
        api.get('/api/invoices'),
        api.get('/api/metrics'),
      ])
      setInvoices(invRes.data)
      setMetrics(metRes.data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  const selectInvoice = async (invoiceId) => {
    setSelectedId(invoiceId)
    setAuditLoading(true)
    setAuditTrail(null)
    setError(null)
    try {
      const res = await api.get(`/api/invoices/${encodeURIComponent(invoiceId)}/audit`)
      setAuditTrail(res.data)
    } catch (e) {
      setError(`Could not load the audit trail for ${invoiceId}: ${e.message}`)
    } finally {
      setAuditLoading(false)
    }
  }

  const runBatch = async () => {
    setRunningBatch(true)
    setError(null)
    try {
      await api.post('/api/run-batch')
      await fetchAll()
    } catch (e) {
      setError(e.response?.data?.detail || `Batch run failed: ${e.message}`)
    } finally {
      setRunningBatch(false)
    }
  }

  const selectedInvoice = invoices.find((inv) => inv.invoice_id === selectedId)

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">RecoverRight</span>
          <span className="brand-sub">Autonomous receivables recovery — decision audit dashboard</span>
        </div>
        <button className="run-batch-btn" onClick={runBatch} disabled={runningBatch}>
          {runningBatch ? 'Running batch…' : 'Run Batch'}
        </button>
      </header>

      {error && <div className="error-banner">Failed to reach API: {error}</div>}

      {metrics && (
        <div className="counters-row">
          <Counter
            label="Verified Recovered / Collectable"
            value={`${lakhs(metrics.verified_recovered_paise)} / ${lakhs(metrics.recoverable_paise)}`}
          />
          <Counter label="Invoices Processed" value={metrics.total_processed} />
          <Counter label="Requiring More Data" value={metrics.requiring_more_data_count} />
          <Counter label="Escalated to Human" value={metrics.escalated_to_human_count} />
          <Counter label="Blocked Actions" value={metrics.block_count} />
          <Counter
            label="Suspicious Content Detected"
            value={metrics.suspicious_content_detected_count}
            tone={metrics.suspicious_content_detected_count > 0 ? 'tone-red' : 'tone-green'}
          />
          <Counter label="API Failures Recovered" value={metrics.api_failures_recovered_count} />
        </div>
      )}

      <CsvUpload onUploaded={fetchAll} />

      <div className="main-row">
        <div className="table-wrap">
          {loading ? (
            <div className="loading">Loading invoices…</div>
          ) : (
            <table className="batch-table">
              <thead>
                <tr>
                  <th>Invoice</th>
                  <th>Evidence</th>
                  <th>Diagnosis</th>
                  <th>Policy Decision</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {invoices.map((inv) => (
                  <tr
                    key={inv.invoice_id}
                    className={inv.invoice_id === selectedId ? 'row-selected' : ''}
                    onClick={() => selectInvoice(inv.invoice_id)}
                  >
                    <td>
                      <div className="cell-stacked">
                        <span className="cell-main">{inv.invoice_id}</span>
                        <div className="cell-sub">{inv.client_name} · {rupees(inv.outstanding_balance_paise)}</div>
                      </div>
                    </td>
                    <td><EvidenceCell invoice={inv} /></td>
                    <td><DiagnosisCell invoice={inv} /></td>
                    <td><PolicyCell invoice={inv} /></td>
                    <td><OutcomeCell invoice={inv} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <DetailPanel
          invoiceId={selectedId}
          invoice={selectedInvoice}
          trail={auditTrail}
          loading={auditLoading}
          onClose={() => setSelectedId(null)}
        />
      </div>
    </div>
  )
}
