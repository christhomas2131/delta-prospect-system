import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { STAGES } from '../lib/format'

const inputStyle = {
  background: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)',
  padding: '6px 8px', fontSize: 12, width: '100%', outline: 'none',
}

function relTime(iso) {
  if (!iso) return ''
  const d = (Date.now() - new Date(iso).getTime()) / 1000
  if (d < 60) return 'just now'
  if (d < 3600) return `${Math.floor(d / 60)}m ago`
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`
  return `${Math.floor(d / 86400)}d ago`
}

function describeActivity(a) {
  switch (a.activity_type) {
    case 'field_change': return `${a.field} → ${a.new_value || '—'}`
    case 'status_change': return `status ${a.old_value || '—'} → ${a.new_value}`
    case 'stage_change': return `stage ${a.old_value || 'none'} → ${a.new_value}`
    case 'note': return 'analyst notes updated'
    case 'signal_review': return `signal ${a.new_value} (${a.field})`
    case 'deep_analysis': return `AI analysis — ${a.note || ''}`
    case 'report_export': return `report exported (${a.note || ''})`
    case 'dq_reinstated': return 'disqualification reversed'
    case 'watchlist': return a.new_value === 'True' || a.new_value === 'true' ? 'added to watchlist' : 'removed from watchlist'
    default: return `${a.activity_type}${a.note ? ` — ${a.note}` : ''}`
  }
}

/**
 * Owner / next action / stage editor with optimistic-concurrency protection,
 * plus the activity timeline. `prospect` must include row_version.
 */
export default function WorkflowPanel({ prospect, activity: initialActivity = [], onSaved, onToast }) {
  const [form, setForm] = useState({})
  const [version, setVersion] = useState(prospect?.row_version)
  const [saving, setSaving] = useState(false)
  const [conflict, setConflict] = useState(null)
  const [activity, setActivity] = useState(initialActivity)

  useEffect(() => {
    if (!prospect) return
    setForm({
      owner: prospect.owner || '',
      next_action: prospect.next_action || '',
      next_action_due: prospect.next_action_due || '',
      last_contacted_at: prospect.last_contacted_at || '',
      opportunity_stage: prospect.opportunity_stage || 'none',
      outcome_reason: prospect.outcome_reason || '',
    })
    setVersion(prospect.row_version)
    setConflict(null)
  }, [prospect])

  useEffect(() => { setActivity(initialActivity) }, [initialActivity])

  if (!prospect) return null

  const changed = Object.entries(form).filter(([k, v]) => String(v || '') !== String(prospect[k] || (k === 'opportunity_stage' ? 'none' : '')))
  const overdue = form.next_action_due && new Date(form.next_action_due) < new Date(new Date().toDateString())

  const save = async () => {
    if (!changed.length) return
    setSaving(true)
    setConflict(null)
    // The API treats "" as "clear this field" (nulls mean "not provided").
    const payload = Object.fromEntries(changed.map(([k, v]) => [k, v ?? '']))
    try {
      const r = await api.patch(`/api/prospects/${prospect.id}`, { ...payload, expected_version: version, actor: 'analyst' })
      setVersion(r.row_version)
      const act = await api.get(`/api/prospects/${prospect.id}/activity?limit=50`)
      setActivity(act.activity || [])
      onToast && onToast(true, 'Workflow saved')
      onSaved && onSaved(r)
    } catch (err) {
      if (err.isConflict) {
        setConflict(err.detail && err.detail.current_version)
        onToast && onToast(false, 'Someone else changed this prospect. Reload to see their changes before saving.')
      } else {
        onToast && onToast(false, err.message)
      }
    }
    setSaving(false)
  }

  const set = (k) => (e) => setForm(f => ({ ...f, [k]: e.target.value }))

  return (
    <div className="card p-4 mb-4" data-testid="workflow-panel">
      <div className="flex items-center justify-between mb-3">
        <span className="font-mono text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Ownership & next action</span>
        <span className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>v{version}</span>
      </div>
      <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))' }}>
        <label className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>Owner
          <input style={inputStyle} value={form.owner || ''} onChange={set('owner')} placeholder="who is driving this" />
        </label>
        <label className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>Stage
          <select style={inputStyle} value={form.opportunity_stage || 'none'} onChange={set('opportunity_stage')}>
            {STAGES.map(s => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
          </select>
        </label>
        <label className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>Next action
          <input style={inputStyle} value={form.next_action || ''} onChange={set('next_action')} placeholder="e.g. call COO re Q3 guidance" />
        </label>
        <label className="font-mono text-xs" style={{ color: overdue ? 'var(--caution)' : 'var(--text-secondary)' }}>Due{overdue ? ' (overdue)' : ''}
          <input type="date" style={inputStyle} value={form.next_action_due || ''} onChange={set('next_action_due')} />
        </label>
        <label className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>Last contacted
          <input type="date" style={inputStyle} value={form.last_contacted_at || ''} onChange={set('last_contacted_at')} />
        </label>
        {(form.opportunity_stage === 'lost' || form.opportunity_stage === 'won') && (
          <label className="font-mono text-xs" style={{ color: 'var(--text-secondary)' }}>Outcome reason
            <input style={inputStyle} value={form.outcome_reason || ''} onChange={set('outcome_reason')} placeholder="why won / lost" />
          </label>
        )}
      </div>
      <div className="flex items-center justify-between mt-3">
        <span className="font-mono text-xs" style={{ color: conflict ? 'var(--caution)' : 'var(--text-muted)' }}>
          {conflict ? `Conflict: server is at v${conflict}, you loaded v${version}.` : changed.length ? `${changed.length} unsaved change(s)` : 'No unsaved changes'}
        </span>
        <button onClick={save} disabled={saving || !changed.length} className="font-mono text-xs px-4 py-2"
          style={{ background: changed.length ? 'var(--accent)' : 'var(--border)', color: changed.length ? 'var(--on-accent)' : 'var(--text-muted)', border: 'none', cursor: changed.length ? 'pointer' : 'not-allowed' }}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>

      <div className="mt-4 pt-3" style={{ borderTop: '1px solid var(--border)' }}>
        <div className="font-mono text-xs uppercase tracking-widest mb-2" style={{ color: 'var(--text-muted)' }}>Activity ({activity.length})</div>
        {activity.length === 0 ? (
          <div className="font-mono text-xs" style={{ color: 'var(--text-muted)' }}>No activity recorded yet.</div>
        ) : (
          <ul className="text-xs" style={{ maxHeight: 220, overflowY: 'auto' }}>
            {activity.map(a => (
              <li key={a.id} className="flex gap-3 py-1" style={{ borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)' }}>
                <span className="font-mono" style={{ color: 'var(--text-muted)', minWidth: 64 }} title={a.created_at}>{relTime(a.created_at)}</span>
                <span className="font-mono" style={{ color: 'var(--text-muted)', minWidth: 56 }}>{a.actor}</span>
                <span>{describeActivity(a)}{a.note && a.activity_type !== 'deep_analysis' && a.activity_type !== 'report_export' ? ` — ${a.note}` : ''}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
