import { useState } from 'react'
import { RISK_TIER_CLASS, RISK_TIER_LABELS, RISK_TIER_ORDER, TREND_LABELS } from '../lib/constants'
import { rupees } from '../lib/format'

// Every sortable column reduces to one comparable number, so one comparator
// covers all three. A row missing the field sorts last rather than throwing.
const HEALTH_SORT_COLUMNS = {
  risk_tier: (row) => RISK_TIER_ORDER[row.risk_tier] ?? 99,
  total_outstanding_paise: (row) => row.total_outstanding_paise ?? 0,
  days_overdue_avg: (row) => row.days_overdue_avg ?? 0,
}

export default function ClientHealthTab({ healthScores, loading, onSelectClient }) {
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
            <th className="sortable" onClick={() => toggleSort('total_outstanding_paise')}>
              Outstanding (₹){sortArrow('total_outstanding_paise')}
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
              <td>{rupees(row.total_outstanding_paise)}</td>
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
