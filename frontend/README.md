# RecoverRight Dashboard

React + Vite dashboard for the RecoverRight receivables agent. It is a decision
*audit* surface, not a control panel: everything it shows is a record of what
the pipeline already decided and why.

## Running it

```bash
npm install
npm run dev          # http://localhost:3000, expects the API on :8000
```

The API base URL comes from `VITE_API_BASE_URL` and falls back to
`http://localhost:8000`, so a build can point at a deployed backend without
editing source.

```bash
npm run build        # production bundle into dist/
npm run preview      # serve the built bundle
npm run lint         # oxlint
npm test             # vitest, watch mode
npm run test:run     # vitest, single run
npm run test:coverage
```

## Structure

```
src/
  api.js                    # the shared axios instance + one error-message rule
  App.jsx                   # shell: data fetching, tab state, client filter
  lib/
    format.js               # pure display formatters (paise → ₹, snake_case → prose)
    constants.js            # backend machine strings → labels and CSS classes
  components/
    InvoiceTable.jsx        # the batch table, with client filtering
    InvoiceCells.jsx        # evidence / diagnosis / policy / outcome cells
    DetailPanel.jsx         # one invoice's full audit trail
    WhyNot.jsx              # what the agent wanted vs. what the engine allowed
    ReasoningChain.jsx      # the recorded ReAct trace — signals + five thoughts
    ClientHealthTab.jsx     # per-client risk tiers, sortable
    AnalyticsTab.jsx        # recovery by scenario, rule frequency, cohorts
    CsvUpload.jsx           # merchant CSV ingestion
    RazorpaySync.jsx        # pull overdue invoices from Razorpay
    Counter.jsx
  test/
    setup.js                # jest-dom matchers, per-test cleanup
    fixtures.js             # payloads shaped like the real API responses
```

`App.jsx` holds all fetching and cross-tab state; every component below it is
driven by props and does its own rendering only. That split is what makes the
components testable without a server.

## Tests

`npm run test:coverage` — 191 tests, Vitest + React Testing Library on jsdom.
Coverage thresholds are enforced in `vite.config.js` (95% statements, 90%
branches); the suite currently sits at ~99% statements.

Nothing here talks to a real backend. The axios instance in `src/api.js` is
stubbed per test, and `src/test/fixtures.js` holds payloads copied from the
shapes `api/main.py` actually returns — so a fixture that drifts from the API
is a visible edit, not an invisible pass.

What the suite is actually protecting:

| Area | What it pins |
|---|---|
| `lib/format.test.js` | Money stays integer paise end to end. Indian digit grouping, exact paise remainders, `null` rendering as "—" rather than ₹0. |
| `lib/constants.test.js` | Every decision, evidence tier, risk tier, trend, and execution status the Python emits has a label here. A new backend status fails this test instead of silently rendering blank. |
| `InvoiceCells.test.jsx` | A failed diagnosis reads as *N/A*, never as evidence. An unmapped status renders raw rather than disappearing. |
| `WhyNot.test.jsx` | The refusal panel names the model's recommendation and the engine's reasons, says "blocked" vs "escalated" correctly, and renders nothing for an ALLOW. |
| `ReasoningChain.test.jsx` | All five thoughts render, missing ones show "—" not `undefined`, and the chain is tinted by the decision it led to. |
| `ClientHealthTab.test.jsx` | Default sort is worst-risk-first; sorting does not mutate the rows it was given; a missing average sorts last instead of throwing. |
| `AnalyticsTab.test.jsx` | Recovery bars are clamped to 0–100%; empty panels stay visible with an empty state instead of vanishing. |
| `CsvUpload.test.jsx` / `RazorpaySync.test.jsx` | Every failure path: a non-CSV rejected before any request, FastAPI's `detail` surfaced, a network error surfaced, controls re-enabled so the action can be retried. |
| `App.test.jsx` | The four dashboard endpoints go out concurrently; one banner on failure rather than four; invoice ids are percent-encoded; the health→invoices drill-down filters correctly; a batch run refetches. |
