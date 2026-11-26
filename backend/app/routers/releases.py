from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, rows_to_list, row_to_dict, write_tx
from app.services.common import (
    RELEASE_SELECT,
    MutexRejected,
    acquire_app_lock,
    assert_app_enabled,
    assert_not_queued,
    audit,
    get_release,
    get_setting,
    next_serial,
    now_str,
    reconcile_app_lock,
    release_app_lock,
)
from app.services import admit_mutex_surface

router = APIRouter(tags=["发版单"])


class ReleaseIn(BaseModel):
    应用编号: int
    目标版本: str = Field(min_length=1)
    制品摘要: str = ""
    金丝雀比例: int | None = None
    备注: str = ""


class PipelineIn(BaseModel):
    流水线: str


@router.get("/发版单")
def list_releases():
    with db() as conn:
        return rows_to_list(conn.execute(RELEASE_SELECT + " ORDER BY r.id DESC").fetchall())


@router.get("/发版单/{release_id}")
def one_release(release_id: int):
    with db() as conn:
        return row_to_dict(get_release(conn, release_id))


@router.post("/发版单")
def create_release(body: ReleaseIn):
    with db() as conn:
        assert_app_enabled(conn, body.应用编号)
        ratio = body.金丝雀比例
        if ratio is None:
            ratio = int(get_setting(conn, "金丝雀默认比例", "10") or "10")
        if ratio < 0 or ratio > 100:
            raise HTTPException(400, "金丝雀比例须在 0 到 100")
        serial = next_serial(conn)
        now = now_str()
        cur = conn.execute(
            """
            INSERT INTO releases(
              serial, app_id, target_version, artifact_digest, status, pipeline_status,
              canary_ratio, note, created_at
            ) VALUES (?, ?, ?, ?, '待入闸', '未回执', ?, ?, ?)
            """,
            (serial, body.应用编号, body.目标版本, body.制品摘要, ratio, body.备注, now),
        )
        rid = cur.lastrowid
        audit(conn, "发版单", rid, "申请发版", serial)
        return row_to_dict(get_release(conn, rid))


@router.post("/发版单/{release_id}/入闸")
def admit(release_id: int):
    # 互斥臂一：整笔入闸在单写者事务内完成，校验、占用位、在途态、审计同成败。
    # 拒绝时先把互斥审计与回执落库再提交，失败撤回到入闸前（不占在途、不挂流水线、不动回执）。
    with write_tx() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        if r["status"] == "排队中":
            assert_not_queued(conn, release_id)
        if r["status"] != "待入闸":
            raise HTTPException(400, "仅待入闸可入闸")
        assert_app_enabled(conn, r["app_id"])
        must_green = get_setting(conn, "入闸须流水线绿", "是") == "是"
        if must_green and r["pipeline_status"] != "绿":
            raise HTTPException(400, "流水线未绿，禁止入闸")
        if not r["artifact_digest"]:
            raise HTTPException(400, "缺少制品摘要，禁止入闸")
        test = conn.execute("SELECT id FROM environments WHERE code='测'").fetchone()
        now = now_str()
        try:
            # 占用位是事实源：同应用已有在途时整笔失败，下面的在途态写入不会执行
            acquire_app_lock(conn, r["app_id"], release_id)
        except MutexRejected as exc:
            audit(
                conn,
                "互斥",
                release_id,
                "入闸互斥拒绝",
                f"{r['serial']} 被占用单 {exc.holder_serial or exc.holder_id} 阻断",
            )
            conn.execute("COMMIT")
            raise HTTPException(
                409,
                {
                    "原因": exc.reason,
                    "本单": r["serial"],
                    "占用发版单": exc.holder_serial,
                    "占用单编号": exc.holder_id,
                    "占用位": f"app:{r['app_id']}",
                },
            )
        # 入闸与在途占用同成败：占用位已拿到后才写在途态与流水线挂接
        conn.execute(
            """
            UPDATE releases SET status='已入闸', admitted_at=?, queued_at=NULL, current_env_id=? WHERE id=?
            """,
            (now, test["id"], release_id),
        )
        audit(conn, "发版单", release_id, "入闸", r["serial"])
        result = row_to_dict(get_release(conn, release_id))
    return result


@router.post("/应用/{app_id}/互斥重放")
def replay_mutex(app_id: int):
    """按写时规则重放某应用的占用位：半截双挂在此收口到单活跃，审计留痕。"""
    with write_tx() as conn:
        if not conn.execute("SELECT id FROM apps WHERE id=?", (app_id,)).fetchone():
            raise HTTPException(404, "应用不存在")
        holder = reconcile_app_lock(conn, app_id)
        payload = {
            "应用编号": app_id,
            "占用发版单编号": holder["release_id"] if holder else None,
        }
    return payload


@router.post("/发版单/{release_id}/排队")
def enqueue(release_id: int):
    """臂一配套的可核排队态：仅待入闸单、且同应用已有在途占用时可入队。

    排队不是已入闸：不占在途、不挂流水线、不可晋升；占用释放时由终态写口唤醒回待入闸。
    """
    with write_tx() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        if r["status"] != "待入闸":
            raise HTTPException(400, "仅待入闸发版单可入排队")
        holder = conn.execute("SELECT * FROM app_locks WHERE app_id=?", (r["app_id"],)).fetchone()
        if not holder:
            raise HTTPException(409, "同应用无在途占用，可直接入闸，无需排队")
        conn.execute(
            "UPDATE releases SET status='排队中', queued_at=? WHERE id=?",
            (now_str(), release_id),
        )
        held = conn.execute("SELECT serial FROM releases WHERE id=?", (holder["release_id"],)).fetchone()
        audit(
            conn,
            "互斥",
            release_id,
            "入排队",
            f"等待 {held['serial'] if held else holder['release_id']} 释放占用",
        )
        result = row_to_dict(get_release(conn, release_id))
    return result


@router.post("/发版单/{release_id}/关闭")
def close_release(release_id: int):
    """关闭发版单：在途单释放应用占用位并唤醒排队单；排队/待入闸单直接终态。"""
    with write_tx() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        if r["status"] in ("已回滚", "已关闭"):
            raise HTTPException(400, "终态发版单不可关闭")
        was_active = r["status"] in ("已入闸", "晋升中", "已放行", "回滚中")
        conn.execute(
            "UPDATE releases SET status='已关闭', queued_at=NULL WHERE id=?",
            (release_id,),
        )
        if was_active:
            release_app_lock(conn, r["app_id"])
        audit(conn, "发版单", release_id, "关闭发版单", r["serial"])
        result = row_to_dict(get_release(conn, release_id))
    return result


@router.post("/发版单/{release_id}/流水线")
def set_pipeline(release_id: int, body: PipelineIn):
    if body.流水线 not in ("未回执", "绿", "红"):
        raise HTTPException(400, "流水线状态非法")
    with db() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        conn.execute(
            "UPDATE releases SET pipeline_status=? WHERE id=?",
            (body.流水线, release_id),
        )
        audit(conn, "发版单", release_id, "流水线回执", body.流水线)
        _out = row_to_dict(get_release(conn, release_id))
        return admit_mutex_surface.after_action(conn, _out)
