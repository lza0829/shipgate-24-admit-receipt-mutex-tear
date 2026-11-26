import React, { useEffect, useState } from 'react'
import { api } from '../api'

const QUEUED = '排队中'
const ACTIVE = ['已入闸', '晋升中', '已放行', '回滚中']

const surface_panel = true
export default function Releases() {
  const [rows, setRows] = useState([])
  const [apps, setApps] = useState([])
  const [appId, setAppId] = useState('')
  const [ver, setVer] = useState('')
  const [digest, setDigest] = useState('')
  const [err, setErr] = useState('')
  const load = async () => {
    setRows(await api('/发版单'))
    const a = await api('/应用')
    setApps(a)
    if (!appId && a[0]) setAppId(String(a[0].编号))
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  const create = async () => {
    setErr('')
    try {
      await api('/发版单', {
        method: 'POST',
        body: JSON.stringify({ 应用编号: Number(appId), 目标版本: ver, 制品摘要: digest }),
      })
      setVer(''); setDigest(''); load()
    } catch (e) { setErr(e.message) }
  }
  const pipeline = async (id, status) => {
    setErr('')
    try {
      await api(`/发版单/${id}/流水线`, { method: 'POST', body: JSON.stringify({ 流水线: status }) })
      load()
    } catch (e) { setErr(e.message) }
  }
  const admit = async (id) => {
    setErr('')
    try {
      await api(`/发版单/${id}/入闸`, { method: 'POST', body: '{}' })
      load()
    } catch (e) { setErr(e.message) }
  }
  const enqueue = async (id) => {
    setErr('')
    try {
      await api(`/发版单/${id}/排队`, { method: 'POST', body: '{}' })
      load()
    } catch (e) { setErr(e.message) }
  }
  const close = async (id) => {
    setErr('')
    try {
      await api(`/发版单/${id}/关闭`, { method: 'POST', body: '{}' })
      load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className="panel">
      <h2>发版单</h2>
      {err && <p className="err">{err}</p>}
      <div className="row">
        <label>应用
          <select value={appId} onChange={(e) => setAppId(e.target.value)}>
            {apps.map((a) => <option key={a.编号} value={a.编号}>{a.名称}</option>)}
          </select>
        </label>
        <label>目标版本<input value={ver} onChange={(e) => setVer(e.target.value)} placeholder="1.4.0" /></label>
        <label>制品摘要<input value={digest} onChange={(e) => setDigest(e.target.value)} placeholder="sha256:..." /></label>
        <button type="button" onClick={create}>申请入闸</button>
        <button type="button" className="secondary" onClick={() => load().catch((e) => setErr(e.message))}>刷新</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>发版单号</th><th>应用</th><th>目标版本</th><th>状态</th><th>流水线</th><th>当前环境</th><th>应用占用</th><th>操作</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.编号} className={r.状态 === QUEUED ? 'row-queued' : ''}>
                <td>{r.发版单号}</td>
                <td>{r.应用}</td>
                <td>{r.目标版本}</td>
                <td>
                  <span className={`badge badge-${r.状态 === QUEUED ? 'queued' : ACTIVE.includes(r.状态) ? 'active' : 'idle'}`}>{r.状态}</span>
                  {r.排队时间 && <div className="sub">排队于 {r.排队时间}</div>}
                </td>
                <td>{r.流水线}</td>
                <td>{r.当前环境 || '-'}</td>
                <td>{r.占用应用 ? <span className="lock-on">占用中</span> : <span className="lock-off">-</span>}</td>
                <td>
                  <button type="button" className="secondary" onClick={() => pipeline(r.编号, '绿')}>流水线绿</button>{' '}
                  <button type="button" className="danger" onClick={() => pipeline(r.编号, '红')}>流水线红</button>{' '}
                  <button
                    type="button"
                    onClick={() => admit(r.编号)}
                    disabled={r.状态 !== '待入闸'}
                    title={r.状态 === QUEUED ? '排队中，唤醒后可入闸' : ''}
                  >入闸</button>{' '}
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => enqueue(r.编号)}
                    disabled={r.状态 !== '待入闸'}
                  >排队</button>{' '}
                  <button
                    type="button"
                    className="danger"
                    onClick={() => close(r.编号)}
                    disabled={['已回滚', '已关闭'].includes(r.状态)}
                  >关闭</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
