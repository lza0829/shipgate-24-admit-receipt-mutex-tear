from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  owner TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '启用' CHECK(status IN ('启用','停用'))
);
CREATE TABLE IF NOT EXISTS environments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS app_env_versions (
  app_id INTEGER NOT NULL,
  env_id INTEGER NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  updated_at TEXT,
  PRIMARY KEY(app_id, env_id),
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL UNIQUE,
  app_id INTEGER NOT NULL,
  target_version TEXT NOT NULL,
  artifact_digest TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '待入闸'
    CHECK(status IN ('待入闸','排队中','已入闸','晋升中','已放行','回滚中','已回滚','已关闭')),
  pipeline_status TEXT NOT NULL DEFAULT '未回执'
    CHECK(pipeline_status IN ('未回执','绿','红')),
  current_env_id INTEGER,
  canary_ratio INTEGER NOT NULL DEFAULT 10,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  admitted_at TEXT,
  queued_at TEXT,
  released_at TEXT,
  rolled_back_at TEXT,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(current_env_id) REFERENCES environments(id)
);
-- 应用占用位：同应用至多一行，release_id 唯一保证一笔在途只能挂一个应用
CREATE TABLE IF NOT EXISTS app_locks (
  app_id INTEGER PRIMARY KEY,
  release_id INTEGER NOT NULL UNIQUE,
  since TEXT NOT NULL,
  until TEXT,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(release_id) REFERENCES releases(id)
);
CREATE TABLE IF NOT EXISTS promote_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  release_id INTEGER NOT NULL,
  from_env_id INTEGER,
  to_env_id INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('放行','拒绝')),
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(release_id) REFERENCES releases(id),
  FOREIGN KEY(from_env_id) REFERENCES environments(id),
  FOREIGN KEY(to_env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS release_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env_id INTEGER NOT NULL,
  open_at TEXT NOT NULL,
  close_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '窗开' CHECK(status IN ('窗开','窗关')),
  FOREIGN KEY(env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS rollback_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env_id INTEGER NOT NULL,
  open_at TEXT NOT NULL,
  close_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '窗开' CHECK(status IN ('窗开','窗关')),
  FOREIGN KEY(env_id) REFERENCES environments(id)
);
CREATE TABLE IF NOT EXISTS instances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id INTEGER NOT NULL,
  env_id INTEGER NOT NULL,
  version TEXT NOT NULL,
  health TEXT NOT NULL DEFAULT '探针绿' CHECK(health IN ('探针绿','探针红')),
  release_id INTEGER,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(env_id) REFERENCES environments(id),
  FOREIGN KEY(release_id) REFERENCES releases(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
"""

RELEASE_STATUSES = ("待入闸", "排队中", "已入闸", "晋升中", "已放行", "回滚中", "已回滚", "已关闭")
# 在途态枚举：处于这些状态的发版单持有应用占用位（app_locks），同应用同时刻只许一单
ACTIVE_RELEASE_STATUSES = ("已入闸", "晋升中", "已放行", "回滚中")
# 终态：离开在途、释放占用位，并唤醒本应用排队单
TERMINAL_RELEASE_STATUSES = ("已回滚", "已关闭")
PIPELINE_STATUSES = ("未回执", "绿", "红")
HEALTH_STATUSES = ("探针绿", "探针红")
WINDOW_STATUSES = ("窗开", "窗关")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 入闸互斥走 BEGIN IMMEDIATE 单写者事务，WAL + busy_timeout 让并发写口排队而不是立即报锁
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx():
    """单写者写事务：入口即拿写锁，事务内所有读按写时规则重放，杜绝双入闸 TOCTOU。"""
    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _column_names(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


_RELEASES_NEW_DDL = """
CREATE TABLE releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL UNIQUE,
  app_id INTEGER NOT NULL,
  target_version TEXT NOT NULL,
  artifact_digest TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '待入闸'
    CHECK(status IN ('待入闸','排队中','已入闸','晋升中','已放行','回滚中','已回滚','已关闭')),
  pipeline_status TEXT NOT NULL DEFAULT '未回执'
    CHECK(pipeline_status IN ('未回执','绿','红')),
  current_env_id INTEGER,
  canary_ratio INTEGER NOT NULL DEFAULT 10,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  admitted_at TEXT,
  queued_at TEXT,
  released_at TEXT,
  rolled_back_at TEXT,
  FOREIGN KEY(app_id) REFERENCES apps(id),
  FOREIGN KEY(current_env_id) REFERENCES environments(id)
)
"""


def _migrate_legacy_releases(conn) -> None:
    """旧库 releases 表的 CHECK 不含排队态，SQLite 无法改约束，按官方 12 步重建表。"""
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='releases'"
    ).fetchone()
    if ddl and "排队中" in ddl[0]:
        return
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE releases RENAME TO releases_legacy")
        conn.execute(_RELEASES_NEW_DDL)
        old_cols = _column_names(conn, "releases_legacy")
        shared = [
            c for c in (
                "id", "serial", "app_id", "target_version", "artifact_digest", "status",
                "pipeline_status", "current_env_id", "canary_ratio", "note",
                "created_at", "admitted_at", "released_at", "rolled_back_at",
            ) if c in old_cols
        ]
        cols = ", ".join(shared)
        conn.execute(f"INSERT INTO releases({cols}, queued_at) SELECT {cols}, NULL FROM releases_legacy")
        conn.execute("DROP TABLE releases_legacy")
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute(f"PRAGMA foreign_keys={'ON' if had_fk else 'OFF'}")


def init_db() -> None:
    conn = connect()
    try:
        # 先跑迁移再建表：老库 releases 重建为含排队态 CHECK 的新表
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='releases'"
        ).fetchone()
        if exists:
            _migrate_legacy_releases(conn)
        conn.executescript(SCHEMA)
        if "queued_at" not in _column_names(conn, "releases"):
            conn.execute("ALTER TABLE releases ADD COLUMN queued_at TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    with db() as conn:
        # 旧库回填：每个在途应用补占用位（同应用多笔在途时最早入闸者持位，半截双挂留给写口收口）
        conn.execute(
            """
            INSERT OR IGNORE INTO app_locks(app_id, release_id, since)
            SELECT r.app_id, r.id, COALESCE(r.admitted_at, r.created_at)
            FROM releases r
            JOIN (
              SELECT app_id, MIN(id) AS min_id FROM releases
              WHERE status IN ('已入闸','晋升中','已放行','回滚中')
              GROUP BY app_id
            ) m ON m.app_id = r.app_id AND m.min_id = r.id
            """
        )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
