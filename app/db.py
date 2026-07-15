from __future__ import annotations

import json
import time
import uuid
from typing import Any

import aiosqlite

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    email TEXT DEFAULT '',
    cookies TEXT NOT NULL DEFAULT '',
    headers_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_check_at REAL,
    last_error TEXT DEFAULT '',
    credits_left INTEGER,
    daily_used INTEGER DEFAULT 0,
    daily_limit INTEGER DEFAULT 2,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    account_id TEXT,
    prompt TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'text2video',
    image_url TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    ratio TEXT DEFAULT '16:9',
    duration INTEGER DEFAULT 5,
    resolution TEXT DEFAULT '720p',
    model TEXT DEFAULT 'seedance_2.0',
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL DEFAULT 0,
    external_task_id TEXT DEFAULT '',
    conversation_id TEXT DEFAULT '',
    message_id TEXT DEFAULT '',
    video_url TEXT DEFAULT '',
    local_path TEXT DEFAULT '',
    error TEXT DEFAULT '',
    raw_request TEXT DEFAULT '',
    raw_response TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account_id);
"""


def _now() -> float:
    return time.time()


def _id() -> str:
    return uuid.uuid4().hex[:12]


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = str(path or settings.db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Add quota tracking columns if missing."""
        cur = await self.conn.execute("PRAGMA table_info(accounts)")
        cols = {row[1] for row in await cur.fetchall()}
        alters = {
            "quota_date": "TEXT DEFAULT ''",
            "quota_left": "INTEGER",
            "quota_total": "INTEGER",
            "has_quota_today": "INTEGER DEFAULT 1",
            "quota_note": "TEXT DEFAULT ''",
            "last_quota_check_at": "REAL",
        }
        for name, typedef in alters.items():
            if name not in cols:
                await self.conn.execute(
                    f"ALTER TABLE accounts ADD COLUMN {name} {typedef}"
                )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected")
        return self._conn

    # ── accounts ──────────────────────────────────────────────

    async def list_accounts(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM accounts ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [self._account_row(r) for r in rows]

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        )
        row = await cur.fetchone()
        return self._account_row(row) if row else None

    async def create_account(
        self,
        label: str,
        cookies: str,
        email: str = "",
        headers: dict | None = None,
        meta: dict | None = None,
    ) -> dict[str, Any]:
        aid = _id()
        now = _now()
        await self.conn.execute(
            """
            INSERT INTO accounts
            (id, label, email, cookies, headers_json, status, meta_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?, ?)
            """,
            (
                aid,
                label,
                email,
                cookies.strip(),
                json.dumps(headers or {}),
                json.dumps(meta or {}),
                now,
                now,
            ),
        )
        await self.conn.commit()
        account = await self.get_account(aid)
        assert account
        return account

    async def update_account(self, account_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return await self.get_account(account_id)
        allowed = {
            "label",
            "email",
            "cookies",
            "headers_json",
            "status",
            "last_check_at",
            "last_error",
            "credits_left",
            "daily_used",
            "daily_limit",
            "meta_json",
            "enabled",
            "quota_date",
            "quota_left",
            "quota_total",
            "has_quota_today",
            "quota_note",
            "last_quota_check_at",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("headers_json", "meta_json") and isinstance(v, dict):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return await self.get_account(account_id)
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(account_id)
        await self.conn.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self.conn.commit()
        return await self.get_account(account_id)

    async def delete_account(self, account_id: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM accounts WHERE id = ?", (account_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def pick_available_account(self) -> dict[str, Any] | None:
        """Pick enabled account with remaining daily quota (local + Dola flags)."""
        # Soft-reset local counters if quota_date is not today
        today = time.strftime("%Y-%m-%d", time.gmtime())
        await self.conn.execute(
            """
            UPDATE accounts
            SET daily_used = 0,
                has_quota_today = 1,
                quota_left = CASE
                  WHEN quota_total IS NOT NULL THEN quota_total
                  WHEN daily_limit IS NOT NULL THEN daily_limit
                  ELSE quota_left END,
                quota_date = ?,
                quota_note = 'auto-reset new day (UTC)',
                updated_at = ?
            WHERE enabled = 1
              AND (quota_date IS NULL OR quota_date = '' OR quota_date != ?)
            """,
            (today, _now(), today),
        )
        await self.conn.commit()

        cur = await self.conn.execute(
            """
            SELECT a.*,
              (SELECT COUNT(*) FROM jobs j
               WHERE j.account_id = a.id AND j.status IN ('running','polling')) AS active_jobs
            FROM accounts a
            WHERE a.enabled = 1
              AND a.status IN ('ok', 'unknown')
              AND a.cookies != ''
              AND COALESCE(a.has_quota_today, 1) = 1
              AND (
                a.quota_left IS NULL
                OR a.quota_left > 0
              )
              AND (
                a.daily_used IS NULL
                OR a.daily_limit IS NULL
                OR a.daily_used < a.daily_limit
              )
            ORDER BY
              CASE WHEN a.quota_left IS NULL THEN 1 ELSE 0 END ASC,
              a.quota_left DESC,
              active_jobs ASC,
              a.daily_used ASC,
              a.updated_at ASC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        return self._account_row(row) if row else None

    async def apply_quota(
        self,
        account_id: str,
        *,
        quota_left: int | None = None,
        quota_total: int | None = None,
        has_quota: bool | None = None,
        note: str = "",
        consume: int = 0,
    ) -> dict[str, Any] | None:
        """Persist daily quota state for an account."""
        acc = await self.get_account(account_id)
        if not acc:
            return None
        today = time.strftime("%Y-%m-%d", time.gmtime())
        fields: dict[str, Any] = {
            "quota_date": today,
            "last_quota_check_at": _now(),
        }
        if note:
            fields["quota_note"] = note[:500]
        if quota_total is not None:
            fields["quota_total"] = int(quota_total)
            fields["daily_limit"] = int(quota_total)
        if quota_left is not None:
            fields["quota_left"] = max(0, int(quota_left))
            fields["credits_left"] = fields["quota_left"]
            # derive used if we know total
            total = fields.get("quota_total", acc.get("quota_total") or acc.get("daily_limit"))
            if total is not None:
                fields["daily_used"] = max(0, int(total) - fields["quota_left"])
            fields["has_quota_today"] = 1 if fields["quota_left"] > 0 else 0
        if has_quota is not None:
            fields["has_quota_today"] = 1 if has_quota else 0
            if not has_quota:
                fields["quota_left"] = 0
                fields["credits_left"] = 0
        if consume > 0:
            left = acc.get("quota_left")
            if left is None:
                left = acc.get("daily_limit")
                if left is not None:
                    left = int(left) - int(acc.get("daily_used") or 0)
            if left is not None:
                new_left = max(0, int(left) - consume)
                fields["quota_left"] = new_left
                fields["credits_left"] = new_left
                fields["has_quota_today"] = 1 if new_left > 0 else 0
            used = int(acc.get("daily_used") or 0) + consume
            fields["daily_used"] = used
            if fields.get("has_quota_today") == 0 and not note:
                fields["quota_note"] = "quota exhausted after use"
        return await self.update_account(account_id, **fields)

    # ── jobs ──────────────────────────────────────────────────

    async def list_jobs(self, limit: int = 200, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            cur = await self.conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        return [self._job_row(r) for r in rows]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return self._job_row(row) if row else None

    async def create_job(
        self,
        prompt: str,
        mode: str = "text2video",
        image_url: str = "",
        image_path: str = "",
        ratio: str = "16:9",
        duration: int = 5,
        resolution: str = "720p",
        model: str = "seedance_v2.0",
        account_id: str | None = None,
    ) -> dict[str, Any]:
        jid = _id()
        now = _now()
        await self.conn.execute(
            """
            INSERT INTO jobs
            (id, account_id, prompt, mode, image_url, image_path, ratio, duration,
             resolution, model, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                jid,
                account_id,
                prompt.strip(),
                mode,
                image_url,
                image_path,
                ratio,
                duration,
                resolution,
                model,
                now,
                now,
            ),
        )
        await self.conn.commit()
        job = await self.get_job(jid)
        assert job
        return job

    async def create_jobs_batch(self, prompts: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        jobs = []
        for p in prompts:
            p = p.strip()
            if not p:
                continue
            jobs.append(await self.create_job(prompt=p, **kwargs))
        return jobs

    async def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "account_id",
            "status",
            "progress",
            "external_task_id",
            "conversation_id",
            "message_id",
            "video_url",
            "local_path",
            "error",
            "raw_request",
            "raw_response",
            "attempts",
            "started_at",
            "finished_at",
            "prompt",
            "mode",
            "ratio",
            "duration",
            "resolution",
            "model",
            "image_url",
            "image_path",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return await self.get_job(job_id)
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(job_id)
        await self.conn.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self.conn.commit()
        return await self.get_job(job_id)

    async def delete_job(self, job_id: str) -> bool:
        cur = await self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def next_queued_job(self) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        return self._job_row(row) if row else None

    async def count_jobs_by_status(self) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
        )
        rows = await cur.fetchall()
        return {r["status"]: r["c"] for r in rows}

    async def count_active_for_account(self, account_id: str) -> int:
        cur = await self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE account_id = ? AND status IN ('running', 'polling')
            """,
            (account_id,),
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    # ── helpers ───────────────────────────────────────────────

    def _account_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("headers_json", "meta_json"):
            try:
                d[k.replace("_json", "")] = json.loads(d.get(k) or "{}")
            except json.JSONDecodeError:
                d[k.replace("_json", "")] = {}
        d["enabled"] = bool(d.get("enabled", 1))
        d["has_quota_today"] = bool(d.get("has_quota_today", 1))
        # never expose full cookies in list responses unless needed
        d["has_cookies"] = bool(d.get("cookies"))
        d["cookies_preview"] = (d.get("cookies") or "")[:40] + (
            "…" if len(d.get("cookies") or "") > 40 else ""
        )
        # computed view
        ql = d.get("quota_left")
        if ql is None and d.get("daily_limit") is not None:
            ql = max(0, int(d.get("daily_limit") or 0) - int(d.get("daily_used") or 0))
        d["quota_display"] = {
            "left": ql,
            "total": d.get("quota_total") or d.get("daily_limit"),
            "used": d.get("daily_used") or 0,
            "has_quota": bool(d.get("has_quota_today", 1)) and (ql is None or int(ql) > 0),
            "date": d.get("quota_date") or "",
            "note": d.get("quota_note") or "",
        }
        return d

    def _job_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        return dict(row)


db = Database()
