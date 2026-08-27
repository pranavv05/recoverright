import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import api from './api'
import {
  allowedTrail,
  analytics,
  apiResponses,
  blockedTrail,
  escalations,
  healthScores,
  invoices,
  metrics,
} from './test/fixtures'

// App is the only place the four dashboard endpoints, the tab state, the
// client filter and the audit-trail fetch meet. These are integration tests
// over that wiring: axios is stubbed at the instance, everything below it is
// the real component tree.

// Every read endpoint the dashboard opens with. Derived from the fixture map
// rather than repeated here, so adding a tab updates both the "fetched in one
// pass" and the "fetched concurrently" assertions at once.
const DASHBOARD_ENDPOINTS = Object.keys(apiResponses())

function mockApi({ responses = apiResponses(), trails = {}, failures = {} } = {}) {
  const get = vi.spyOn(api, 'get').mockImplementation((url) => {
    if (failures[url]) return Promise.reject(failures[url])

    const auditMatch = url.match(/^\/api\/invoices\/(.+)\/audit$/)
    if (auditMatch) {
      const id = decodeURIComponent(auditMatch[1])
      if (failures.audit) return Promise.reject(failures.audit)
      return Promise.resolve({ data: trails[id] ?? [] })
    }

    if (url in responses) return Promise.resolve({ data: responses[url] })
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
  const post = vi.spyOn(api, 'post').mockResolvedValue({ data: {} })
  return { get, post }
}

// The dashboard is fully loaded once the first invoice row is on screen.
async function renderApp({ readyText = 'INV-1001', ...options } = {}) {
  const mocks = mockApi(options)
  const utils = render(<App />)
  await screen.findByText(readyText)
  return { ...utils, ...mocks }
}

describe('App — initial load', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('fetches every dashboard endpoint in one pass', async () => {
    const { get } = await renderApp()
    const urls = get.mock.calls.map(([url]) => url)
    expect(urls).toEqual(expect.arrayContaining(DASHBOARD_ENDPOINTS))
  })

  it('fetches them concurrently rather than one after another', async () => {
    // A serial chain would make the post-batch refresh four round trips deep.
    let inFlight = 0
    let maxInFlight = 0
    vi.spyOn(api, 'get').mockImplementation((url) => {
      inFlight += 1
      maxInFlight = Math.max(maxInFlight, inFlight)
      return new Promise((resolve) => {
        setTimeout(() => {
          inFlight -= 1
          resolve({ data: apiResponses()[url] ?? [] })
        }, 0)
      })
    })
    vi.spyOn(api, 'post').mockResolvedValue({ data: {} })

    render(<App />)
    await screen.findByText('INV-1001')
    expect(maxInFlight).toBe(DASHBOARD_ENDPOINTS.length)
  })

  it('shows a loading state before the invoices arrive', () => {
    mockApi()
    render(<App />)
    expect(screen.getByText('Loading invoices…')).toBeInTheDocument()
  })

  it('renders the metrics row from the metrics endpoint', async () => {
    await renderApp()
    expect(screen.getByText('Invoices Processed')).toBeInTheDocument()
    expect(screen.getByText('Verified Recovered / Collectable')).toBeInTheDocument()
    expect(screen.getByText('₹4.5L / ₹4.5L')).toBeInTheDocument()
    expect(screen.getByText('API Failures Recovered')).toBeInTheDocument()
  })

  it('shows the suspicious-content counter green at zero and red above it', async () => {
    const { container, unmount } = await renderApp()
    expect(container.querySelector('.tone-green')).toBeInTheDocument()
    expect(container.querySelector('.tone-red')).toBeNull()
    unmount()

    vi.restoreAllMocks()
    const flagged = await renderApp({
      responses: apiResponses({
        '/api/metrics': { ...metrics, suspicious_content_detected_count: 2 },
      }),
    })
    expect(flagged.container.querySelector('.tone-red')).toBeInTheDocument()
  })

  it('opens on the invoices tab with the detail panel empty', async () => {
    await renderApp()
    expect(screen.getByRole('button', { name: 'Invoices' })).toHaveClass('active')
    expect(screen.getByText('Select an invoice to see its full audit trail.')).toBeInTheDocument()
  })
})

describe('App — API failure', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('shows one banner when the backend is unreachable, not four', async () => {
    mockApi({ failures: { '/api/invoices': new Error('Network Error') } })
    render(<App />)
    expect(await screen.findByText(/Failed to reach API: Network Error/)).toBeInTheDocument()
    expect(screen.getAllByText(/Failed to reach API/)).toHaveLength(1)
  })

  it('stops loading rather than spinning forever after a failure', async () => {
    mockApi({ failures: { '/api/invoices': new Error('Network Error') } })
    render(<App />)
    await screen.findByText(/Failed to reach API/)
    await waitFor(() => expect(screen.queryByText('Loading invoices…')).toBeNull())
  })

  it('renders no metrics row when the metrics call failed', async () => {
    // Better an absent counter row than one showing stale or zeroed money.
    mockApi({ failures: { '/api/metrics': new Error('Network Error') } })
    render(<App />)
    await screen.findByText(/Failed to reach API/)
    expect(screen.queryByText('Invoices Processed')).toBeNull()
  })
})

describe('App — audit trail', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('loads the trail for the clicked invoice', async () => {
    const { get } = await renderApp({ trails: { 'INV-1042': blockedTrail } })
    await userEvent.setup().click(screen.getByText('INV-1042'))

    await waitFor(() =>
      expect(get).toHaveBeenCalledWith('/api/invoices/INV-1042/audit')
    )
    expect(await screen.findByText('AUDIT TRAIL')).toBeInTheDocument()
  })

  it('explains a blocked invoice with the WHY NOT panel', async () => {
    await renderApp({ trails: { 'INV-1042': blockedTrail } })
    await userEvent.setup().click(screen.getByText('INV-1042'))

    expect(await screen.findByText('WHY NOT')).toBeInTheDocument()
    const panel = screen.getByText('WHY NOT').closest('.why-not')
    expect(panel.textContent).toContain('dispute_detected')
    expect(within(panel).getByText('blocked')).toBeInTheDocument()
  })

  it('shows the reasoning chain that led to the decision', async () => {
    await renderApp({ trails: { 'INV-1042': blockedTrail } })
    await userEvent.setup().click(screen.getByText('INV-1042'))

    expect(await screen.findByText('REASONING CHAIN')).toBeInTheDocument()
    expect(screen.getByText(/^Thought 5 —/)).toBeInTheDocument()
  })

  it('omits WHY NOT for an allowed invoice', async () => {
    await renderApp({ trails: { 'INV-1001': allowedTrail } })
    await userEvent.setup().click(screen.getByText('INV-1001'))

    await screen.findByText('AUDIT TRAIL')
    expect(screen.queryByText('WHY NOT')).toBeNull()
  })

  it('percent-encodes an invoice id with a slash in it', async () => {
    // Merchant CSVs carry ids like "2026/INV/0042"; an unencoded slash would
    // hit a different route entirely.
    const odd = { ...invoices[0], invoice_id: '2026/INV/0042' }
    const { get } = await renderApp({
      responses: apiResponses({ '/api/invoices': [odd, ...invoices.slice(1)] }),
      trails: { '2026/INV/0042': allowedTrail },
      readyText: '2026/INV/0042',
    })
    await userEvent.setup().click(screen.getByText('2026/INV/0042'))

    await waitFor(() =>
      expect(get).toHaveBeenCalledWith('/api/invoices/2026%2FINV%2F0042/audit')
    )
  })

  it('names the invoice in the error when its trail cannot be loaded', async () => {
    await renderApp({ failures: { audit: new Error('Network Error') } })
    await userEvent.setup().click(screen.getByText('INV-1042'))

    expect(
      await screen.findByText(/Could not load the audit trail for INV-1042/)
    ).toBeInTheDocument()
  })

  it('replaces the open trail when a different invoice is clicked', async () => {
    await renderApp({ trails: { 'INV-1042': blockedTrail, 'INV-1001': allowedTrail } })
    const user = userEvent.setup()

    await user.click(screen.getByText('INV-1042'))
    await screen.findByText('WHY NOT')

    await user.click(screen.getByText('INV-1001'))
    await waitFor(() => expect(screen.queryByText('WHY NOT')).toBeNull())
    expect(screen.getByText('AUDIT TRAIL')).toBeInTheDocument()
  })

  it('closes the panel and returns to the prompt', async () => {
    await renderApp({ trails: { 'INV-1042': blockedTrail } })
    const user = userEvent.setup()
    await user.click(screen.getByText('INV-1042'))
    await screen.findByText('AUDIT TRAIL')

    await user.click(screen.getByRole('button', { name: 'Close audit trail' }))
    expect(screen.getByText('Select an invoice to see its full audit trail.')).toBeInTheDocument()
  })
})

describe('App — tabs and client filter', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('switches to the client health tab', async () => {
    await renderApp()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Client Health' }))

    expect(screen.getByText('Nilgiri Textiles')).toBeInTheDocument()
    expect(screen.getByText('Critical')).toBeInTheDocument()
    expect(screen.queryByText('INV-1001')).toBeNull()
  })

  it('switches to the analytics tab', async () => {
    await renderApp()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Analytics' }))

    expect(screen.getByText('Recovery by Scenario')).toBeInTheDocument()
    expect(screen.getByText('Top Policy Rules')).toBeInTheDocument()
  })

  it('marks exactly one tab active at a time', async () => {
    await renderApp()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Analytics' }))

    expect(screen.getByRole('button', { name: 'Analytics' })).toHaveClass('active')
    expect(screen.getByRole('button', { name: 'Invoices' })).not.toHaveClass('active')
  })

  it('drills from a client health row into that client’s invoices', async () => {
    await renderApp()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Client Health' }))
    await user.click(screen.getByText('Nilgiri Textiles'))

    expect(screen.getByRole('button', { name: 'Invoices' })).toHaveClass('active')
    expect(screen.getByText(/Showing invoices for/)).toBeInTheDocument()
    expect(screen.getByText('INV-1042')).toBeInTheDocument()
    expect(screen.queryByText('INV-1001')).toBeNull()
  })

  it('restores every invoice when the filter is cleared', async () => {
    await renderApp()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Client Health' }))
    await user.click(screen.getByText('Nilgiri Textiles'))
    await user.click(screen.getByRole('button', { name: 'Clear filter' }))

    expect(screen.getByText('INV-1001')).toBeInTheDocument()
    expect(screen.queryByText(/Showing invoices for/)).toBeNull()
  })

  it('keeps the health tab empty-stated when no batch has been scored', async () => {
    await renderApp({ responses: apiResponses({ '/api/client-health': [] }) })
    await userEvent.setup().click(screen.getByRole('button', { name: 'Client Health' }))

    expect(screen.getByText('Run a batch first to generate client health scores.')).toBeInTheDocument()
  })
})

describe('App — running a batch', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('posts to the batch endpoint and then refetches the dashboard', async () => {
    const { get, post } = await renderApp()
    const before = get.mock.calls.length

    await userEvent.setup().click(screen.getByRole('button', { name: 'Run Batch' }))

    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/run-batch'))
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(before))
  })

  it('disables the button and says so while the batch runs', async () => {
    await renderApp()
    let resolvePost
    vi.spyOn(api, 'post').mockReturnValue(new Promise((resolve) => { resolvePost = resolve }))

    await userEvent.setup().click(screen.getByRole('button', { name: 'Run Batch' }))

    const running = await screen.findByRole('button', { name: 'Running batch…' })
    expect(running).toBeDisabled()

    resolvePost({ data: { processed: 4, allow: 1, block: 1, require_human: 2 } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run Batch' })).toBeEnabled())
  })

  it("surfaces the server's reason when a batch fails", async () => {
    await renderApp()
    vi.spyOn(api, 'post').mockRejectedValue({
      response: { data: { detail: 'NVIDIA_API_KEY not configured on server' } },
      message: 'Request failed with status code 500',
    })

    await userEvent.setup().click(screen.getByRole('button', { name: 'Run Batch' }))

    expect(
      await screen.findByText(/NVIDIA_API_KEY not configured on server/)
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run Batch' })).toBeEnabled()
  })

  it('shows a batch-already-running notice on 409, not the generic error banner', async () => {
    await renderApp()
    vi.spyOn(api, 'post').mockRejectedValue({
      response: { status: 409, data: { detail: 'A batch is already running — wait for it to finish before starting another.' } },
      message: 'Request failed with status code 409',
    })

    await userEvent.setup().click(screen.getByRole('button', { name: 'Run Batch' }))

    expect(
      await screen.findByText(/A batch is already running/)
    ).toBeInTheDocument()
    // The 409 case is api/routes.py's guard working as intended, not a
    // failure — it must not also render through the generic error banner,
    // which is worded and styled for an actual API failure.
    expect(screen.queryByText(/Failed to reach API/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Run Batch' })).toBeEnabled()
  })

  it('picks up new rows and new metrics after a batch', async () => {
    const { post } = await renderApp()
    expect(screen.queryByText('INV-2001')).toBeNull()

    const extra = { ...invoices[0], invoice_id: 'INV-2001', client_name: 'Vindhya Ceramics' }
    vi.spyOn(api, 'get').mockImplementation((url) => {
      const responses = apiResponses({
        '/api/invoices': [...invoices, extra],
        '/api/metrics': { ...metrics, total_processed: 5 },
        '/api/client-health': healthScores,
        '/api/analytics': analytics,
      })
      if (url in responses) return Promise.resolve({ data: responses[url] })
      return Promise.resolve({ data: [] })
    })
    post.mockResolvedValue({ data: { processed: 5, allow: 2, block: 1, require_human: 2 } })

    await userEvent.setup().click(screen.getByRole('button', { name: 'Run Batch' }))
    expect(await screen.findByText('INV-2001')).toBeInTheDocument()
  })
})

describe('App — escalation queue', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the open count on the tab so the queue is visible without opening it', async () => {
    await renderApp()
    expect(
      screen.getByRole('button', { name: `Escalations (${escalations.length})` })
    ).toBeInTheDocument()
  })

  it('drops the count from the tab when nothing is awaiting review', async () => {
    await renderApp({ responses: apiResponses({ '/api/escalations': [] }) })
    expect(screen.getByRole('button', { name: 'Escalations' })).toBeInTheDocument()
  })

  it('opens the queue and shows the cases', async () => {
    await renderApp()
    await userEvent.setup().click(
      screen.getByRole('button', { name: /^Escalations/ })
    )

    expect(screen.getByText('INV-1077')).toBeInTheDocument()
    expect(screen.getByText(/awaiting review/)).toBeInTheDocument()
  })

  it('refetches the whole dashboard after a verdict, not just the queue', async () => {
    // A resolution can create an execution, which changes the invoice table
    // and the metrics too — so the refresh has to be the full one.
    const { get, post } = await renderApp()
    post.mockResolvedValue({ data: { resolution: 'rejected' } })
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /^Escalations/ }))

    const before = get.mock.calls.length
    await user.type(screen.getByLabelText('Reviewer name or email'), 'cfo@example.com')
    await user.click(screen.getAllByRole('button', { name: 'Reject' })[0])

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(get.mock.calls.length).toBeGreaterThanOrEqual(
        before + DASHBOARD_ENDPOINTS.length
      )
    )
  })
})

describe('App — data source panels', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('offers both ingestion routes side by side', async () => {
    await renderApp()
    expect(screen.getByText('Sync from Razorpay', { selector: '.rzp-sync-title' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sync from Razorpay' })).toBeInTheDocument()
    expect(screen.getByText('Upload Invoice CSV')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Upload & Process/ })).toBeInTheDocument()
  })

  it('refetches the dashboard after a CSV upload', async () => {
    const { get, post } = await renderApp()
    post.mockResolvedValue({
      data: { processed: 1, allow: 1, block: 0, require_human: 0, warnings: [] },
    })
    const before = get.mock.calls.length

    const user = userEvent.setup()
    await user.upload(
      screen.getByLabelText('Invoice CSV file'),
      new File(['invoice_id\nINV-9\n'], 'invoices.csv', { type: 'text/csv' })
    )
    await user.click(screen.getByRole('button', { name: /Upload & Process/ }))

    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/upload-csv', expect.any(FormData)))
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(before))
  })

  it('refetches the dashboard after a Razorpay sync', async () => {
    const { get, post } = await renderApp()
    post.mockResolvedValue({
      data: { invoices_fetched: 2, processed: 2, allow: 1, block: 1, require_human: 0, warnings: [] },
    })
    const before = get.mock.calls.length

    await userEvent.setup().click(screen.getByRole('button', { name: 'Sync from Razorpay' }))

    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/sync-razorpay'))
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(before))
  })
})
