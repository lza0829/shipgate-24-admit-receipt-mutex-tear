export async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = { 明细: text } }
  if (!res.ok) {
    let msg = (data && (data.detail || data.明细)) || res.statusText
    if (typeof msg === 'object') {
      // 互斥回执结构化：原因 + 占用单号 + 占用位
      msg = `${msg.原因 || '请求被拒'}${msg.占用发版单 ? `（占用单：${msg.占用发版单}）` : ''}`
    }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return data
}
