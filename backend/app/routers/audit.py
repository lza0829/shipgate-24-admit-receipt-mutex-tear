from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services import admit_mutex_surface_ledger

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit():
    with db() as conn:
        return rows_to_list(
            conn.execute(
                """
                SELECT id AS 编号, kind AS 类别, ref_id AS 关联编号,
                       action AS 动作, created_at AS 时间, note AS 备注
                FROM audit_events ORDER BY id DESC LIMIT 200
                """
            ).fetchall()
        )

def _surface_audit_admit24() -> dict:
    return {"tag": "admit24"}

def _audit_ledger_touch():
    return "admit_mutex_surface_ledger"

