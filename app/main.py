from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, settings
from .db import db
from .assisted_login import assisted_login
from .dola_client import (
    DolaClient,
    check_account_quota,
    parse_cookie_header,
    validate_cookies,
)
from .worker import worker

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("dola.app")

app = FastAPI(
    title="Dola Seedance 2 Batch",
    description="Multi-account LIVE Seedance 2.0 batch via Dola internal API",
    version="1.1.0-live",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.api_token else [],
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = ROOT / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

_START_TS = time.time()


@app.middleware("http")
async def production_guards(request: Request, call_next: Callable):
    # Optional API token for mutating / sensitive routes when exposed
    if settings.api_token:
        path = request.url.path
        open_paths = {"/", "/api/health", "/docs", "/openapi.json"}
        is_static = path.startswith("/static/")
        if path not in open_paths and not is_static:
            auth = request.headers.get("Authorization", "")
            token = ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            token = token or request.headers.get("X-API-Token", "")
            if token != settings.api_token:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        return await call_next(request)
    except Exception as e:
        log.exception("unhandled %s %s", request.method, request.url.path)
        return JSONResponse({"detail": str(e)}, status_code=500)


# ── schemas ───────────────────────────────────────────────────


class AccountCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    cookies: str = Field(..., min_length=5, description="Cookie header / JSON / Netscape")
    email: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    daily_limit: int = 2
    enabled: bool = True


class AccountUpdate(BaseModel):
    label: str | None = None
    cookies: str | None = None
    email: str | None = None
    headers: dict[str, str] | None = None
    daily_limit: int | None = None
    daily_used: int | None = None
    enabled: bool | None = None


class JobCreate(BaseModel):
    prompt: str = Field(..., min_length=1)
    mode: str = "text2video"
    image_url: str = ""
    image_path: str = ""
    ratio: str = "16:9"
    duration: int = 5
    resolution: str = "720p"
    model: str = "seedance_v2.0"
    account_id: str | None = None


class BatchCreate(BaseModel):
    prompts: list[str] = Field(..., min_length=1)
    mode: str = "text2video"
    image_url: str = ""
    image_path: str = ""
    ratio: str = "16:9"
    duration: int = 5
    resolution: str = "720p"
    model: str = "seedance_v2.0"
    account_id: str | None = None


class RawApiCall(BaseModel):
    account_id: str
    method: str = "POST"
    path: str
    body: dict[str, Any] = Field(default_factory=dict)


class BrowserLoginRequest(BaseModel):
    label: str = "gmail-account"
    email: str = ""
    password: str = ""
    headless: bool = False
    timeout_sec: int = 300
    daily_limit: int = 2
    # async=True: trả session id ngay, poll /api/accounts/login-status/{id}
    async_mode: bool = True


class AssistedLoginRequest(BaseModel):
    """Cách 2: mở Chrome tuần tự, BẠN tự login — app chỉ lấy session + check quota."""
    count: int = Field(1, ge=1, le=50, description="Số lượt login tuần tự (max 50/batch)")
    label_prefix: str = "acc"
    timeout_sec: int = 300
    headless: bool = False


# ── lifecycle ─────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup() -> None:
    await db.connect()
    worker.start()
    mode = "DEMO" if settings.demo_mode else "LIVE"
    log.info(
        "Dola Seedance %s production-ready on %s:%s env=%s demo=%s base=%s",
        mode,
        settings.host,
        settings.port,
        settings.env,
        settings.demo_mode,
        settings.base_url,
    )
    if settings.demo_mode and settings.is_production:
        log.warning("⚠️  DEMO_MODE is ON while env=production — set DOLA_DEMO_MODE=false for real videos")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await worker.stop()
    await db.close()


# ── pages ─────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ── health / stats ────────────────────────────────────────────


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "live": not settings.demo_mode,
        "mode": "demo" if settings.demo_mode else "live",
        "env": settings.env,
        "worker": worker.running,
        "demo_mode": settings.demo_mode,
        "base_url": settings.base_url,
        "bot_id": settings.bot_id,
        "app_id": settings.app_id,
        "uptime_sec": int(time.time() - _START_TS),
        "version": "1.1.0-live",
    }


@app.get("/api/stats")
async def stats() -> dict[str, Any]:
    accounts = await db.list_accounts()
    by_status = await db.count_jobs_by_status()
    return {
        "accounts": len(accounts),
        "accounts_ok": sum(1 for a in accounts if a.get("status") == "ok"),
        "accounts_enabled": sum(1 for a in accounts if a.get("enabled")),
        "accounts_expired": sum(1 for a in accounts if a.get("status") == "expired"),
        "jobs": by_status,
        "worker": worker.running,
        "demo_mode": settings.demo_mode,
        "live": not settings.demo_mode,
        "mode": "demo" if settings.demo_mode else "live",
        "env": settings.env,
        "max_global_concurrent": settings.max_global_concurrent,
        "max_job_attempts": settings.max_job_attempts,
    }


# ── accounts ──────────────────────────────────────────────────


@app.get("/api/accounts")
async def list_accounts() -> list[dict[str, Any]]:
    rows = await db.list_accounts()
    # strip full cookies from list
    for r in rows:
        r.pop("cookies", None)
    return rows


@app.post("/api/accounts")
async def create_account(body: AccountCreate) -> dict[str, Any]:
    # normalize cookies
    parsed = parse_cookie_header(body.cookies)
    if not parsed and not settings.demo_mode:
        raise HTTPException(400, "Invalid cookies format")
    cookie_str = body.cookies.strip()

    account = await db.create_account(
        label=body.label,
        cookies=cookie_str,
        email=body.email,
        headers=body.headers,
    )
    await db.update_account(
        account["id"],
        daily_limit=body.daily_limit,
        enabled=1 if body.enabled else 0,
    )

    # auto validate
    try:
        result = await validate_cookies(cookie_str, body.headers)
        await db.update_account(
            account["id"],
            status="ok" if result.get("ok") else "expired",
            email=result.get("email") or body.email,
            last_error="" if result.get("ok") else (result.get("error") or "invalid"),
            last_check_at=__import__("time").time(),
        )
    except Exception as e:
        await db.update_account(
            account["id"],
            status="error",
            last_error=str(e),
            last_check_at=__import__("time").time(),
        )

    account = await db.get_account(account["id"])
    assert account
    account.pop("cookies", None)
    return account


@app.patch("/api/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if body.label is not None:
        fields["label"] = body.label
    if body.cookies is not None:
        fields["cookies"] = body.cookies
    if body.email is not None:
        fields["email"] = body.email
    if body.headers is not None:
        fields["headers_json"] = body.headers
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.daily_used is not None:
        fields["daily_used"] = body.daily_used
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0

    account = await db.update_account(account_id, **fields)
    if not account:
        raise HTTPException(404, "Account not found")
    account.pop("cookies", None)
    return account


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str) -> dict[str, bool]:
    ok = await db.delete_account(account_id)
    if not ok:
        raise HTTPException(404, "Account not found")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/check")
async def check_account(account_id: str) -> dict[str, Any]:
    account = await db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    result = await validate_cookies(account["cookies"], account.get("headers") or {})
    await db.update_account(
        account_id,
        status="ok" if result.get("ok") else "expired",
        email=result.get("email") or account.get("email") or "",
        last_error="" if result.get("ok") else (result.get("error") or "invalid"),
        last_check_at=__import__("time").time(),
    )
    account = await db.get_account(account_id)
    assert account
    account.pop("cookies", None)
    return {"validation": result, "account": account}


@app.post("/api/accounts/{account_id}/reset-quota")
async def reset_quota(account_id: str) -> dict[str, Any]:
    import time as _t

    today = _t.strftime("%Y-%m-%d", _t.gmtime())
    account = await db.update_account(
        account_id,
        daily_used=0,
        status="ok",
        last_error="",
        has_quota_today=1,
        quota_left=None,
        quota_date=today,
        quota_note="manual local reset",
    )
    if not account:
        raise HTTPException(404, "Account not found")
    # re-apply with default total if known
    acc2 = await db.get_account(account_id)
    total = (acc2 or {}).get("quota_total") or (acc2 or {}).get("daily_limit") or 2
    account = await db.apply_quota(
        account_id,
        quota_left=int(total),
        quota_total=int(total),
        has_quota=True,
        note="manual local reset",
    )
    if account:
        account.pop("cookies", None)
    return account


async def _save_logged_in_account(
    *,
    label: str,
    cookies: str,
    email: str = "",
    daily_limit: int = 2,
) -> dict[str, Any]:
    account = await db.create_account(label=label, cookies=cookies, email=email)
    await db.update_account(account["id"], daily_limit=daily_limit, enabled=1)
    try:
        validation = await validate_cookies(cookies)
        await db.update_account(
            account["id"],
            status="ok" if validation.get("ok") else "unknown",
            email=validation.get("email") or email or "",
            last_error="" if validation.get("ok") else (validation.get("error") or ""),
            last_check_at=__import__("time").time(),
        )
    except Exception as e:
        await db.update_account(account["id"], status="error", last_error=str(e))
    account = await db.get_account(account["id"])
    assert account
    account.pop("cookies", None)
    return account


@app.post("/api/accounts/browser-login")
async def browser_login(body: BrowserLoginRequest) -> dict[str, Any]:
    """
    Tự mở Chromium → login Gmail trên Dola → lấy cookies → lưu account.

    Mặc định async: trả login session ngay, UI poll status.
    """
    from .browser_login import capture_cookies_via_browser, login_manager

    if body.async_mode:
        session = await login_manager.start(
            label=body.label or (body.email.split("@")[0] if body.email else "gmail-account"),
            email=body.email,
            password=body.password,
            headless=body.headless,
            timeout_sec=body.timeout_sec,
            daily_limit=body.daily_limit,
            save_account=_save_logged_in_account,
        )
        return {"async": True, "session": session}

    # Sync (blocking cho tới khi login xong)
    result = await capture_cookies_via_browser(
        label=body.label,
        email=body.email,
        password=body.password,
        headless=body.headless,
        timeout_sec=body.timeout_sec,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "login capture failed")

    account = await _save_logged_in_account(
        label=body.label or result.get("email_hint") or "gmail-account",
        cookies=result["cookie_header"],
        email=result.get("email_hint") or body.email or "",
        daily_limit=body.daily_limit,
    )
    return {
        "async": False,
        "account": account,
        "cookie_count": len(result.get("cookies") or {}),
    }


@app.get("/api/accounts/login-status/{session_id}")
async def login_status(session_id: str) -> dict[str, Any]:
    from .browser_login import login_manager

    session = login_manager.get(session_id)
    if not session:
        raise HTTPException(404, "Login session not found")
    data = login_manager._public(session)
    if session.account_id:
        acc = await db.get_account(session.account_id)
        if acc:
            acc.pop("cookies", None)
            data["account"] = acc
    return data


@app.get("/api/accounts/login-sessions")
async def login_sessions() -> list[dict[str, Any]]:
    from .browser_login import login_manager

    return login_manager.list_active()


@app.post("/api/accounts/assisted-login")
async def start_assisted_login(body: AssistedLoginRequest) -> dict[str, Any]:
    """
    Cách 2 — Assisted login tuần tự:
      Mỗi lượt mở 1 Chrome → BẠN tự login → app capture session + check quota.
    """

    async def save_account(
        *,
        label: str,
        cookies: str,
        email: str = "",
        daily_limit: int = 2,
    ) -> dict[str, Any]:
        return await _save_logged_in_account(
            label=label,
            cookies=cookies,
            email=email,
            daily_limit=daily_limit,
        )

    async def apply_quota(
        account_id: str,
        *,
        quota_left: int | None = None,
        has_quota: bool | None = None,
        note: str = "",
    ) -> Any:
        return await db.apply_quota(
            account_id,
            quota_left=quota_left,
            has_quota=has_quota,
            note=note,
        )

    try:
        session = await assisted_login.start(
            count=body.count,
            label_prefix=body.label_prefix,
            timeout_sec=body.timeout_sec,
            headless=body.headless,
            save_account=save_account,
            apply_quota=apply_quota,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"queue": session}


@app.get("/api/accounts/assisted-login/{queue_id}")
async def assisted_login_status(queue_id: str) -> dict[str, Any]:
    q = assisted_login.get_queue(queue_id)
    if not q:
        raise HTTPException(404, "Queue not found")
    return assisted_login.public(q)


@app.post("/api/accounts/assisted-login/cancel")
async def assisted_login_cancel() -> dict[str, Any]:
    await assisted_login.cancel()
    return {"ok": True}


@app.post("/api/accounts/{account_id}/check-quota")
async def check_quota(account_id: str) -> dict[str, Any]:
    account = await db.get_account(account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    if not account.get("cookies"):
        raise HTTPException(400, "Account has no cookies")

    result = await check_account_quota(
        account["cookies"], account.get("headers") or {}
    )
    updated = await db.apply_quota(
        account_id,
        quota_left=result.get("quota_left"),
        has_quota=result.get("has_quota"),
        note=str(result.get("note") or ""),
    )
    if result.get("email") and updated:
        await db.update_account(account_id, email=result["email"], status="ok")
        updated = await db.get_account(account_id)
    if updated:
        updated.pop("cookies", None)
    # if API couldn't get numeric left but is_limit false → keep has_quota
    if result.get("ok") and result.get("quota_left") is None and result.get("has_quota") is True:
        await db.apply_quota(
            account_id,
            has_quota=True,
            note=str(result.get("note") or "available (exact count unknown)"),
        )
        updated = await db.get_account(account_id)
        if updated:
            updated.pop("cookies", None)
    return {"quota": result, "account": updated}


@app.post("/api/accounts/check-quota-all")
async def check_quota_all() -> dict[str, Any]:
    accounts = await db.list_accounts()
    results = []
    for acc in accounts:
        if not acc.get("enabled") or not acc.get("has_cookies"):
            continue
        full = await db.get_account(acc["id"])
        if not full or not full.get("cookies"):
            continue
        try:
            q = await check_account_quota(full["cookies"], full.get("headers") or {})
            await db.apply_quota(
                acc["id"],
                quota_left=q.get("quota_left"),
                has_quota=q.get("has_quota"),
                note=str(q.get("note") or ""),
            )
            results.append(
                {
                    "id": acc["id"],
                    "label": acc["label"],
                    "ok": q.get("ok"),
                    "has_quota": q.get("has_quota"),
                    "quota_left": q.get("quota_left"),
                    "note": q.get("note"),
                }
            )
        except Exception as e:
            results.append({"id": acc["id"], "label": acc["label"], "ok": False, "error": str(e)})
    return {"count": len(results), "results": results}


# ── jobs ──────────────────────────────────────────────────────


@app.get("/api/jobs")
async def list_jobs(
    status: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    jobs = await db.list_jobs(limit=limit, status=status)
    for j in jobs:
        path = _job_video_path(j)
        j["has_video"] = path is not None
        j["video_size"] = path.stat().st_size if path else 0
        if path:
            j["stream_url"] = f"/api/jobs/{j['id']}/stream"
            j["download_url"] = f"/api/jobs/{j['id']}/download"
    return jobs


@app.post("/api/upload/image")
async def upload_image(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload reference image for image→video. Saved under uploads/."""
    import uuid as _uuid

    if not file.filename:
        raise HTTPException(400, "Missing filename")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 12MB)")
    # basic type check
    content_type = (file.content_type or "").lower()
    name_l = file.filename.lower()
    ok_ext = name_l.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"))
    ok_mime = content_type.startswith("image/") or not content_type
    if not (ok_ext or ok_mime):
        raise HTTPException(400, "Only image files allowed")
    ext = Path(file.filename).suffix.lower() or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}:
        ext = ".png"
    fid = _uuid.uuid4().hex[:16]
    dest = Path(settings.upload_dir) / f"{fid}{ext}"
    dest.write_bytes(raw)
    return {
        "id": fid,
        "filename": file.filename,
        "path": str(dest),
        "size": len(raw),
        "content_type": content_type or "image/*",
        "preview_url": f"/api/uploads/{dest.name}",
        "image_path": str(dest),
    }


@app.get("/api/uploads/{filename}")
async def get_upload(filename: str) -> FileResponse:
    # prevent path traversal
    safe = Path(filename).name
    path = Path(settings.upload_dir) / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Upload not found")
    return FileResponse(path)


@app.post("/api/jobs")
async def create_job(body: JobCreate) -> dict[str, Any]:
    if body.mode in ("image2video", "ref2video") and not (
        body.image_url or body.image_path
    ):
        raise HTTPException(400, "image2video requires image_url or image_path (upload)")
    return await db.create_job(
        prompt=body.prompt,
        mode=body.mode,
        image_url=body.image_url,
        image_path=body.image_path,
        ratio=body.ratio,
        duration=body.duration,
        resolution=body.resolution,
        model=body.model,
        account_id=body.account_id,
    )


@app.post("/api/jobs/batch")
async def create_batch(body: BatchCreate) -> dict[str, Any]:
    prompts = [p.strip() for p in body.prompts if p and p.strip()]
    if not prompts:
        raise HTTPException(400, "No prompts")
    if body.mode in ("image2video", "ref2video") and not (
        body.image_url or body.image_path
    ):
        raise HTTPException(400, "image2video batch requires image_url or image_path")
    jobs = await db.create_jobs_batch(
        prompts,
        mode=body.mode,
        image_url=body.image_url,
        image_path=body.image_path,
        ratio=body.ratio,
        duration=body.duration,
        resolution=body.resolution,
        model=body.model,
        account_id=body.account_id,
    )
    return {"count": len(jobs), "jobs": jobs}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict[str, Any]:
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    updated = await db.update_job(
        job_id,
        status="queued",
        error="",
        progress=0,
        video_url="",
        local_path="",
        finished_at=None,
        started_at=None,
        external_task_id="",
        conversation_id="",
        message_id="",
    )
    assert updated
    return updated


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, bool]:
    ok = await db.delete_job(job_id)
    if not ok:
        raise HTTPException(404, "Job not found")
    return {"ok": True}


def _job_video_path(job: dict[str, Any]) -> Path | None:
    path = job.get("local_path") or ""
    if path and Path(path).exists() and Path(path).stat().st_size > 1000:
        return Path(path)
    # fallback: downloads/<job_id>.mp4
    fallback = Path(settings.download_dir) / f"{job['id']}.mp4"
    if fallback.exists() and fallback.stat().st_size > 1000:
        return fallback
    return None


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str) -> FileResponse:
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    path = _job_video_path(job)
    if not path:
        raise HTTPException(404, "File not available")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"seedance_{job_id}.mp4",
        content_disposition_type="attachment",
    )


@app.get("/api/jobs/{job_id}/stream")
async def stream_job_video(job_id: str) -> FileResponse:
    """Stream video for in-page <video> player (inline)."""
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    path = _job_video_path(job)
    if not path:
        raise HTTPException(404, "File not available")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"seedance_{job_id}.mp4",
        content_disposition_type="inline",
    )


@app.get("/api/gallery")
async def gallery(limit: int = Query(48, ge=1, le=200)) -> list[dict[str, Any]]:
    """Completed jobs that have a local video file — for UI gallery."""
    jobs = await db.list_jobs(limit=limit * 2, status="success")
    out: list[dict[str, Any]] = []
    for j in jobs:
        path = _job_video_path(j)
        if not path:
            continue
        out.append(
            {
                "id": j["id"],
                "prompt": j.get("prompt") or "",
                "ratio": j.get("ratio") or "16:9",
                "duration": j.get("duration"),
                "created_at": j.get("created_at"),
                "finished_at": j.get("finished_at"),
                "size_bytes": path.stat().st_size,
                "stream_url": f"/api/jobs/{j['id']}/stream",
                "download_url": f"/api/jobs/{j['id']}/download",
            }
        )
        if len(out) >= limit:
            break
    return out


# ── raw internal API escape hatch ─────────────────────────────


@app.post("/api/raw")
async def raw_api(body: RawApiCall) -> dict[str, Any]:
    account = await db.get_account(body.account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    async with DolaClient(account["cookies"], extra_headers=account.get("headers") or {}) as client:
        result = await client.send_raw(body.method, body.path, body.body)
    return result


@app.post("/api/worker/start")
async def worker_start() -> dict[str, Any]:
    worker.start()
    return {"running": worker.running}


@app.post("/api/worker/stop")
async def worker_stop() -> dict[str, Any]:
    await worker.stop()
    return {"running": worker.running}


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
