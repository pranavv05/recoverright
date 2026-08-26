import { describe, expect, it } from 'vitest'
import api, { errorMessage } from './api'

describe('api client', () => {
  it('points at the local backend by default', () => {
    // VITE_API_BASE_URL overrides this at build time; nothing in the test
    // environment sets it, so this is the fallback under test.
    expect(api.defaults.baseURL).toBe('http://localhost:8000')
  })

  it('exports the same instance as the default and named binding', async () => {
    const mod = await import('./api')
    expect(mod.default).toBe(mod.api)
  })
})

describe('errorMessage', () => {
  it("prefers FastAPI's detail, which explains what the server actually refused", () => {
    const e = {
      response: { data: { detail: 'Please upload a CSV file' } },
      message: 'Request failed with status code 400',
    }
    expect(errorMessage(e)).toBe('Please upload a CSV file')
  })

  it('falls back to the transport message when there is no response body', () => {
    expect(errorMessage({ message: 'Network Error' })).toBe('Network Error')
  })

  it('falls back to the caller-supplied text when the error carries neither', () => {
    expect(errorMessage({}, 'Batch run failed')).toBe('Batch run failed')
  })

  it('never returns undefined, so an error banner is never blank', () => {
    expect(errorMessage(null)).toBe('Unknown error')
    expect(errorMessage(undefined)).toBe('Unknown error')
    expect(errorMessage({ response: {} })).toBe('Unknown error')
  })
})
