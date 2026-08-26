import { useState } from 'react'
import api, { errorMessage } from '../api'

export default function CsvUpload({ onUploaded }) {
  const [file, setFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [rowCount, setRowCount] = useState(null)

  const handleFileChange = (e) => {
    const picked = e.target.files?.[0] || null
    setError(null)
    setResult(null)
    if (picked && !picked.name.toLowerCase().endsWith('.csv')) {
      setFile(null)
      setError('Please upload a CSV file')
      return
    }
    setFile(picked)
  }

  const handleUpload = async () => {
    if (!file) {
      setError('Please choose a CSV file first')
      return
    }
    setError(null)
    setResult(null)

    // Counted locally only to label the progress line. A file we cannot read
    // still uploads — the server is the authority on the row count.
    try {
      const text = await file.text()
      const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0)
      setRowCount(Math.max(lines.length - 1, 0))
    } catch {
      setRowCount(null)
    }

    setUploading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const res = await api.post('/api/upload-csv', formData)
      setResult(res.data)
      await onUploaded()
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setUploading(false)
    }
  }

  const downloadSample = () => {
    window.location.href = `${api.defaults.baseURL}/api/sample-csv`
  }

  return (
    <div className="csv-upload">
      <div className="csv-upload-head">
        <div className="csv-upload-title">Upload Invoice CSV</div>
        <div className="csv-upload-sub">
          Export overdue invoices from Tally, Zoho Books, or Excel and upload the CSV directly.
        </div>
      </div>
      <div className="csv-upload-controls">
        <input
          type="file"
          accept=".csv"
          aria-label="Invoice CSV file"
          onChange={handleFileChange}
          disabled={uploading}
          className="csv-file-input"
        />
        <button className="csv-upload-btn" onClick={handleUpload} disabled={uploading || !file}>
          {uploading ? 'Processing…' : 'Upload & Process'}
        </button>
        <button className="csv-sample-btn" onClick={downloadSample} disabled={uploading}>
          Download Sample CSV
        </button>
      </div>

      {uploading && (
        <div className="csv-processing">
          Processing {rowCount !== null ? `${rowCount} invoices` : 'invoices'}…
        </div>
      )}

      {error && <div className="csv-error">{error}</div>}

      {result && !uploading && (
        <div className="csv-result">
          <span><strong>{result.processed}</strong> processed</span>
          <span className="csv-result-allow"><strong>{result.allow}</strong> ALLOW</span>
          <span className="csv-result-block"><strong>{result.block}</strong> BLOCK</span>
          <span className="csv-result-human"><strong>{result.require_human}</strong> REQUIRE_HUMAN</span>
        </div>
      )}

      {result && !uploading && result.warnings?.length > 0 && (
        <div className="csv-warning-box">
          <div className="csv-warning-box-head">
            ⚠ {result.warnings.length} invoice{result.warnings.length === 1 ? '' : 's'} skipped (already processed):
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
