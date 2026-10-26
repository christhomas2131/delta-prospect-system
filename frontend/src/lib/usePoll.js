import { useEffect, useRef } from 'react'
import { apiFetch } from './api'

/**
 * Poll `path` every `interval` ms while `enabled`.
 * - Backs off (x2, capped at `maxInterval`) after consecutive errors, resets on success.
 * - `onData(data)` is called for each successful response; return `false` to stop.
 * - Cleans up on unmount or when `enabled` turns false.
 */
export function usePoll(path, { interval = 3000, maxInterval = 30000, enabled = true, onData } = {}) {
  const timer = useRef(null)
  const failures = useRef(0)
  const latest = useRef(onData)
  useEffect(() => { latest.current = onData }, [onData])

  useEffect(() => {
    if (!enabled || !path) return undefined
    let stopped = false

    const schedule = (delay) => {
      timer.current = setTimeout(tick, delay)
    }

    const tick = async () => {
      if (stopped) return
      try {
        const data = await apiFetch(path)
        failures.current = 0
        const keepGoing = latest.current ? latest.current(data) : true
        if (keepGoing === false || stopped) return
        schedule(interval)
      } catch {
        failures.current += 1
        if (stopped) return
        schedule(Math.min(interval * 2 ** failures.current, maxInterval))
      }
    }

    tick()
    return () => {
      stopped = true
      if (timer.current) clearTimeout(timer.current)
      timer.current = null
    }
  }, [path, interval, maxInterval, enabled])
}

export function backoffDelay(base, failures, max = 30000) {
  return Math.min(base * 2 ** failures, max)
}
