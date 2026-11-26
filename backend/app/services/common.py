from __future__ import annotations
from datetime import datetime
from fastapi import HTTPException

# 入闸互斥臂一：同应用只许一单活跃在途。
# 占用位 app_locks 是唯一事实源；在途态枚举（已入闸/晋升中/已放行/回滚中）必须与其逐行对齐。
ACTIVE_STATUS_SQL = "('已入闸','晋升中','已放行','回滚中')"


class MutexRejected(Exception):
    """入闸互斥拒绝：携带占用单号用于双入闸回执（事务先落审计再抛）。"""

    def __init__(self, reason: str, holder_serial: str | None = None, holder_id: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.holder_serial = holder_serial
        self.holder_id = holder_id


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def audit(conn, kind: str, ref_id: int | None, action: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_events(kind, ref_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
        (kind, ref_id, action, now_str(), note),
    )


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def next_serial(conn) -> str:
    day = datetime.now().strftime("%Y%m%d")
    prefix = f"发{day}"
    row = conn.execute(
        "SELECT serial FROM releases WHERE serial LIKE ? ORDER BY serial DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    n = 1
    if row and len(row["serial"]) >= 4:
        try:
            n = int(row["serial"][-4:]) + 1
        except ValueError:
            n = 1
    return f"{prefix}{n:04d}"


RELEASE_SELECT = """
SELECT r.id AS 编号,
       r.serial AS 发版单号,
       r.app_id AS 应用编号,
       a.name AS 应用,
       a.owner AS 负责人,
       a.status AS 应用状态,
       r.target_version AS 目标版本,
       r.artifact_digest AS 制品摘要,
       r.status AS 状态,
       r.pipeline_status AS 流水线,
       r.current_env_id AS 当前环境编号,
       e.code AS 当前环境,
       e.name AS 当前环境名,
       r.canary_ratio AS 金丝雀比例,
       r.note AS 备注,
       r.created_at AS 申请时间,
       r.admitted_at AS 入闸时间,
       r.queued_at AS 排队时间,
       r.released_at AS 放行时间,
       r.rolled_back_at AS 回滚时间,
       l.release_id IS NOT NULL AS 占用应用,
       l.since AS 占用起始
FROM releases r
JOIN apps a ON a.id = r.app_id
LEFT JOIN environments e ON e.id = r.current_env_id
LEFT JOIN app_locks l ON l.release_id = r.id
"""


def get_release(conn, release_id: int):
    row = conn.execute(RELEASE_SELECT + " WHERE r.id=?", (release_id,)).fetchone()
    if not row:
        raise HTTPException(404, "发版单不存在")
    return row


def env_by_code(conn, code: str):
    row = conn.execute("SELECT * FROM environments WHERE code=?", (code,)).fetchone()
    if not row:
        raise HTTPException(400, f"环境不存在：{code}")
    return row


def env_by_id(conn, env_id: int):
    row = conn.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
    if not row:
        raise HTTPException(400, "环境不存在")
    return row


def ordered_envs(conn):
    return conn.execute("SELECT * FROM environments ORDER BY sort_order").fetchall()


def active_release_window(conn, env_id: int):
    return conn.execute(
        """
        SELECT * FROM release_windows
        WHERE env_id=? AND status='窗开'
        ORDER BY id DESC LIMIT 1
        """,
        (env_id,),
    ).fetchone()


def active_rollback_window(conn, env_id: int):
    return conn.execute(
        """
        SELECT * FROM rollback_windows
        WHERE env_id=? AND status='窗开'
        ORDER BY id DESC LIMIT 1
        """,
        (env_id,),
    ).fetchone()


def set_app_env_version(conn, app_id: int, env_id: int, version: str) -> None:
    conn.execute(
        """
        INSERT INTO app_env_versions(app_id, env_id, version, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(app_id, env_id) DO UPDATE SET version=excluded.version, updated_at=excluded.updated_at
        """,
        (app_id, env_id, version, now_str()),
    )


def upsert_instance(conn, app_id: int, env_id: int, version: str, health: str, release_id: int | None) -> None:
    row = conn.execute(
        "SELECT id FROM instances WHERE app_id=? AND env_id=? AND version=?",
        (app_id, env_id, version),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE instances SET health=?, release_id=?, updated_at=? WHERE id=?",
            (health, release_id, now_str(), row["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO instances(app_id, env_id, version, health, release_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (app_id, env_id, version, health, release_id, now_str()),
        )


def assert_app_enabled(conn, app_id: int):
    row = conn.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
    if not row:
        raise HTTPException(404, "应用不存在")
    if row["status"] != "启用":
        raise HTTPException(400, "应用已停用，禁止入闸")
    return row


def _active_releases(conn, app_id: int):
    return conn.execute(
        f"SELECT id, serial, status FROM releases WHERE app_id=? AND status IN {ACTIVE_STATUS_SQL} ORDER BY id",
        (app_id,),
    ).fetchall()


def reconcile_app_lock(conn, app_id: int):
    """按写时规则重放占用位：在途态枚举与 app_locks 逐行对齐，半截双挂在此收口到单活跃。

    必须在 BEGIN IMMEDIATE 单写者事务内调用。返回占用行（无占用返回 None）。
    """
    actives = _active_releases(conn, app_id)
    lock = conn.execute("SELECT * FROM app_locks WHERE app_id=?", (app_id,)).fetchone()
    if not actives:
        if lock:
            conn.execute("DELETE FROM app_locks WHERE app_id=?", (app_id,))
        return None
    # 同应用出现多笔在途（半截双挂脏数据）：最早入闸者保留占用，其余按终态收口，禁止双挂
    holder = actives[0]
    extras = actives[1:]
    for extra in extras:
        conn.execute(
            "UPDATE releases SET status='已关闭', note=note||? WHERE id=?",
            (f"｜互斥收口：{holder['serial']} 持有占用，本单关闭", extra["id"]),
        )
        audit(conn, "互斥", extra["id"], "占用收口", f"收口到 {holder['serial']}")
    if not lock or lock["release_id"] != holder["id"]:
        conn.execute("DELETE FROM app_locks WHERE app_id=?", (app_id,))
        since = conn.execute("SELECT admitted_at FROM releases WHERE id=?", (holder["id"],)).fetchone()["admitted_at"] or now_str()
        conn.execute(
            "INSERT INTO app_locks(app_id, release_id, since) VALUES (?, ?, ?)",
            (app_id, holder["id"], since),
        )
    return conn.execute("SELECT * FROM app_locks WHERE app_id=?", (app_id,)).fetchone()


def acquire_app_lock(conn, app_id: int, release_id: int) -> None:
    """入闸占用：与在途态同成败。占用位唯一约束兜底，任何双入闸都不可能同时成功。"""
    holder = reconcile_app_lock(conn, app_id)
    if holder is not None:
        row = conn.execute("SELECT serial FROM releases WHERE id=?", (holder["release_id"],)).fetchone()
        raise MutexRejected(
            "同应用已有在途发版单，入闸互斥拒绝",
            holder_serial=row["serial"] if row else None,
            holder_id=holder["release_id"],
        )
    now = now_str()
    conn.execute(
        "INSERT INTO app_locks(app_id, release_id, since) VALUES (?, ?, ?)",
        (app_id, release_id, now),
    )


def release_app_lock(conn, app_id: int) -> None:
    """终态释放占用位，并唤醒本应用排队单：排队中 -> 待入闸（可核、不自动入闸、不可晋升）。"""
    lock = conn.execute("SELECT * FROM app_locks WHERE app_id=?", (app_id,)).fetchone()
    if lock:
        conn.execute("UPDATE app_locks SET until=? WHERE app_id=?", (now_str(), app_id))
        conn.execute("DELETE FROM app_locks WHERE app_id=?", (app_id,))
    queued = conn.execute(
        "SELECT id, serial FROM releases WHERE app_id=? AND status='排队中' ORDER BY queued_at, id",
        (app_id,),
    ).fetchall()
    for q in queued:
        conn.execute(
            "UPDATE releases SET status='待入闸', queued_at=NULL WHERE id=?",
            (q["id"],),
        )
        audit(conn, "互斥", q["id"], "排队唤醒", "占用位已释放，可重新申请入闸")


def assert_not_queued(conn, release_id: int) -> None:
    """排队单被误点晋升/入闸时按臂一硬阻断：排队不是已入闸，不可晋升。"""
    r = conn.execute("SELECT status FROM releases WHERE id=?", (release_id,)).fetchone()
    if r and r["status"] == "排队中":
        raise HTTPException(409, "排队中发版单未入闸，禁止晋升；待占用释放唤醒后重新入闸")

def _surface_tag_admit24() -> str:
    return "admit24"


def surface_merge_counts(base: dict, overlay: dict) -> dict:
    out = dict(base or {})
    for k, v in (overlay or {}).items():
        if isinstance(v, (int, float)):
            out[k] = v
    return out

def _surface_merge_admit24(left: dict | None, right: dict | None) -> dict:
    out = dict(left or {})
    for k, v in (right or {}).items():
        if isinstance(v, (int, float)):
            out[k] = max(int(out.get(k) or 0), int(v))
        elif k not in out:
            out[k] = v
    out.setdefault("panel", "admit24")
    return out


def surface_project_admit24(rows: list | None) -> list:
    items = list(rows or [])
    for row in items:
        if isinstance(row, dict):
            row.setdefault("projected", True)
            row.setdefault("panel", "admit24")
    return items

