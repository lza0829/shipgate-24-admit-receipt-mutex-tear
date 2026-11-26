import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Reconcile() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/对账').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中</div>
  const s = data.发版统计 || {}
  return (
    <div className="panel">
      <h2>发布对账</h2>
      <div className="grid">
        {Object.entries(s).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
        <div className="stat"><div className="n">{data.晋升事件数}</div><div className="l">晋升事件数</div></div>
      </div>
      <h2>互斥占用重放</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>应用</th><th>在途单数</th><th>占用位数</th><th>在途单号们</th><th>判定</th></tr></thead>
          <tbody>
            {(data.占用位明细 || []).map((r) => {
              const bad = r.在途单数 !== r.占用位数 || r.在途单数 > 1
              return (
                <tr key={r.应用编号} className={bad ? 'row-bad' : ''}>
                  <td>{r.应用}</td><td>{r.在途单数}</td><td>{r.占用位数}</td>
                  <td>{r.在途单号们 || '-'}</td>
                  <td>{bad ? <span className="badge badge-bad">互斥违例</span> : <span className="badge badge-idle">一致</span>}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <h2>名册与实例</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>应用</th><th>环境</th><th>名册版本</th><th>实例版本们</th><th>红灯数</th></tr></thead>
          <tbody>
            {(data.名册与实例 || []).map((r, i) => (
              <tr key={i}><td>{r.应用}</td><td>{r.环境}</td><td>{r.名册版本}</td><td>{r.实例版本们 || '-'}</td><td>{r.红灯数}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
