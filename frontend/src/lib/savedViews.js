const KEY = 'delta-saved-views'

export function loadViews() {
  try { return JSON.parse(localStorage.getItem(KEY) || '[]') } catch { return [] }
}
export function storeViews(views) {
  localStorage.setItem(KEY, JSON.stringify(views))
}
