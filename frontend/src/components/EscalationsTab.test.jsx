import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EscalationsTab from './EscalationsTab'
import api from '../api'
import {
  disputeEscalation,
  escalationCase,
  escalations,
  lowPriorityEscalation,
} from '../test/fixtures'

// The queue is the only screen where a person can authorise the system to act,
// so the tests that matter are about what it will and will not let them do.
// The permission set is the server's: the UI renders buttons from
// `approvable_actions` and sends back exactly one of those strings. There is
// no client-side allowlist here to drift out of step with the policy engine,
// and these tests are what stop one from appearing.

function renderTab(props = {}) {
  const onResolved = vi.fn().mockResolvedValue(undefined)
  const utils = render(
    <EscalationsTab
      escalations={escalations}
      loading={false}
      onResolved={onResolved}
      {...props}
    />
  )
  return { ...utils, onResolved }
}

function cardFor(invoiceId) {
  return screen.getByText(invoiceId).closest('.esc-card')
}

async function signIn(user, who = 'finance@example.com') {
  const input = screen.getByLabelText('Reviewer name or email')
  await user.clear(input)
  await user.type(input, who)
}

describe('EscalationsTab — the queue', () => {
  it('shows a loading message rather than an empty queue', () => {
    renderTab({ loading: true })
    expect(screen.getByText('Loading escalations…')).toBeInTheDocument()
  })

  it('says the queue is clear rather than showing nothing', () => {
    // An empty table reads as broken; an empty state reads as done.
    renderTab({ escalations: [] })
    expect(screen.getByText(/Nothing awaiting review/)).toBeInTheDocument()
  })

  it('pre-fills the reviewer field so a decision can be recorded immediately', () => {
    renderTab()
    expect(screen.getByLabelText('Reviewer name or email')).toHaveValue('Priya (Finance Lead)')
  })

  it('shows who is reviewing above each card\'s actions', () => {
    renderTab()
    const card = cardFor('INV-1077')
    expect(within(card).getByText('Reviewing as: Priya (Finance Lead)')).toBeInTheDocument()
  })

  it('renders one card per open escalation', () => {
    const { container } = renderTab()
    expect(container.querySelectorAll('.esc-card')).toHaveLength(escalations.length)
  })

  it('orders the queue by priority, not by amount', () => {
    // INV-1042 is URGENT at ₹12.8L; INV-1077 is HIGH at ₹520L. Urgency wins —
    // a legal or dispute signal outranks a large but ordinary balance.
    const { container } = renderTab()
    const ids = [...container.querySelectorAll('.esc-card-invoice')].map((n) => n.textContent)
    expect(ids).toEqual(['INV-1042', 'INV-1077', 'INV-1099'])
  })

  it('summarises queue depth and the money under review', () => {
    renderTab()
    expect(screen.getByText('3')).toBeInTheDocument()
    // 5,20,00,000 + 12,80,000 + 9,000 paise, as exact rupees
    expect(screen.getByText('₹5,32,89,000')).toBeInTheDocument()
  })

  it('shows why each invoice was escalated', () => {
    renderTab()
    const card = cardFor('INV-1077')
    expect(within(card).getByText('Strategic account requires human approval')).toBeInTheDocument()
  })

  it('shows what the agent proposed alongside the refusal', () => {
    // The reviewer needs the recommendation to judge it, not just the verdict.
    renderTab()
    const card = cardFor('INV-1077')
    expect(within(card).getByText(/Send reminder/)).toBeInTheDocument()
    expect(within(card).getByText(/diagnosed as Cash flow delay/)).toBeInTheDocument()
    expect(within(card).getByText(/Client mentioned a funding delay/)).toBeInTheDocument()
  })

  it('quotes the client note in full, not truncated', () => {
    renderTab()
    const card = cardFor('INV-1077')
    expect(
      within(card).getByText(/Funding round closes next week, will clear right after\./)
    ).toBeInTheDocument()
  })

  it('renders the priority badge for each tier', () => {
    const { container } = renderTab()
    expect(container.querySelector('.badge-priority-urgent')).toBeInTheDocument()
    expect(container.querySelector('.badge-priority-high')).toBeInTheDocument()
    expect(container.querySelector('.badge-priority-low')).toBeInTheDocument()
  })

  it('renders an unrecognised priority raw and sorts it last', () => {
    // A new priority in the worker must be visible, not blank — and it must
    // not jump the queue ahead of an URGENT case.
    const { container } = renderTab({
      escalations: [
        { ...escalationCase, invoice_id: 'INV-NEW', priority: 'SOMEDAY' },
        { ...disputeEscalation },
      ],
    })
    expect(screen.getByText('SOMEDAY')).toBeInTheDocument()
    const ids = [...container.querySelectorAll('.esc-card-invoice')].map((n) => n.textContent)
    expect(ids).toEqual(['INV-1042', 'INV-NEW'])
  })

  it('orders equal-priority cases by outstanding balance, largest first', () => {
    const { container } = renderTab({
      escalations: [
        { ...escalationCase, invoice_id: 'INV-SMALL', outstanding_balance_paise: 100000 },
        { ...escalationCase, invoice_id: 'INV-LARGE', outstanding_balance_paise: 900000 },
      ],
    })
    const ids = [...container.querySelectorAll('.esc-card-invoice')].map((n) => n.textContent)
    expect(ids).toEqual(['INV-LARGE', 'INV-SMALL'])
  })

  it('says "nothing" when the engine permits no action at all', () => {
    renderTab({
      escalations: [{ ...disputeEscalation, allowed_actions: [], approvable_actions: [] }],
    })
    expect(screen.getByText(/allows only nothing/)).toBeInTheDocument()
  })

  it('omits the evidence tier when the diagnosis carries none', () => {
    const { container } = renderTab({
      escalations: [{ ...escalationCase, evidence_tier: null }],
    })
    expect(container.textContent).toContain('diagnosed as Cash flow delay')
    expect(container.textContent).not.toContain('(null)')
  })
})

describe('EscalationsTab — what a reviewer may authorise', () => {
  it('offers only the actions the server marked approvable', () => {
    renderTab()
    const card = cardFor('INV-1077')
    const options = [...within(card).getByRole('combobox').options].map((o) => o.value)

    expect(options).toEqual(['gather_information'])
    // Permitted by the engine but a handoff to a person — not offered.
    expect(options).not.toContain('escalate_to_human')
  })

  it('offers every approvable action when there is more than one', () => {
    renderTab()
    const card = cardFor('INV-1099')
    const options = [...within(card).getByRole('combobox').options].map((o) => o.value)
    expect(options).toEqual(['gather_information', 'send_diagnostic_reminder'])
  })

  it('shows no Approve control at all on a dispute', () => {
    // The engine permits only handoffs here, which is what this review already
    // is. A disabled button would imply the action exists and is merely
    // unavailable; the honest control is a sentence.
    renderTab()
    const card = cardFor('INV-1042')
    expect(within(card).queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(within(card).queryByRole('combobox')).toBeNull()
    expect(within(card).getByText(/No automated action is permitted/)).toBeInTheDocument()
  })

  it('still lets a dispute be rejected or deferred', () => {
    // The reviewer handles it personally; the verdict still gets recorded.
    renderTab()
    const card = cardFor('INV-1042')
    expect(within(card).getByRole('button', { name: 'Reject' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Defer' })).toBeInTheDocument()
  })

  it('never offers a customer-facing action anywhere in the queue', () => {
    // The invariant api/escalations.py proves over the whole corpus, asserted
    // here at the surface a person actually clicks.
    renderTab()
    for (const box of screen.queryAllByRole('combobox')) {
      const options = [...box.options].map((o) => o.value)
      expect(options).not.toContain('send_reminder')
      expect(options).not.toContain('create_payment_link')
    }
  })
})

describe('EscalationsTab — resolving', () => {
  let post

  beforeEach(() => {
    post = vi.spyOn(api, 'post').mockResolvedValue({ data: { resolution: 'approved' } })
  })

  it('refuses to record a verdict nobody signed', async () => {
    // An unattributed approval is not an audit trail — the server enforces
    // this too, but failing here saves a round trip and says so plainly.
    const user = userEvent.setup()
    renderTab()
    await user.clear(screen.getByLabelText('Reviewer name or email'))
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Approve' }))

    expect(await screen.findByText(/Enter your name or email/)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('posts the reviewer, the resolution and the chosen action', async () => {
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post).toHaveBeenCalledWith('/api/escalations/INV-1077/resolve', {
      resolution: 'approved',
      actor: 'finance@example.com',
      note: '',
      action: 'gather_information',
    })
  })

  it('sends the action the reviewer actually selected', async () => {
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    const card = cardFor('INV-1099')
    await user.selectOptions(within(card).getByRole('combobox'), 'send_diagnostic_reminder')
    await user.click(within(card).getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1].action).toBe('send_diagnostic_reminder')
  })

  it('sends no action on a rejection', async () => {
    // A rejection must not smuggle the selected action through.
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Reject' }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toMatchObject({ resolution: 'rejected', action: null })
  })

  it('sends no action on a deferral', async () => {
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Defer' }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toMatchObject({ resolution: 'deferred', action: null })
  })

  it('includes the reviewer note', async () => {
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    const card = cardFor('INV-1077')
    await user.type(within(card).getByLabelText('Note for INV-1077'), 'Spoke to their CFO.')
    await user.click(within(card).getByRole('button', { name: 'Reject' }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1].note).toBe('Spoke to their CFO.')
  })

  it('percent-encodes an invoice id with a slash in it', async () => {
    const user = userEvent.setup()
    renderTab({ escalations: [{ ...escalationCase, invoice_id: '2026/INV/0042' }] })
    await signIn(user)
    await user.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][0]).toBe('/api/escalations/2026%2FINV%2F0042/resolve')
  })

  it('refreshes the queue after a verdict is recorded', async () => {
    const user = userEvent.setup()
    const { onResolved } = renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1))
  })

  it("surfaces the server's refusal when an action is not permitted", async () => {
    // The 422 the API returns when a reviewer asks for something the engine
    // never allowed. It must be shown, not swallowed into a generic failure.
    post.mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail:
            "the policy engine does not permit 'send_reminder' on INV-1077 — a "
            + 'reviewer may authorise only: gather_information',
        },
      },
      message: 'Request failed with status code 422',
    })
    const user = userEvent.setup()
    const { onResolved } = renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Approve' }))

    expect(await screen.findByText(/does not permit 'send_reminder'/)).toBeInTheDocument()
    expect(onResolved).not.toHaveBeenCalled()
  })

  it('surfaces a stale-case conflict', async () => {
    post.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'INV-1077 was re-processed and its current policy decision is ALLOW' },
      },
      message: 'Request failed with status code 409',
    })
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Defer' }))

    expect(await screen.findByText(/was re-processed/)).toBeInTheDocument()
  })

  it('falls back to a named error when the API is unreachable', async () => {
    post.mockRejectedValue({ message: 'Network Error' })
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Reject' }))

    expect(await screen.findByText('Network Error')).toBeInTheDocument()
  })

  it('re-enables the card after a failure so the verdict can be retried', async () => {
    post.mockRejectedValue({ message: 'Network Error' })
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    const card = cardFor('INV-1077')
    await user.click(within(card).getByRole('button', { name: 'Reject' }))

    await screen.findByText('Network Error')
    expect(within(card).getByRole('button', { name: 'Reject' })).toBeEnabled()
  })

  it('disables the whole card while a verdict is in flight', async () => {
    let resolvePost
    post.mockReturnValue(new Promise((resolve) => { resolvePost = resolve }))
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    const card = cardFor('INV-1077')
    await user.click(within(card).getByRole('button', { name: 'Approve' }))

    expect(await within(card).findByRole('button', { name: 'Approving…' })).toBeDisabled()
    expect(within(card).getByRole('button', { name: 'Reject' })).toBeDisabled()
    expect(within(card).getByRole('combobox')).toBeDisabled()

    resolvePost({ data: {} })
    await waitFor(() =>
      expect(within(card).getByRole('button', { name: 'Approve' })).toBeEnabled()
    )
  })

  it('leaves the other cards usable while one is in flight', async () => {
    // Reviewing is a queue, not a modal — one slow request must not freeze it.
    post.mockReturnValue(new Promise(() => {}))
    const user = userEvent.setup()
    renderTab()
    await signIn(user)
    await user.click(within(cardFor('INV-1077')).getByRole('button', { name: 'Approve' }))

    await within(cardFor('INV-1077')).findByRole('button', { name: 'Approving…' })
    expect(within(cardFor('INV-1099')).getByRole('button', { name: 'Approve' })).toBeEnabled()
  })

  it('does not send a stale action after the queue changes underneath it', async () => {
    // The dispute card has nothing approvable; re-rendering with only it must
    // not leave an Approve control behind from the previous list.
    const { rerender } = renderTab()
    rerender(
      <EscalationsTab
        escalations={[disputeEscalation]}
        loading={false}
        onResolved={vi.fn()}
      />
    )
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('omits the note and evidence blocks when the invoice has neither', () => {
    const { container } = renderTab({ escalations: [lowPriorityEscalation] })
    expect(container.querySelector('.esc-note')).toBeNull()
    expect(container.textContent).not.toContain('Evidence:')
    // No diagnosed cause either — and it must read as absent, not as "null".
    expect(container.textContent).not.toContain('diagnosed as')
    expect(container.textContent).not.toContain('null')
  })

  it('renders an em dash when the agent proposed nothing at all', () => {
    // A fail-closed escalation has a decision and no recommendation.
    renderTab({
      escalations: [{ ...lowPriorityEscalation, recommended_action: null }],
    })
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
