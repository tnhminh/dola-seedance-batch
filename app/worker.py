"""Production batch worker: assign → Dola live API → poll → download."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .config import settings
from .db import db
from .dola_client import DolaClient

log = logging.getLogger("dola.worker")


class BatchWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._active_tasks: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(settings.max_global_concurrent)

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="batch-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        # cancel in-flight job tasks
        for t in list(self._active_tasks):
            t.cancel()
        self._running = False

    async def _loop(self) -> None:
        self._running = True
        log.info(
            "Batch worker LIVE started (demo=%s, max_global=%s)",
            settings.demo_mode,
            settings.max_global_concurrent,
        )
        try:
            while not self._stop.is_set():
                # prune done tasks
                self._active_tasks = {t for t in self._active_tasks if not t.done()}
                try:
                    spawned = await self._spawn_available()
                except Exception:
                    log.exception("worker spawn failed")
                    spawned = 0
                await asyncio.sleep(1.0 if spawned else 2.5)
        finally:
            self._running = False
            log.info("Batch worker stopped")

    async def _spawn_available(self) -> int:
        free_slots = settings.max_global_concurrent - len(self._active_tasks)
        if free_slots <= 0:
            return 0

        spawned = 0
        for _ in range(free_slots):
            job = await db.next_queued_job()
            if not job:
                break

            attempts = int(job.get("attempts") or 0)
            if attempts >= settings.max_job_attempts:
                await db.update_job(
                    job["id"],
                    status="failed",
                    error=f"Exceeded max attempts ({settings.max_job_attempts})",
                    finished_at=time.time(),
                )
                continue

            account = None
            if job.get("account_id"):
                account = await db.get_account(job["account_id"])
                if not account or not account.get("enabled") or not account.get("cookies"):
                    await db.update_job(
                        job["id"],
                        status="failed",
                        error="Assigned account missing/disabled/no cookies",
                        finished_at=time.time(),
                    )
                    continue
            else:
                account = await db.pick_available_account()
                if not account:
                    break

            active = await db.count_active_for_account(account["id"])
            if active >= settings.max_concurrent_per_account:
                # leave job queued; try next tick
                # mark temporarily so we don't spin — requeue by leaving as queued
                # skip this job by not claiming — pick another later
                # simple approach: if only this job, sleep
                break

            # claim job immediately to avoid double spawn
            await db.update_job(
                job["id"],
                account_id=account["id"],
                status="running",
                started_at=time.time(),
                attempts=attempts + 1,
                error="",
                progress=2,
            )

            task = asyncio.create_task(
                self._run_job_guarded(job, account),
                name=f"job-{job['id']}",
            )
            self._active_tasks.add(task)
            spawned += 1
        return spawned

    async def _run_job_guarded(self, job: dict[str, Any], account: dict[str, Any]) -> None:
        async with self._sem:
            try:
                await self._run_job(job, account)
            except Exception as e:
                log.exception("job %s crashed", job["id"])
                await db.update_job(
                    job["id"],
                    status="failed",
                    error=str(e),
                    finished_at=time.time(),
                )

    async def _run_job(self, job: dict[str, Any], account: dict[str, Any]) -> None:
        jid = job["id"]
        log.info("job %s start on account %s (%s)", jid, account["id"], account.get("label"))

        async with DolaClient(
            account["cookies"],
            extra_headers=account.get("headers") or {},
        ) as client:
            check = await client.validate_session()
            if not check.get("ok"):
                await db.update_account(
                    account["id"],
                    status="expired",
                    last_error=check.get("error") or "session invalid",
                    last_check_at=time.time(),
                )
                # requeue without account so another can pick up
                await db.update_job(
                    jid,
                    status="queued",
                    account_id=None,
                    error=f"Account expired: {check.get('error')}",
                    progress=0,
                )
                return

            if check.get("email"):
                await db.update_account(
                    account["id"],
                    email=check["email"],
                    status="ok",
                    last_check_at=time.time(),
                    last_error="",
                )
            else:
                await db.update_account(
                    account["id"],
                    status="ok",
                    last_check_at=time.time(),
                    last_error="",
                )

            # ── quota gate before gen ──
            q = await client.check_quota()
            await db.apply_quota(
                account["id"],
                quota_left=q.get("quota_left"),
                has_quota=q.get("has_quota"),
                note=str(q.get("note") or ""),
            )
            if q.get("has_quota") is False or (
                q.get("quota_left") is not None and int(q["quota_left"]) <= 0
            ):
                await db.update_job(
                    jid,
                    status="queued",
                    account_id=None,
                    error=f"Account out of daily quota: {q.get('note')}",
                    progress=0,
                )
                log.info("job %s skip account %s — no quota", jid, account["id"])
                return

            await asyncio.sleep(settings.account_cooldown_sec)

            result = await client.create_video(
                prompt=job["prompt"],
                mode=job.get("mode") or "text2video",
                ratio=job.get("ratio") or "16:9",
                duration=int(job.get("duration") or 5),
                resolution=job.get("resolution") or "720p",
                model=job.get("model") or "seedance_v2.0",
                image_url=job.get("image_url") or "",
                image_path=job.get("image_path") or "",
            )

            await db.update_job(
                jid,
                raw_request=_safe_str(result.get("request_body"), 8000),
                raw_response=_safe_str(result.get("raw"), 8000),
                conversation_id=result.get("conversation_id") or "",
                message_id=result.get("message_id") or "",
                external_task_id=result.get("task_id") or "",
                progress=15,
            )

            # always refresh quota from SSE brief if present
            qinfo = result.get("quota") or {}
            if qinfo.get("found") or qinfo.get("quota_left") is not None:
                await db.apply_quota(
                    account["id"],
                    quota_left=qinfo.get("quota_left"),
                    has_quota=qinfo.get("has_quota"),
                    note=str(qinfo.get("note") or result.get("brief") or ""),
                )

            if not result.get("ok"):
                err = result.get("error") or "create_video failed"
                low = err.lower()
                if any(
                    k in low
                    for k in (
                        "quota",
                        "limit",
                        "credit",
                        "rate",
                        "too many",
                        "busy",
                        "point",
                        "lượt",
                    )
                ):
                    await db.apply_quota(
                        account["id"],
                        quota_left=0,
                        has_quota=False,
                        note=err,
                    )
                    await db.update_job(
                        jid,
                        status="queued",
                        account_id=None,
                        error=f"Quota/rate: {err}",
                        progress=0,
                    )
                    return

                if any(k in low for k in ("session", "login", "expired", "auth")):
                    await db.update_account(
                        account["id"],
                        status="expired",
                        last_error=err,
                    )
                    await db.update_job(
                        jid,
                        status="queued",
                        account_id=None,
                        error=f"Auth: {err}",
                        progress=0,
                    )
                    return

                # retry later if attempts remain
                attempts = int((await db.get_job(jid) or {}).get("attempts") or 1)
                if attempts < settings.max_job_attempts:
                    await asyncio.sleep(settings.retry_backoff_sec)
                    await db.update_job(
                        jid,
                        status="queued",
                        error=f"Retryable: {err}",
                        progress=0,
                    )
                    return

                await db.update_job(
                    jid,
                    status="failed",
                    error=err,
                    finished_at=time.time(),
                )
                return

            await db.update_job(jid, status="polling", progress=20)

            deadline = time.time() + settings.job_timeout_sec
            video_url = ""
            last_err = ""
            while time.time() < deadline and not self._stop.is_set():
                st = await client.poll_video_status(
                    task_id=result.get("task_id") or "",
                    conversation_id=result.get("conversation_id") or "",
                    message_id=result.get("message_id") or "",
                )
                progress = max(20.0, float(st.get("progress") or 20))
                await db.update_job(
                    jid,
                    progress=progress,
                    raw_response=_safe_str(st.get("raw"), 8000),
                )

                if st.get("status") == "success" and st.get("video_url"):
                    video_url = st["video_url"]
                    break
                if st.get("status") == "failed":
                    last_err = st.get("error") or "generation failed"
                    break

                await asyncio.sleep(settings.poll_interval_sec)

            if not video_url:
                attempts = int((await db.get_job(jid) or {}).get("attempts") or 1)
                if attempts < settings.max_job_attempts and not last_err:
                    await db.update_job(
                        jid,
                        status="queued",
                        error="Timeout — will retry",
                        progress=0,
                    )
                    return
                await db.update_job(
                    jid,
                    status="failed",
                    error=last_err or "Timeout waiting for video",
                    finished_at=time.time(),
                )
                return

            dest_dir = Path(settings.download_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{jid}.mp4"
            local_path = ""
            try:
                await client.download_video(video_url, str(dest))
                # validate non-empty
                if dest.exists() and dest.stat().st_size > 100:
                    local_path = str(dest)
                else:
                    log.warning("download too small for job %s", jid)
            except Exception as e:
                log.warning("download failed %s: %s", jid, e)

            await db.update_job(
                jid,
                status="success",
                progress=100,
                video_url=video_url,
                local_path=local_path,
                finished_at=time.time(),
                error="",
            )
            # consume 1 quota unit after successful gen
            await db.apply_quota(
                account["id"],
                consume=1,
                note="consumed after successful video",
            )
            # if SSE told remaining points, prefer that
            if qinfo.get("found") and qinfo.get("quota_left") is not None:
                await db.apply_quota(
                    account["id"],
                    quota_left=qinfo.get("quota_left"),
                    has_quota=qinfo.get("has_quota"),
                    note=str(qinfo.get("note") or ""),
                )
            log.info("job %s success url=%s local=%s", jid, bool(video_url), bool(local_path))


def _safe_str(obj: Any, limit: int = 8000) -> str:
    try:
        import json

        if isinstance(obj, (dict, list)):
            s = json.dumps(obj, ensure_ascii=False, default=str)
        else:
            s = str(obj)
    except Exception:
        s = str(obj)
    return s[:limit]


worker = BatchWorker()
