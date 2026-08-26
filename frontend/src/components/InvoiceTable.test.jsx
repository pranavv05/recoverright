import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import InvoiceTable from './InvoiceTable'
import { invoices } from '../test/fixtures'

function renderTable(props = {}) {
  const onSelectInvoice = vi.fn()
  const onClearFilter = vi.fn()
  const utils = render(
    <InvoiceTable
      invoices={invoices}
      loading={false}
      selectedId={null}
      clientFilter={null}
      onSelectInvoice={onSelectInvoice}
      onClearFilter={onClearFilter}
      {...props}
    />
  )
  return { ...utils, onSelectInvoice, onClearFilter }
}

describe('InvoiceTable', () => {
  it('shows a loading message instead of an empty table while fetching', () => {
    renderTable({ loading: true })
    expect(screen.getByText('Loading invoices…')).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('renders one row per invoice with its id and client', () => {
    renderTable()
    const rows = screen.getAllByRole('row').slice(1) // drop the header row
    expect(rows).toHaveLength(invoices.length)
    expect(screen.getByText('INV-1001')).toBeInTheDocument()
    expect(screen.getByText(/Sahyadri Precision Auto/)).toBeInTheDocument()
  })

  it('formats the outstanding balance in the Indian numbering system', () => {
    renderTable()
    expect(screen.getByText(/₹4,50,000/)).toBeInTheDocument()
  })

  it('shows every decision the engine produced, not just the allowed ones', () => {
    renderTable()
    expect(screen.getByText('ALLOW')).toBeInTheDocument()
    expect(screen.getByText('BLOCK')).toBeInTheDocument()
    expect(screen.getAllByText('REQUIRE_HUMAN')).toHaveLength(2)
  })

  it('reports the clicked invoice id to its parent', async () => {
    const user = userEvent.setup()
    const { onSelectInvoice } = renderTable()
    await user.click(screen.getByText('INV-1042'))
    expect(onSelectInvoice).toHaveBeenCalledWith('INV-1042')
  })

  it('marks the selected row so the detail panel and the table agree', () => {
    const { container } = renderTable({ selectedId: 'INV-1042' })
    const selected = container.querySelectorAll('.row-selected')
    expect(selected).toHaveLength(1)
    expect(within(selected[0]).getByText('INV-1042')).toBeInTheDocument()
  })

  it('shows only the filtered client when a client filter is active', () => {
    renderTable({ clientFilter: 'Nilgiri Textiles' })
    expect(screen.getByText('INV-1042')).toBeInTheDocument()
    expect(screen.queryByText('INV-1001')).toBeNull()
    expect(screen.getAllByRole('row').slice(1)).toHaveLength(1)
  })

  it('names the filtered client in the banner it shows', () => {
    renderTable({ clientFilter: 'Nilgiri Textiles' })
    expect(screen.getByText(/Showing invoices for/)).toBeInTheDocument()
    expect(screen.getByText('Nilgiri Textiles')).toBeInTheDocument()
  })

  it('renders no banner when nothing is filtered', () => {
    const { container } = renderTable()
    expect(container.querySelector('.client-filter-banner')).toBeNull()
  })

  it('asks its parent to clear the filter rather than clearing it locally', async () => {
    const user = userEvent.setup()
    const { onClearFilter } = renderTable({ clientFilter: 'Nilgiri Textiles' })
    await user.click(screen.getByRole('button', { name: 'Clear filter' }))
    expect(onClearFilter).toHaveBeenCalledTimes(1)
  })

  it('renders an empty body without crashing when a filter matches nobody', () => {
    renderTable({ clientFilter: 'A Client That Does Not Exist' })
    expect(screen.getAllByRole('row')).toHaveLength(1) // header only
  })
})
