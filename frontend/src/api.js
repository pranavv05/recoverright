import axios from 'axios'

// Overridable with VITE_API_BASE_URL so a build can point at something other
// than a local uvicorn without editing this file.
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
})

// Axios puts a FastAPI HTTPException's `detail` on the response body; a
// network failure has no response at all and only carries a message. Every
// error surface in the dashboard needs both, so it lives in one place.
export function errorMessage(e, fallback) {
  return e?.response?.data?.detail || e?.message || fallback || 'Unknown error'
}

export default api
