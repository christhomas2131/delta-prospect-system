import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch, makeLatest } from './api'

function mockFetch(status, body, headers = {}) {
  globalThis.fetch = vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => headers[k.toLowerCase()] ?? null },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  }))
}

afterEach(() => { vi.restoreAllMocks() })

describe('apiFetch', () => {
  it('returns parsed JSON on success', async () => {
    mockFetch(200, { ok: true })
    expect(await apiFetch('/api/x')).toEqual({ ok: true })
  })

  it('maps a 409 into a conflict ApiError with the server detail', async () => {
    mockFetch(409, { detail: { message: 'stale', current_version: 4 } }, { 'x-request-id': 'abc' })
    const err = await apiFetch('/api/x', { method: 'PATCH', body: {} }).catch(e => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.isConflict).toBe(true)
    expect(err.message).toBe('stale')
    expect(err.detail.current_version).toBe(4)
    expect(err.requestId).toBe('abc')
  })

  it('does not report a failed save as success', async () => {
    mockFetch(500, { detail: 'boom' })
    await expect(apiFetch('/api/x', { method: 'POST', body: { a: 1 } })).rejects.toMatchObject({ status: 500, message: 'boom' })
  })

  it('wraps network failures as status 0', async () => {
    globalThis.fetch = vi.fn(async () => { throw new TypeError('Failed to fetch') })
    const err = await apiFetch('/api/x').catch(e => e)
    expect(err.isNetwork).toBe(true)
  })

  it('sends JSON bodies with the right header', async () => {
    mockFetch(200, {})
    await apiFetch('/api/x', { method: 'PATCH', body: { owner: 'me' } })
    const [, init] = globalThis.fetch.mock.calls[0]
    expect(init.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(init.body)).toEqual({ owner: 'me' })
  })
})

describe('makeLatest', () => {
  it('aborts the previous request when a newer one starts', () => {
    const next = makeLatest()
    const first = next()
    const second = next()
    expect(first.aborted).toBe(true)
    expect(second.aborted).toBe(false)
  })
})
