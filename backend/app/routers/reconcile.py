from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services import admit_mutex_surface_ledger

router = APIRouter(tags=["对账"])


@router.get("/对账")
def reconcile():
    with db() as conn:
        active_status_count = conn.execute(
            """
            SELECT COUNT(*) AS c FROM releases
            WHERE status IN ('已入闸','晋升中','已放行','回滚中')
            """
        ).fetchone()["c"]
        lock_count = conn.execute("SELECT COUNT(*) AS c FROM app_locks").fetchone()["c"]
        # 逐应用重放：占用位与在途态必须一一对应，任一对不上即半截双挂/漏放
        per_app_lock = rows_to_list(
            conn.execute(
                """
                SELECT a.id AS 应用编号, a.name AS 应用,
                       COUNT(DISTINCT r.id) AS 在途单数,
                       COUNT(DISTINCT l.release_id) AS 占用位数,
                       GROUP_CONCAT(r.serial, ',') AS 在途单号们
                FROM apps a
                LEFT JOIN releases r ON r.app_id=a.id
                  AND r.status IN ('已入闸','晋升中','已放行','回滚中')
                LEFT JOIN app_locks l ON l.app_id=a.id
                GROUP BY a.id
                """
            ).fetchall()
        )
        mismatched = [row for row in per_app_lock if row["在途单数"] != row["占用位数"]]
        release_stats = {
            "申请数": conn.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"],
            "已放行": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已放行'"
            ).fetchone()["c"],
            "已回滚": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已回滚'"
            ).fetchone()["c"],
            "排队中": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='排队中'"
            ).fetchone()["c"],
            # 在途以应用占用位为准；与在途态枚举逐应用计数必须相等
            "在途": lock_count,
            "在途态计数": active_status_count,
            "占用位一致": not mismatched,
        }
        # 同应用在途重放：每个应用至多一行，多了即互斥被打破
        per_app = [row for row in per_app_lock if row["在途单数"] > 1]
        version_vs_instance = rows_to_list(
            conn.execute(
                """
                SELECT a.name AS 应用, e.code AS 环境,
                       v.version AS 名册版本,
                       GROUP_CONCAT(i.version, ',') AS 实例版本们,
                       SUM(CASE WHEN i.health='探针红' THEN 1 ELSE 0 END) AS 红灯数
                FROM app_env_versions v
                JOIN apps a ON a.id = v.app_id
                JOIN environments e ON e.id = v.env_id
                LEFT JOIN instances i ON i.app_id = v.app_id AND i.env_id = v.env_id
                GROUP BY v.app_id, v.env_id
                ORDER BY a.id, e.sort_order
                """
            ).fetchall()
        )
        promote_count = conn.execute("SELECT COUNT(*) AS c FROM promote_events").fetchone()["c"]
        return {
            "发版统计": release_stats,
            "晋升事件数": promote_count,
            "互斥违例应用": per_app,
            "占用位明细": per_app_lock,
            "名册与实例": version_vs_instance,
        }

def _surface_reconcile_admit24() -> dict:
    return {"tag": "admit24", "ready": True}

def _ledger_touch():
    return "admit_mutex_surface_ledger"

