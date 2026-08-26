import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CsvUpload from './CsvUpload'
import api from '../api'

const SAMPLE_CSV = 'invoice_id,client_name,amount\nINV-1,Acme,1000\nINV-2,Globex,2000\n'

function csvFile(contents = SAMPLE_CSV, name = 'invoices.csv') {
  return new File([contents], name, { type: 'text/csv' })
}

function pickFile(file) {
  return userEvent.setup().upload(screen.getByLabelText('Invoice CSV file'), file)
}

const uploadButton = () => screen.getByRole('button', { name: /Upload & Process/ })

describe('CsvUpload', () => {
  let onUploaded

  beforeEach(() => {
    onUploaded = vi.fn().mockResolvedValue(undefined)
  })

  it('keeps the upload button disabled until a file is chosen', () => {
    render(<CsvUpload onUploaded={onUploaded} />)
    expect(uploadButton()).toBeDisabled()
  })

  it('enables the button once a CSV is chosen', async () => {
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    expect(uploadButton()).toBeEnabled()
  })

  it('rejects a non-CSV file before any request is made', async () => {
    // fireEvent, not userEvent.upload: userEvent honours the input's
    // accept=".csv" and silently drops the file, so the component's own
    // extension check would never run. A real browser's file picker can be
    // told to show all files, which is exactly the case this guards.
    const post = vi.spyOn(api, 'post')
    render(<CsvUpload onUploaded={onUploaded} />)
    const input = screen.getByLabelText('Invoice CSV file')
    fireEvent.change(input, {
      target: { files: [new File(['{}'], 'invoices.json', { type: 'application/json' })] },
    })

    expect(screen.getByText('Please upload a CSV file')).toBeInTheDocument()
    expect(uploadButton()).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })

  it('clears the selected file when the replacement is not a CSV', async () => {
    // Otherwise the previous valid file would still be uploaded under the
    // impression that the newly picked one was.
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    expect(uploadButton()).toBeEnabled()

    fireEvent.change(screen.getByLabelText('Invoice CSV file'), {
      target: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] },
    })
    expect(uploadButton()).toBeDisabled()
  })

  it('disables the upload again when the file picker is cancelled', async () => {
    // A cancelled picker fires change with an empty FileList. Leaving the
    // previous file selected would upload it on the next click.
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    expect(uploadButton()).toBeEnabled()

    fireEvent.change(screen.getByLabelText('Invoice CSV file'), { target: { files: [] } })
    expect(uploadButton()).toBeDisabled()
    expect(screen.queryByText('Please upload a CSV file')).toBeNull()
  })

  it('never posts an upload with no file attached', async () => {
    const post = vi.spyOn(api, 'post')
    render(<CsvUpload onUploaded={onUploaded} />)
    await userEvent.setup().click(uploadButton())
    expect(post).not.toHaveBeenCalled()
  })

  it('accepts a .CSV extension regardless of case', async () => {
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile(SAMPLE_CSV, 'INVOICES.CSV'))
    expect(screen.queryByText('Please upload a CSV file')).toBeNull()
    expect(uploadButton()).toBeEnabled()
  })

  it('posts the file as multipart form data to the upload endpoint', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({
      data: { processed: 2, allow: 1, block: 1, require_human: 0, warnings: [] },
    })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [url, body] = post.mock.calls[0]
    expect(url).toBe('/api/upload-csv')
    expect(body).toBeInstanceOf(FormData)
    expect(body.get('file')).toBeInstanceOf(File)
  })

  it('reports the decision breakdown the server returned', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { processed: 12, allow: 4, block: 3, require_human: 5, warnings: [] },
    })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    expect(await screen.findByText('12')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText(/ALLOW/)).toBeInTheDocument()
  })

  it('refreshes the dashboard after a successful upload', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { processed: 1, allow: 1, block: 0, require_human: 0, warnings: [] },
    })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(1))
  })

  it('lists the rows the server skipped as duplicates', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: {
        processed: 1,
        allow: 1,
        block: 0,
        require_human: 0,
        warnings: ['invoice_id INV-1 already exists in database — skipped'],
      },
    })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    expect(await screen.findByText(/1 invoice skipped/)).toBeInTheDocument()
    expect(
      screen.getByText('invoice_id INV-1 already exists in database — skipped')
    ).toBeInTheDocument()
  })

  it('pluralises the skipped-row count correctly', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { processed: 0, allow: 0, block: 0, require_human: 0, warnings: ['a', 'b'] },
    })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    expect(await screen.findByText(/2 invoices skipped/)).toBeInTheDocument()
  })

  it("surfaces FastAPI's detail message when the server rejects the file", async () => {
    vi.spyOn(api, 'post').mockRejectedValue({
      response: { data: { detail: 'No valid invoice rows found in CSV' } },
      message: 'Request failed with status code 400',
    })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    expect(await screen.findByText('No valid invoice rows found in CSV')).toBeInTheDocument()
    expect(onUploaded).not.toHaveBeenCalled()
  })

  it('falls back to the axios message when the failure carries no response', async () => {
    vi.spyOn(api, 'post').mockRejectedValue({ message: 'Network Error' })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    expect(await screen.findByText('Network Error')).toBeInTheDocument()
  })

  it('re-enables the button after a failure so the upload can be retried', async () => {
    vi.spyOn(api, 'post').mockRejectedValue({ message: 'Network Error' })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    await screen.findByText('Network Error')
    expect(uploadButton()).toBeEnabled()
  })

  it('clears a stale result when a new file is chosen', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { processed: 7, allow: 5, block: 1, require_human: 1, warnings: [] },
    })
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())
    await screen.findByText('7')

    await pickFile(csvFile(SAMPLE_CSV, 'other.csv'))
    expect(screen.queryByText('7')).toBeNull()
  })

  it('disables the controls and counts the rows while the upload is in flight', async () => {
    let resolvePost
    vi.spyOn(api, 'post').mockReturnValue(new Promise((resolve) => { resolvePost = resolve }))
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile())
    await userEvent.setup().click(uploadButton())

    // Two data rows behind one header row.
    expect(await screen.findByText('Processing 2 invoices…')).toBeInTheDocument()
    expect(screen.getByLabelText('Invoice CSV file')).toBeDisabled()
    expect(screen.getByRole('button', { name: /Download Sample CSV/ })).toBeDisabled()

    resolvePost({ data: { processed: 2, allow: 2, block: 0, require_human: 0, warnings: [] } })
    await waitFor(() => expect(screen.queryByText('Processing 2 invoices…')).toBeNull())
  })

  it('ignores blank trailing lines when counting rows', async () => {
    let resolvePost
    vi.spyOn(api, 'post').mockReturnValue(new Promise((resolve) => { resolvePost = resolve }))
    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(csvFile('h1,h2\r\na,b\r\n\r\n\r\n'))
    await userEvent.setup().click(uploadButton())

    expect(await screen.findByText('Processing 1 invoices…')).toBeInTheDocument()
    resolvePost({ data: { processed: 1, allow: 1, block: 0, require_human: 0, warnings: [] } })
  })

  it('still uploads when the row count cannot be read locally', async () => {
    // The count only labels the progress line — the server is the authority.
    const post = vi.spyOn(api, 'post').mockResolvedValue({
      data: { processed: 3, allow: 2, block: 1, require_human: 0, warnings: [] },
    })
    const file = csvFile()
    vi.spyOn(file, 'text').mockRejectedValue(new Error('unreadable'))

    render(<CsvUpload onUploaded={onUploaded} />)
    await pickFile(file)
    await userEvent.setup().click(uploadButton())

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('3')).toBeInTheDocument()
  })

  it('sends the browser to the sample CSV endpoint on the configured API host', async () => {
    // jsdom refuses to navigate and leaves location.href untouched, so the
    // navigation target is captured on a stub instead.
    const location = { href: '' }
    vi.stubGlobal('location', location)

    render(<CsvUpload onUploaded={onUploaded} />)
    await userEvent.setup().click(screen.getByRole('button', { name: /Download Sample CSV/ }))

    expect(location.href).toBe(`${api.defaults.baseURL}/api/sample-csv`)
    expect(location.href).toContain('/api/sample-csv')
  })
})
