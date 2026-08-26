import Counter from './Counter'
import { humanize, lakhs, recoveryTone } from '../lib/format'

export default function AnalyticsTab({ analytics, loading }) {
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
