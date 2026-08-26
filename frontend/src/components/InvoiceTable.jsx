import { DiagnosisCell, EvidenceCell, OutcomeCell, PolicyCell } from './InvoiceCells'
import { rupees } from '../lib/format'

// The batch table. Filtering by client is applied here rather than in App so
// the "showing invoices for X" banner and the rows it describes cannot drift
// apart — one prop drives both.
export default function InvoiceTable({
  invoices,
  loading,
  selectedId,
  clientFilter,
  onSelectInvoice,
  onClearFilter,
}) {
  const visible = clientFilter
    ? invoices.filter((inv) => inv.client_name === clientFilter)
    : invoices

  return (
    <div className="table-wrap">
      {clientFilter && (
        <div className="client-filter-banner">
          Showing invoices for <strong>{clientFilter}</strong>
          <button className="client-filter-clear" onClick={onClearFilter}>
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
            {visible.map((inv) => (
              <tr
                key={inv.invoice_id}
                className={inv.invoice_id === selectedId ? 'row-selected' : ''}
                onClick={() => onSelectInvoice(inv.invoice_id)}
              >
                <td>
                  <div className="cell-stacked">
                    <span className="cell-main">{inv.invoice_id}</span>
                    <div className="cell-sub">
                      {inv.client_name} · {rupees(inv.outstanding_balance_paise)}
                    </div>
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
  )
}
