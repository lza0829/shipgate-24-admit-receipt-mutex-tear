import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Overview() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/总览').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中</div>
  const d = data.分布 || {}
  return (
    <div className="panel">
      <h2>总览</h2>
      <div className="grid">
        {Object.entries(d).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
      </div>
      <h2>在途发版（应用占用位）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>发版单号</th><th>应用</th><th>目标版本</th><th>状态</th><th>当前环境</th><th>流水线</th><th>占用</th></tr></thead>
          <tbody>
            {(data.在途 || []).map((r) => (
              <tr key={r.编号}><td>{r.发版单号}</td><td>{r.应用}</td><td>{r.目标版本}</td><td>{r.状态}</td><td>{r.当前环境 || '-'}</td><td>{r.流水线}</td><td><span className="lock-on">占用中</span></td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>排队发版（未入闸，不可晋升）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>发版单号</th><th>应用</th><th>目标版本</th><th>状态</th><th>排队时间</th><th>流水线</th></tr></thead>
          <tbody>
            {(data.排队 || []).length === 0 && <tr><td colSpan="6" className="sub">无排队单</td></tr>}
            {(data.排队 || []).map((r) => (
              <tr key={r.编号} className="row-queued"><td>{r.发版单号}</td><td>{r.应用}</td><td>{r.目标版本}</td><td><span className="badge badge-queued">{r.状态}</span></td><td>{r.排队时间 || '-'}</td><td>{r.流水线}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>各环境版本</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>应用</th><th>环境</th><th>版本</th><th>更新时间</th></tr></thead>
          <tbody>
            {(data.环境版本 || []).map((v, i) => (
              <tr key={i}><td>{v.应用}</td><td>{v.环境}</td><td>{v.版本}</td><td>{v.更新时间}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
