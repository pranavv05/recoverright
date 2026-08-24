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

const RISK_TIER_LABELS = {
  critical: 'Critical',
  at_risk: 'At Risk',
  watch: 'Watch',
  healthy: 'Healthy',
}

const RISK_TIER_CLASS = {
  critical: 'badge-risk-critical',
  at_risk: 'badge-risk-at_risk',
  watch: 'badge-risk-watch',
  healthy: 'badge-risk-healthy',
}

const RISK_TIER_ORDER = { critical: 0, at_risk: 1, watch: 2, healthy: 3 }

const TREND_LABELS = {
  improving: 'Improving',
  stable: 'Stable',
  deteriorating: 'Deteriorating',
  insufficient_data: 'Insufficient data',
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

function RazorpaySync({ onSynced }) {
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)

  const handleSync = async () => {
    setError(null)
    setResult(null)
    setSyncing(true)
    try {
      const res = await api.post('/api/sync-razorpay')
      setResult(res.data)
      await onSynced()
    } catch (e) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div className="rzp-sync">
      <div className="rzp-sync-head">
        <div className="rzp-sync-title">Sync from Razorpay</div>
        <div className="rzp-sync-sub">
          Pull overdue invoices directly from your connected Razorpay account — no file needed.
        </div>
      </div>
      <div className="rzp-sync-controls">
        <button className="rzp-sync-btn" onClick={handleSync} disabled={syncing}>
          {syncing ? 'Fetching invoices from Razorpay…' : 'Sync from Razorpay'}
        </button>
      </div>

      {error && <div className="rzp-sync-error">{error}</div>}

      {result && !syncing && (
        <div className="rzp-sync-result">
          {result.invoices_fetched === 0 ? (
            <span>{result.message || 'No overdue invoices found in your Razorpay account'}</span>
          ) : (
            <>
              <span><strong>{result.invoices_fetched}</strong> fetched</span>
              <span><strong>{result.processed}</strong> processed</span>
              <span className="csv-result-allow"><strong>{result.allow}</strong> ALLOW</span>
              <span className="csv-result-block"><strong>{result.block}</strong> BLOCK</span>
              <span className="csv-result-human"><strong>{result.require_human}</strong> REQUIRE_HUMAN</span>
            </>
          )}
        </div>
      )}

      {result && !syncing && result.warnings?.length > 0 && (
        <div className="csv-warning-box">
          <div className="csv-warning-box-head">
            ⚠ {result.warnings.length} invoice{result.warnings.length === 1 ? '' : 's'} skipped:
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

// The API already returns rupees (paise / 100) for health-score amounts, so
// this formats a plain rupee number rather than splitting paise like rupees().
function formatRupees(value) {
  if (value === null || value === undefined) return '—'
  return `₹${Number(value).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
}

const HEALTH_SORT_COLUMNS = {
  risk_tier: (row) => RISK_TIER_ORDER[row.risk_tier] ?? 99,
  total_outstanding_rupees: (row) => row.total_outstanding_rupees ?? 0,
  days_overdue_avg: (row) => row.days_overdue_avg ?? 0,
}

function ClientHealthTab({ healthScores, loading, onSelectClient }) {
  const [sortKey, setSortKey] = useState('risk_tier')
  const [sortDir, setSortDir] = useState('asc')

  const toggleSort = (key) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      // Risk tier reads naturally worst-first; the two numeric columns read
      // naturally highest-first — both are "asc" as this table defines it.
      setSortDir('asc')
    }
  }

  if (loading) {
    return <div className="loading">Loading client health scores…</div>
  }

  if (healthScores.length === 0) {
    return (
      <div className="health-empty">
        Run a batch first to generate client health scores.
      </div>
    )
  }

  const getValue = HEALTH_SORT_COLUMNS[sortKey]
  const sorted = [...healthScores].sort((a, b) => {
    const diff = getValue(a) - getValue(b)
    return sortDir === 'asc' ? diff : -diff
  })

  const sortArrow = (key) => (key === sortKey ? (sortDir === 'asc' ? ' ▲' : ' ▼') : '')

  return (
    <div className="table-wrap health-table-wrap">
      <table className="batch-table health-table">
        <thead>
          <tr>
            <th>Client Name</th>
            <th className="sortable" onClick={() => toggleSort('risk_tier')}>
              Risk Tier{sortArrow('risk_tier')}
            </th>
            <th className="sortable" onClick={() => toggleSort('total_outstanding_rupees')}>
              Outstanding (₹){sortArrow('total_outstanding_rupees')}
            </th>
            <th>Invoices</th>
            <th className="sortable" onClick={() => toggleSort('days_overdue_avg')}>
              Avg Days Overdue{sortArrow('days_overdue_avg')}
            </th>
            <th>Trend</th>
            <th>Capital Flag</th>
            <th>Pattern Description</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.client_name} onClick={() => onSelectClient(row.client_name)}>
              <td><span className="cell-main">{row.client_name}</span></td>
              <td>
                <span className={`badge ${RISK_TIER_CLASS[row.risk_tier] || 'badge-none'}`}>
                  {RISK_TIER_LABELS[row.risk_tier] || row.risk_tier}
                </span>
              </td>
              <td>{formatRupees(row.total_outstanding_rupees)}</td>
              <td>{row.invoice_count}</td>
              <td>{row.days_overdue_avg ?? '—'}</td>
              <td>{TREND_LABELS[row.payment_trend] || row.payment_trend}</td>
              <td>
                {row.capital_flag && (
                  <span
                    className="badge badge-capital"
                    title={row.capital_flag_reason || ''}
                  >
                    CANDIDATE
                  </span>
                )}
              </td>
              <td><div className="cell-sub health-pattern">{row.pattern_description}</div></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// "cash_flow_delay" -> "Cash flow delay". Used for policy reasons, scenario
// types, and diagnosed causes alike — all three are underscore-joined
// machine strings from the backend with no separate display label.
function humanize(text) {
  if (!text) return '—'
  const spaced = text.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function recoveryTone(rate) {
  if (rate > 70) return 'recovery-high'
  if (rate >= 30) return 'recovery-medium'
  return 'recovery-low'
}

function AnalyticsTab({ analytics, loading }) {
  if (loading) {
    return <div className="loading">Loading analytics…</div>
  }

  if (!analytics || analytics.batch_summary.total_invoices_processed === 0) {
    return (
      <div className="health-empty">
        Run a batch to generate analytics.
      </div>
    )
  }

  const {
    batch_summary: summary,
    recovery_by_scenario: byScenario,
    top_policy_rules: topRules,
    multi_invoice_clients: multiClients,
    avg_overdue_by_cause: byCause,
  } = analytics

  return (
    <div className="analytics-tab">
      <div className="counters-row analytics-counters">
        <Counter label="Total Processed" value={summary.total_invoices_processed} />
        <Counter label="Total Outstanding" value={lakhs(summary.total_outstanding_paise)} />
        <Counter label="Total Recovered" value={lakhs(summary.total_recovered_paise)} />
        <Counter
          label="Recovery Rate"
          value={`${summary.overall_recovery_rate}%`}
          tone={summary.overall_recovery_rate > 0 ? 'tone-green' : ''}
        />
      </div>

      <div className="analytics-row">
        <div className="analytics-panel">
          <div className="analytics-panel-title">Recovery by Scenario</div>
          <div className="analytics-panel-body">
            {byScenario.length === 0 ? (
              <div className="health-empty">No scenario data yet.</div>
            ) : (
              <table className="batch-table analytics-scenario-table">
                <thead>
                  <tr>
                    <th>Scenario</th>
                    <th>Recovery Rate</th>
                    <th>Invoices</th>
                  </tr>
                </thead>
                <tbody>
                  {byScenario.map((row) => (
                    <tr key={row.scenario_type}>
                      <td><span className="cell-main">{humanize(row.scenario_type)}</span></td>
                      <td>
                        <div className="recovery-bar-track">
                          <div
                            className={`recovery-bar-fill ${recoveryTone(row.recovery_rate)}`}
                            style={{ width: `${Math.min(Math.max(row.recovery_rate, 0), 100)}%` }}
                          />
                          <span className="recovery-bar-label">{row.recovery_rate}%</span>
                        </div>
                      </td>
                      <td>{row.total_invoices}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="analytics-panel">
          <div className="analytics-panel-title">Top Policy Rules</div>
          <div className="analytics-panel-body">
            {topRules.length === 0 ? (
              <div className="health-empty">No policy decisions yet.</div>
            ) : (
              <table className="batch-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Rule</th>
                    <th>Count</th>
                  </tr>
                </thead>
                <tbody>
                  {topRules.map((rule, i) => (
                    <tr key={rule.reason}>
                      <td className="rank-cell">{i + 1}</td>
                      <td>{humanize(rule.reason)}</td>
                      <td>{rule.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>

      <div className="analytics-row">
        <div className="analytics-panel">
          <div className="analytics-panel-title">Multi-Invoice Clients</div>
          <div className="analytics-panel-body">
            {multiClients.length === 0 ? (
              <div className="health-empty">No clients with 2+ invoices in this batch.</div>
            ) : (
              <table className="batch-table">
                <thead>
                  <tr>
                    <th>Client</th>
                    <th>Invoices</th>
                    <th>Outstanding (₹)</th>
                    <th>Invoice IDs</th>
                  </tr>
                </thead>
                <tbody>
                  {multiClients.map((row) => (
                    <tr key={row.client_name}>
                      <td><span className="cell-main">{row.client_name}</span></td>
                      <td>{row.invoice_count}</td>
                      <td>{lakhs(row.total_outstanding_paise)}</td>
                      <td><div className="cell-sub">{row.invoice_ids.join(', ')}</div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="analytics-panel">
          <div className="analytics-panel-title">Average Overdue by Cause</div>
          <div className="analytics-panel-body">
            {byCause.length === 0 ? (
              <div className="health-empty">No diagnosis data yet.</div>
            ) : (
              <table className="batch-table">
                <thead>
                  <tr>
                    <th>Cause</th>
                    <th>Avg Days Overdue</th>
                    <th>Invoices</th>
                  </tr>
                </thead>
                <tbody>
                  {byCause.map((row) => (
                    <tr key={row.diagnosed_cause}>
                      <td><span className="cell-main">{humanize(row.diagnosed_cause)}</span></td>
                      <td>{row.avg_days_overdue}</td>
                      <td>{row.invoice_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
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

  const [activeTab, setActiveTab] = useState('invoices')
  const [healthScores, setHealthScores] = useState([])
  const [healthLoading, setHealthLoading] = useState(true)
  const [clientFilter, setClientFilter] = useState(null)
  const [analytics, setAnalytics] = useState(null)
  const [analyticsLoading, setAnalyticsLoading] = useState(true)

  const fetchAll = useCallback(async () => {
    setLoading(true)
    setHealthLoading(true)
    setAnalyticsLoading(true)
    setError(null)
    try {
      const [invRes, metRes, healthRes, analyticsRes] = await Promise.all([
        api.get('/api/invoices'),
        api.get('/api/metrics'),
        api.get('/api/client-health'),
        api.get('/api/analytics'),
      ])
      setInvoices(invRes.data)
      setMetrics(metRes.data)
      setHealthScores(healthRes.data)
      setAnalytics(analyticsRes.data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setHealthLoading(false)
      setAnalyticsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  const goToClientInvoices = (clientName) => {
    setClientFilter(clientName)
    setActiveTab('invoices')
  }

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

      <div className="data-source-row">
        <RazorpaySync onSynced={fetchAll} />
        <CsvUpload onUploaded={fetchAll} />
      </div>

      <div className="tabs">
        <button
          className={`tab-btn ${activeTab === 'invoices' ? 'active' : ''}`}
          onClick={() => setActiveTab('invoices')}
        >
          Invoices
        </button>
        <button
          className={`tab-btn ${activeTab === 'health' ? 'active' : ''}`}
          onClick={() => setActiveTab('health')}
        >
          Client Health
        </button>
        <button
          className={`tab-btn ${activeTab === 'analytics' ? 'active' : ''}`}
          onClick={() => setActiveTab('analytics')}
        >
          Analytics
        </button>
      </div>

      {activeTab === 'invoices' ? (
        <div className="main-row">
          <div className="table-wrap">
            {clientFilter && (
              <div className="client-filter-banner">
                Showing invoices for <strong>{clientFilter}</strong>
                <button className="client-filter-clear" onClick={() => setClientFilter(null)}>
                  Clear filter
                </button>
              </div>
            )}
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
                  {(clientFilter ? invoices.filter((inv) => inv.client_name === clientFilter) : invoices).map((inv) => (
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
      ) : activeTab === 'health' ? (
        <ClientHealthTab
          healthScores={healthScores}
          loading={healthLoading}
          onSelectClient={goToClientInvoices}
        />
      ) : (
        <AnalyticsTab analytics={analytics} loading={analyticsLoading} />
      )}
    </div>
  )
}
