// Shared API client: consistent error objects, JSON handling, abort support.
// Every call returns parsed JSON or throws ApiError({status, detail, requestId}).

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null, requestId = null, body = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.requestId = requestId
    this.body = body
  }
  get isConflict() { return this.status === 409 }
  get isNetwork() { return this.status === 0 }
}

function describe(status, body) {
  if (body && typeof body.detail === 'string') return body.detail
  if (body && body.detail && typeof body.detail === 'object' && body.detail.message) return body.detail.message
  if (status === 401) return 'Sign in required'
  if (status === 402) return 'This feature needs an API key — see Settings'
  if (status === 404) return 'Not found'
  if (status === 409) return 'Changed by someone else — reload and retry'
  if (status === 503) return 'Service temporarily unavailable'
  if (status >= 500) return 'Server error'
  return `Request failed (${status})`
}

export async function apiFetch(path, { method = 'GET', body, signal, headers = {} } = {}) {
  let response
  try {
    response = await fetch(path, {
      method,
      signal,
      headers: body !== undefined ? { 'Content-Type': 'application/json', ...headers } : headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (err) {
    if (err && err.name === 'AbortError') throw err
    throw new ApiError('Cannot reach the API — is the backend running?', { status: 0 })
  }
  const requestId = response.headers.get('x-request-id')
  const text = await response.text()
  let parsed = null
  if (text) {
    try { parsed = JSON.parse(text) } catch { parsed = { raw: text } }
  }
  if (!response.ok) {
    throw new ApiError(describe(response.status, parsed), {
      status: response.status, detail: parsed && parsed.detail, requestId, body: parsed,
    })
  }
  return parsed
}

export const api = {
  get: (path, opts) => apiFetch(path, { ...opts, method: 'GET' }),
  post: (path, body, opts) => apiFetch(path, { ...opts, method: 'POST', body }),
  patch: (path, body, opts) => apiFetch(path, { ...opts, method: 'PATCH', body }),
  put: (path, body, opts) => apiFetch(path, { ...opts, method: 'PUT', body }),
}

// Keeps only the newest in-flight request for a key alive; older ones are aborted.
export function makeLatest() {
  let controller = null
  return () => {
    if (controller) controller.abort()
    controller = new AbortController()
    return controller.signal
  }
}
