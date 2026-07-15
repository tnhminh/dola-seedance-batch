"""
Assisted login (Cách 2):
  - Mở 1 Chrome
  - BẠN tự login Gmail/Dola
  - App detect session → lưu account → check quota
  - Có thể xếp hàng nhiều lượt login tuần tự
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .browser_login import capture_cookies_via_browser
from .dola_client import check_account_quota, cookies_to_header, validate_cookies

log = logging.getLogger("dola.assisted")


@dataclass
class AssistedSlot:
    index: int
    label: str
    status: str = "pending"  # pending|waiting_user|capturing|saving|done|error|skipped
    message: str = ""
    account_id: str | None = None
    error: str = ""
    quota: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistedQueue:
    id: str
    total: int
    status: str = "running"  # running|paused|done|error|cancelled
    message: str = ""
    current: int = 0
    slots: list[AssistedSlot] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class AssistedLoginService:
    def __init__(self) -> None:
        self._queues: dict[str, AssistedQueue] = {}
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()

    def get_queue(self, qid: str) -> AssistedQueue | None:
        return self._queues.get(qid)

    def public(self, q: AssistedQueue) -> dict[str, Any]:
        return {
            "id": q.id,
            "total": q.total,
            "current": q.current,
            "status": q.status,
            "message": q.message,
            "done": sum(1 for s in q.slots if s.status == "done"),
            "errors": sum(1 for s in q.slots if s.status == "error"),
            "slots": [
                {
                    "index": s.index,
                    "label": s.label,
                    "status": s.status,
                    "message": s.message,
                    "account_id": s.account_id,
                    "error": s.error,
                    "quota": s.quota,
                }
                for s in q.slots
            ],
            "created_at": q.created_at,
            "finished_at": q.finished_at,
        }

    async def start(
        self,
        *,
        count: int = 1,
        label_prefix: str = "acc",
        timeout_sec: int = 300,
        headless: bool = False,
        save_account: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        apply_quota: Callable[..., Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        if self._task and not self._task.done():
            raise RuntimeError("Assisted login queue already running")

        count = max(1, min(int(count), 50))  # hard cap per batch (safety)
        qid = uuid.uuid4().hex[:10]
        slots = [
            AssistedSlot(index=i + 1, label=f"{label_prefix}-{i + 1:03d}")
            for i in range(count)
        ]
        q = AssistedQueue(
            id=qid,
            total=count,
            status="running",
            message=f"Sẵn sàng login {count} account — mỗi lượt mở 1 Chrome, BẠN tự đăng nhập",
            slots=slots,
        )
        self._queues[qid] = q
        self._cancel.clear()

        self._task = asyncio.create_task(
            self._run(
                q,
                timeout_sec=timeout_sec,
                headless=headless,
                save_account=save_account,
                apply_quota=apply_quota,
            ),
            name=f"assisted-{qid}",
        )
        return self.public(q)

    async def cancel(self) -> None:
        self._cancel.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(
        self,
        q: AssistedQueue,
        *,
        timeout_sec: int,
        headless: bool,
        save_account: Callable | None,
        apply_quota: Callable | None,
    ) -> None:
        try:
            for slot in q.slots:
                if self._cancel.is_set():
                    q.status = "cancelled"
                    q.message = "Đã huỷ queue login"
                    break

                q.current = slot.index
                slot.status = "waiting_user"
                slot.message = (
                    f"[{slot.index}/{q.total}] Đang mở Chrome — "
                    "HÃY TỰ LOGIN Gmail/Dola trong cửa sổ browser"
                )
                q.message = slot.message
                log.info(slot.message)

                result = await capture_cookies_via_browser(
                    label=slot.label,
                    email="",  # không auto-fill password
                    password="",
                    headless=headless,
                    timeout_sec=timeout_sec,
                    login_url="https://www.dola.com/chat/create-video",
                    on_status=lambda st, msg, s=slot, qq=q: self._on_status(s, qq, st, msg),
                )

                if self._cancel.is_set():
                    slot.status = "skipped"
                    slot.message = "cancelled"
                    break

                if not result.get("ok"):
                    slot.status = "error"
                    slot.error = result.get("error") or "login failed"
                    slot.message = slot.error
                    q.message = f"[{slot.index}/{q.total}] Lỗi: {slot.error}"
                    continue

                slot.status = "saving"
                slot.message = "Đã lấy session — đang lưu + check quota…"
                q.message = slot.message

                cookies = result.get("cookie_header") or cookies_to_header(
                    result.get("cookies") or {}
                )
                email = result.get("email_hint") or ""

                # validate
                try:
                    val = await validate_cookies(cookies)
                    if val.get("email"):
                        email = val["email"]
                except Exception as e:
                    val = {"ok": False, "error": str(e)}

                account = None
                if save_account:
                    account = await save_account(
                        label=slot.label,
                        cookies=cookies,
                        email=email or val.get("email") or "",
                        daily_limit=2,
                    )
                    slot.account_id = (account or {}).get("id")

                # quota check
                try:
                    quota = await check_account_quota(cookies)
                    slot.quota = {
                        "has_quota": quota.get("has_quota"),
                        "quota_left": quota.get("quota_left"),
                        "note": quota.get("note"),
                    }
                    if apply_quota and slot.account_id:
                        await apply_quota(
                            slot.account_id,
                            quota_left=quota.get("quota_left"),
                            has_quota=quota.get("has_quota"),
                            note=str(quota.get("note") or ""),
                        )
                except Exception as e:
                    slot.quota = {"error": str(e)}

                if not val.get("ok"):
                    slot.status = "error"
                    slot.error = val.get("error") or "session invalid after login"
                    slot.message = slot.error
                else:
                    slot.status = "done"
                    qleft = slot.quota.get("quota_left")
                    qnote = f"quota_left={qleft}" if qleft is not None else "quota checked"
                    slot.message = f"OK · {email or slot.label} · {qnote}"
                q.message = f"[{slot.index}/{q.total}] {slot.message}"

            if q.status == "running":
                q.status = "done"
                done = sum(1 for s in q.slots if s.status == "done")
                q.message = f"Hoàn tất assisted login: {done}/{q.total} account"
            q.finished_at = time.time()
        except Exception as e:
            log.exception("assisted queue failed")
            q.status = "error"
            q.message = str(e)
            q.finished_at = time.time()

    def _on_status(self, slot: AssistedSlot, q: AssistedQueue, st: str, msg: str) -> None:
        slot.status = "waiting_user" if st in ("waiting_login", "browser_open") else st
        slot.message = f"[{slot.index}/{q.total}] {msg}"
        q.message = slot.message


assisted_login = AssistedLoginService()
