import { useCallback, useEffect, useState } from 'react'
import api, { errorMessage } from './api'
import AnalyticsTab from './components/AnalyticsTab'
import ClientHealthTab from './components/ClientHealthTab'
import Counter from './components/Counter'
import CsvUpload from './components/CsvUpload'
import DetailPanel from './components/DetailPanel'
import EscalationsTab from './components/EscalationsTab'
import InvoiceTable from './components/InvoiceTable'
import RazorpaySync from './components/RazorpaySync'
import { lakhs } from './lib/format'
import './App.css'

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
  const [escalations, setEscalations] = useState([])
  const [escalationsLoading, setEscalationsLoading] = useState(true)

  // One round trip for the whole dashboard. The five endpoints are read-only
  // and independent, so they go out together — a serial chain would make the
  // post-batch refresh five times as slow for no ordering benefit.
  const fetchAll = useCallback(async () => {
    setLoading(true)
    setHealthLoading(true)
    setAnalyticsLoading(true)
    setEscalationsLoading(true)
    setError(null)
    try {
      const [invRes, metRes, healthRes, analyticsRes, escRes] = await Promise.all([
        api.get('/api/invoices'),
        api.get('/api/metrics'),
        api.get('/api/client-health'),
        api.get('/api/analytics'),
        api.get('/api/escalations'),
      ])
      setInvoices(invRes.data)
      setMetrics(metRes.data)
      setHealthScores(healthRes.data)
      setAnalytics(analyticsRes.data)
      setEscalations(escRes.data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setHealthLoading(false)
      setAnalyticsLoading(false)
      setEscalationsLoading(false)
    }
  }, [])

  // fetchAll() opens by setting the four loading flags to true. On this mount
  // that is a no-op: all four already initialise to true, and React bails out
  // of a re-render when the next state is Object.is-equal to the current one,
  // so there is no cascading render for the lint rule to protect against. The
  // calls are not removable either — the same fetchAll() is the manual Refresh
  // handler, where the flags are genuinely flipping back from false.
  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
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
      setError(errorMessage(e, 'Batch run failed'))
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
          className={`tab-btn ${activeTab === 'escalations' ? 'active' : ''}`}
          onClick={() => setActiveTab('escalations')}
        >
          Escalations{escalations.length > 0 ? ` (${escalations.length})` : ''}
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
          <InvoiceTable
            invoices={invoices}
            loading={loading}
            selectedId={selectedId}
            clientFilter={clientFilter}
            onSelectInvoice={selectInvoice}
            onClearFilter={() => setClientFilter(null)}
          />

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
      ) : activeTab === 'escalations' ? (
        <EscalationsTab
          escalations={escalations}
          loading={escalationsLoading}
          onResolved={fetchAll}
        />
      ) : (
        <AnalyticsTab
          analytics={analytics}
          healthScores={healthScores}
          loading={analyticsLoading}
        />
      )}
    </div>
  )
}
