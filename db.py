"""SQLite 数据库管理模块"""
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime


class Database:
    def __init__(self, db_path="nas_bridge.db"):
        self.db_path = db_path
        self.init_db()

    @contextmanager
    def get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self):
        with self.get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                production_path TEXT,
                group_path TEXT,
                source_root TEXT,
                sync_status TEXT DEFAULT 'pending',
                sync_progress TEXT DEFAULT '',
                delivery_status TEXT DEFAULT 'pending',
                custom_status TEXT DEFAULT '',
                last_synced_at TEXT,
                last_delivered_at TEXT,
                is_special INTEGER DEFAULT 0,
                special_config TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )""")
            # 迁移：旧表缺少 custom_status 列时自动添加
            try:
                c.execute("SELECT custom_status FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                c.execute(
                    "ALTER TABLE projects ADD COLUMN custom_status TEXT DEFAULT ''")
            # 迁移：添加集数字段
            try:
                c.execute("SELECT total_episodes FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                c.execute(
                    "ALTER TABLE projects ADD COLUMN total_episodes INTEGER DEFAULT 0")
            try:
                c.execute("SELECT current_episodes FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                c.execute(
                    "ALTER TABLE projects ADD COLUMN current_episodes INTEGER DEFAULT 0")
            try:
                c.execute("SELECT episode_plan FROM projects LIMIT 1")
            except sqlite3.OperationalError:
                c.execute(
                    "ALTER TABLE projects ADD COLUMN episode_plan TEXT DEFAULT '{}'")
            c.execute("""CREATE TABLE IF NOT EXISTS sync_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT,
                action TEXT,
                direction TEXT,
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                status TEXT,
                message TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS delivery_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT,
                file_name TEXT,
                source_path TEXT,
                dest_path TEXT,
                file_size INTEGER DEFAULT 0,
                status TEXT,
                message TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS deliver_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL,
                src TEXT,
                dst TEXT,
                total_files INTEGER DEFAULT 0,
                status TEXT,
                message TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )""")

    # ---------- 项目 CRUD ----------
    def upsert_project(self, name, production_path, group_path,
                       source_root="", is_special=0, special_config=None):
        sc = json.dumps(special_config or {}, ensure_ascii=False)
        with self.get_conn() as conn:
            conn.execute(
                """INSERT INTO projects (name, production_path, group_path, source_root, is_special, special_config)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       production_path=excluded.production_path,
                       group_path=excluded.group_path,
                       source_root=excluded.source_root,
                       is_special=excluded.is_special,
                       special_config=excluded.special_config""",
                (name, production_path, group_path, source_root, is_special, sc))

    def get_project(self, name):
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None

    def get_all_projects(self):
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def update_project_status(self, name, **kwargs):
        if not kwargs:
            return
        fields = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values()) + [name]
        with self.get_conn() as conn:
            conn.execute(
                f"UPDATE projects SET {fields} WHERE name=?", values)

    def set_episode_plan(self, name, plan_dict):
        """保存剪辑人员分配表（JSON: {"1": "张三", "2": "李四", ...}）"""
        import json as _j
        if not isinstance(plan_dict, dict):
            plan_dict = {}
        with self.get_conn() as conn:
            conn.execute(
                "UPDATE projects SET episode_plan=? WHERE name=?",
                (_j.dumps(plan_dict, ensure_ascii=False), name))

    def get_episode_plan(self, name):
        p = self.get_project(name)
        if not p:
            return {}
        raw = p.get("episode_plan") or "{}"
        try:
            import json as _j
            v = _j.loads(raw) if isinstance(raw, str) else raw
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def delete_project(self, name):
        with self.get_conn() as conn:
            conn.execute("DELETE FROM projects WHERE name=?", (name,))

    # ---------- 同步日志 ----------
    def add_sync_log(self, project_name, action, direction="", file_path="",
                     file_size=0, status="info", message=""):
        with self.get_conn() as conn:
            conn.execute(
                """INSERT INTO sync_logs
                   (project_name, action, direction, file_path, file_size, status, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_name, action, direction, file_path,
                 file_size, status, message))

    def get_sync_logs(self, project_name=None, limit=50):
        with self.get_conn() as conn:
            if project_name:
                rows = conn.execute(
                    "SELECT * FROM sync_logs WHERE project_name=? ORDER BY id DESC LIMIT ?",
                    (project_name, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sync_logs ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ---------- 交付日志 ----------
    def add_delivery_log(self, project_name, file_name, source_path="",
                         dest_path="", file_size=0, status="info", message=""):
        with self.get_conn() as conn:
            conn.execute(
                """INSERT INTO delivery_logs
                   (project_name, file_name, source_path, dest_path, file_size, status, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_name, file_name, source_path, dest_path,
                 file_size, status, message))

    def get_delivery_logs(self, project_name=None, limit=50):
        with self.get_conn() as conn:
            if project_name:
                rows = conn.execute(
                    "SELECT * FROM delivery_logs WHERE project_name=? ORDER BY id DESC LIMIT ?",
                    (project_name, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM delivery_logs ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ---------- 综合日志 ----------
    def get_recent_logs(self, limit=100):
        with self.get_conn() as conn:
            rows = conn.execute(
                """SELECT 'sync' AS type, project_name, action AS title,
                          status, message, created_at
                   FROM sync_logs
                   UNION ALL
                   SELECT 'delivery' AS type, project_name, file_name AS title,
                          status, message, created_at
                   FROM delivery_logs
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ---------- 一键交付历史 ----------
    def insert_deliver_run(self, project_name, src="", dst="",
                           total_files=0, status="running", message="",
                           started_at=""):
        with self.get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO deliver_runs
                   (project_name, src, dst, total_files, status, message, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_name, src, dst, total_files,
                 status, message, started_at))
            return cur.lastrowid

    def finish_deliver_run(self, run_id, status, message="", finished_at=""):
        with self.get_conn() as conn:
            conn.execute(
                """UPDATE deliver_runs SET status=?, message=?, finished_at=?
                   WHERE id=?""",
                (status, message, finished_at, run_id))

    def get_deliver_runs(self, project_name=None, limit=30):
        with self.get_conn() as conn:
            if project_name:
                rows = conn.execute(
                    "SELECT * FROM deliver_runs WHERE project_name=? ORDER BY id DESC LIMIT ?",
                    (project_name, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM deliver_runs ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            return [dict(r) for r in rows]
