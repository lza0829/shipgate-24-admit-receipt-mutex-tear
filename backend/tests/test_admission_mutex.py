"""同应用入闸互斥夹具（互斥臂一：同应用只许一单活跃在途）。

覆盖：
1. 双单同点：两个入闸写口并发对撞，恰好一单成功，另一单 409 且零副作用；
2. 先关后入：占用单关闭/回滚释放占用位后，后单可入闸，排队单被唤醒；
3. 半截双挂：手工制造双在途脏数据，下一次写口按写时规则收口到单活跃；
4. 排队单被误晋升：排队态在晋升/入闸写口均被硬阻断；
横切：发版单详情、应用名册占用、总览在途格、对账、审计互斥记录同刷对齐。
"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from conftest import admit, make_release, release_status


def _raw_conn():
    conn = sqlite3.connect(os.environ["SHIPGATE_DB"])
    conn.row_factory = sqlite3.Row
    return conn


def _lock_rows(app_id):
    with _raw_conn() as conn:
        return conn.execute("SELECT * FROM app_locks WHERE app_id=?", (app_id,)).fetchall()


def _audit_actions(ref_id):
    with _raw_conn() as conn:
        return [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE ref_id=? ORDER BY id", (ref_id,)
            ).fetchall()
        ]


def _audit_mutex_notes():
    with _raw_conn() as conn:
        return [
            (r["ref_id"], r["action"], r["note"])
            for r in conn.execute(
                "SELECT ref_id, action, note FROM audit_events WHERE kind='互斥' ORDER BY id"
            ).fetchall()
        ]


# ---------- 夹具一：双单同点 ----------

def test_concurrent_double_admit_exactly_one_wins(client, app_id):
    for round_no in range(5):
        r1 = make_release(client, app_id, f"9.1.{round_no}")
        r2 = make_release(client, app_id, f"9.2.{round_no}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda rid: admit(client, rid), (r1, r2)))

        statuses = [r.status_code for r in results]
        assert sorted(statuses) == [200, 409], f"第{round_no}轮回执: {statuses}"
        winner = next(r for r in results if r.status_code == 200).json()
        loser_resp = next(r for r in results if r.status_code == 409).json()["detail"]

        # 双入闸回执带占用证据
        assert loser_resp["占用发版单"] == winner["发版单号"]
        assert loser_resp["占用位"] == f"app:{app_id}"

        # 恰好一单已入闸，一单回到入闸前（待入闸），不占在途
        assert release_status(client, r1) == "已入闸" or release_status(client, r2) == "已入闸"
        loser_id = r1 if winner["编号"] == r2 else r2
        assert release_status(client, loser_id) == "待入闸"

        # 应用占用位恰好一行，挂在赢家身上
        locks = _lock_rows(app_id)
        assert len(locks) == 1
        assert locks[0]["release_id"] == winner["编号"]

        # 输家流水线挂接未被改写（仍是绿，未产生入闸类副作用），审计有互斥拒绝
        loser = client.get(f"/api/发版单/{loser_id}").json()
        assert loser["流水线"] == "绿"
        assert loser["占用应用"] in (0, False)
        assert "入闸互斥拒绝" in _audit_actions(loser_id)

        # 总览在途格同应用只计一笔，分布里没有重复在途
        ov = client.get("/api/总览").json()
        app_active = [r for r in ov["在途"] if r["应用编号"] == app_id]
        assert len(app_active) == 1 and app_active[0]["编号"] == winner["编号"]

        # 收口本轮，释放占用位进入下一轮
        client.post(f"/api/发版单/{winner['编号']}/关闭")

    # 五轮对撞后占用位清零，互斥审计每轮一条拒绝
    assert _lock_rows(app_id) == []
    rejects = [a for a in _audit_mutex_notes() if a[1] == "入闸互斥拒绝"]
    assert len(rejects) == 5


def test_failed_admit_rolls_back_all_side_effects(client, app_id):
    """失败撤到入闸前：输家无 current_env、无 admitted_at、无占用、无入闸审计。"""
    r1 = make_release(client, app_id, "8.1.0")
    r2 = make_release(client, app_id, "8.2.0")
    assert admit(client, r1).status_code == 200
    resp = admit(client, r2)
    assert resp.status_code == 409

    loser = client.get(f"/api/发版单/{r2}").json()
    assert loser["状态"] == "待入闸"
    assert loser["当前环境编号"] is None
    assert loser["入闸时间"] is None
    assert loser["占用应用"] in (0, False)
    with _raw_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM app_locks WHERE release_id=?", (r2,)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE ref_id=? AND action='入闸'", (r2,)
        ).fetchone()["c"] == 0


# ---------- 夹具二：先关后入 ----------

def test_close_then_admit_frees_lock(client, app_id):
    r1 = make_release(client, app_id, "7.1.0")
    r2 = make_release(client, app_id, "7.2.0")
    assert admit(client, r1).status_code == 200
    assert admit(client, r2).status_code == 409

    closed = client.post(f"/api/发版单/{r1}/关闭")
    assert closed.status_code == 200 and closed.json()["状态"] == "已关闭"
    assert _lock_rows(app_id) == []

    # 应用名册占用位已空
    roster = [a for a in client.get("/api/应用").json() if a["编号"] == app_id][0]
    assert roster["占用发版单"] is None

    # 后单现在可以入闸成功
    ok = admit(client, r2)
    assert ok.status_code == 200 and ok.json()["状态"] == "已入闸"
    assert _lock_rows(app_id)[0]["release_id"] == r2


def test_rollback_releases_lock_and_wakes_queue(client, app_id):
    r1 = make_release(client, app_id, "6.1.0")
    r2 = make_release(client, app_id, "6.2.0")
    assert admit(client, r1).status_code == 200

    # r2 进入可核排队态
    q = client.post(f"/api/发版单/{r2}/排队")
    assert q.status_code == 200 and q.json()["状态"] == "排队中"
    assert q.json()["排队时间"]
    ov = client.get("/api/总览").json()
    assert [x["编号"] for x in ov["排队"]] == [r2]
    assert all(x["编号"] != r2 for x in ov["在途"])  # 排队不计在途

    # r1 晋升到预再回滚（走完整终态写口）
    assert client.post(f"/api/发版单/{r1}/晋升", json={"目标环境": "预"}).status_code == 200
    rb = client.post(f"/api/发版单/{r1}/回滚", json={"目标版本": "6.0.0"})
    assert rb.status_code == 200 and rb.json()["状态"] == "已回滚"

    # 唤醒：排队单回到待入闸，且未被自动入闸
    assert release_status(client, r2) == "待入闸"
    assert "排队唤醒" in _audit_actions(r2)
    assert admit(client, r2).status_code == 200


# ---------- 夹具三：半截双挂收口 ----------

def test_half_double_attach_converges_to_single_active(client, app_id):
    r1 = make_release(client, app_id, "5.1.0")
    r2 = make_release(client, app_id, "5.2.0")
    assert admit(client, r1).status_code == 200

    # 注入半截双挂：r2 被直接写成已入闸（绕过互斥的脏数据），但没有占用位
    with _raw_conn() as conn:
        conn.execute(
            "UPDATE releases SET status='已入闸', current_env_id=(SELECT id FROM environments WHERE code='测') WHERE id=?",
            (r2,),
        )
        conn.commit()

    # 对账先能发现互斥违例
    rec = client.get("/api/对账").json()
    my_row = [v for v in rec["互斥违例应用"] if v["应用编号"] == app_id]
    assert my_row and my_row[0]["在途单数"] == 2

    # 再点入闸被状态规则挡住（已不是待入闸），互斥重放写口按写时规则收口
    resp = admit(client, r2)
    assert resp.status_code in (400, 409)
    replay = client.post(f"/api/应用/{app_id}/互斥重放")
    assert replay.status_code == 200 and replay.json()["占用发版单编号"] == r1
    locks = _lock_rows(app_id)
    assert len(locks) == 1 and locks[0]["release_id"] == r1
    assert release_status(client, r1) == "已入闸"
    assert release_status(client, r2) == "已关闭"  # 收口，不可再点晋升
    assert any(a[1] == "占用收口" and a[0] == r2 for a in _audit_mutex_notes())

    # 收口后对账恢复一致（按应用重放），总览只计一笔
    rec2 = client.get("/api/对账").json()
    my_rows = [v for v in rec2["互斥违例应用"] if v["应用编号"] == app_id]
    assert not my_rows
    lock_rows = [v for v in rec2["占用位明细"] if v["应用编号"] == app_id] if "占用位明细" in rec2 else []
    assert all(v["在途单数"] == v["占用位数"] for v in lock_rows)
    ov = client.get("/api/总览").json()
    assert [x["编号"] for x in ov["在途"] if x["应用编号"] == app_id] == [r1]


def test_half_double_attach_converges_on_promote(client, app_id):
    """半截双挂后从晋升写口收口：非持位单晋升被 409 阻断。"""
    r1 = make_release(client, app_id, "4.1.0")
    r2 = make_release(client, app_id, "4.2.0")
    assert admit(client, r1).status_code == 200
    with _raw_conn() as conn:
        conn.execute("UPDATE releases SET status='已入闸' WHERE id=?", (r2,))
        conn.commit()

    resp = client.post(f"/api/发版单/{r2}/晋升", json={"目标环境": "预"})
    assert resp.status_code == 409
    assert len(_lock_rows(app_id)) == 1
    # r1 仍可正常晋升
    ok = client.post(f"/api/发版单/{r1}/晋升", json={"目标环境": "预"})
    assert ok.status_code == 200


# ---------- 夹具四：排队单被误晋升 ----------

def test_queued_release_cannot_promote_or_readmit(client, app_id):
    r1 = make_release(client, app_id, "3.1.0")
    r2 = make_release(client, app_id, "3.2.0")
    assert admit(client, r1).status_code == 200
    assert client.post(f"/api/发版单/{r2}/排队").status_code == 200

    # 误点晋升：硬阻断
    p = client.post(f"/api/发版单/{r2}/晋升", json={"目标环境": "预"})
    assert p.status_code == 409 and "排队中" in p.json()["detail"]
    # 误点入闸：硬阻断，状态仍是排队中
    a = admit(client, r2)
    assert a.status_code == 409
    assert release_status(client, r2) == "排队中"
    # 晋升页只列已入闸/晋升中，排队单不出现
    rows = client.get("/api/发版单").json()
    queued = [x for x in rows if x["编号"] == r2][0]
    assert queued["状态"] == "排队中" and not queued["占用应用"]


# ---------- 横切：两个入口与总览、名册、审计同刷 ----------

def test_all_faces_agree_on_single_active(client, app_id):
    r1 = make_release(client, app_id, "2.1.0")
    r2 = make_release(client, app_id, "2.2.0")
    assert admit(client, r1).status_code == 200
    assert admit(client, r2).status_code == 409

    # 发版单详情
    d1 = client.get(f"/api/发版单/{r1}").json()
    d2 = client.get(f"/api/发版单/{r2}").json()
    assert d1["状态"] == "已入闸" and d1["占用应用"]
    assert d2["状态"] == "待入闸" and not d2["占用应用"]

    # 应用名册占用
    roster = [a for a in client.get("/api/应用").json() if a["编号"] == app_id][0]
    assert roster["占用发版单编号"] == r1
    assert roster["占用发版单"] == d1["发版单号"]

    # 总览在途格 + 分布
    ov = client.get("/api/总览").json()
    assert ov["分布"]["已入闸"] >= 1
    assert sum(1 for x in ov["在途"] if x["应用编号"] == app_id) == 1

    # 对账：逐应用占用位与在途态对齐
    rec = client.get("/api/对账").json()
    my_lock = [v for v in rec["占用位明细"] if v["应用编号"] == app_id][0]
    assert my_lock["在途单数"] == 1 and my_lock["占用位数"] == 1
    assert [v for v in rec["互斥违例应用"] if v["应用编号"] == app_id] == []

    # 审计互斥记录可查，且关联到被拒单号
    notes = [a for a in _audit_mutex_notes() if a[0] == r2]
    assert notes and notes[0][1] == "入闸互斥拒绝"


def test_different_apps_do_not_block_each_other(client, app_id):
    """夹具边界：互斥只管同应用，跨应用各自入闸（跨环晋升另立，不在本夹具）。"""
    other = client.post(
        "/api/应用", json={"名称": f"互斥夹具另一应用-{os.urandom(4).hex()}", "状态": "启用"}
    ).json()["编号"]
    r1 = make_release(client, app_id, "1.1.0")
    r2 = make_release(client, other, "1.1.0")
    assert admit(client, r1).status_code == 200
    assert admit(client, r2).status_code == 200
    assert len(_lock_rows(app_id)) == 1
    assert len(_lock_rows(other)) == 1
