import { useState } from 'react'
import api, { errorMessage } from '../api'

export default function RazorpaySync({ onSynced }) {
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
      setError(errorMessage(e))
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
