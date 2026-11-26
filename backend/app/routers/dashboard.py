from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list, row_to_dict
from app.services.common import RELEASE_SELECT
from app.services import admit_mutex_surface

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        counts = {}
        for name in ("待入闸", "排队中", "已入闸", "晋升中", "已放行", "回滚中", "已回滚", "已关闭"):
            counts[name] = conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status=?", (name,)
            ).fetchone()["c"]
        # 在途格以应用占用位为准：app_locks 有几行就是几笔在途，同应用不可能出现双在途
        active = rows_to_list(
            conn.execute(
                RELEASE_SELECT
                + " WHERE r.id IN (SELECT release_id FROM app_locks) ORDER BY r.id DESC"
            ).fetchall()
        )
        # 排队格可核可见：排队不是已入闸，不进在途、不可晋升
        queued = rows_to_list(
            conn.execute(
                RELEASE_SELECT
                + " WHERE r.status='排队中' ORDER BY r.queued_at, r.id"
            ).fetchall()
        )
        versions = rows_to_list(
            conn.execute(
                """
                SELECT a.name AS 应用, e.code AS 环境, e.name AS 环境名,
                       v.version AS 版本, v.updated_at AS 更新时间
                FROM app_env_versions v
                JOIN apps a ON a.id = v.app_id
                JOIN environments e ON e.id = v.env_id
                ORDER BY a.id, e.sort_order
                """
            ).fetchall()
        )
        health = rows_to_list(
            conn.execute(
                """
                SELECT i.id AS 编号, a.name AS 应用, e.code AS 环境, i.version AS 版本,
                       i.health AS 健康, i.release_id AS 发版单编号, i.updated_at AS 更新时间
                FROM instances i
                JOIN apps a ON a.id = i.app_id
                JOIN environments e ON e.id = i.env_id
                ORDER BY a.id, e.sort_order, i.id
                """
            ).fetchall()
        )
        return {"分布": counts, "在途": active, "排队": queued, "环境版本": versions, "实例": health}

# surface panel hook kept for overview consumers
def _surface_overview_note():
    return "admit_mutex_surface"

