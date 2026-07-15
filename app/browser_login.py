"""
Tự động mở Chromium → login Dola (Gmail) → capture cookies/session.

Flow:
  1. Mở https://www.dola.com/chat/
  2. Tự bấm nút Google / Continue with Google nếu thấy
  3. (Tuỳ chọn) điền email/password nếu user cung cấp
  4. Chờ session cookie xuất hiện → lấy cookie → đóng browser
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .dola_client import cookies_to_header

log = logging.getLogger("dola.browser")

SESSION_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "sid_guard",
    "uid_tt",
    "uid_tt_ss",
}

GOOGLE_SELECTORS = [
    'button:has-text("Google")',
    'button:has-text("Continue with Google")',
    'button:has-text("Sign in with Google")',
    'button:has-text("Đăng nhập bằng Google")',
    'a:has-text("Google")',
    'div[role="button"]:has-text("Google")',
    '[data-testid*="google" i]',
    'button[aria-label*="Google" i]',
    'img[alt*="Google" i]',
]

LOGIN_BUTTON_SELECTORS = [
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    'button:has-text("Đăng nhập")',
    'a:has-text("Log in")',
    'a:has-text("Login")',
    'a:has-text("Sign in")',
    'a:has-text("Đăng nhập")',
]


@dataclass
class LoginSession:
    id: str
    label: str
    status: str = "starting"  # starting|browser_open|waiting_login|capturing|done|error
    message: str = ""
    email_hint: str = ""
    cookie_header: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    account_id: str | None = None


class LoginManager:
    """Manage concurrent browser-login sessions with live status for UI polling."""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()

    def get(self, session_id: str) -> LoginSession | None:
        return self._sessions.get(session_id)

    def list_active(self) -> list[dict[str, Any]]:
        return [self._public(s) for s in self._sessions.values() if s.status not in ("done", "error") or (time.time() - s.created_at) < 600]

    def _public(self, s: LoginSession) -> dict[str, Any]:
        return {
            "id": s.id,
            "label": s.label,
            "status": s.status,
            "message": s.message,
            "email_hint": s.email_hint,
            "error": s.error,
            "account_id": s.account_id,
            "cookie_count": len(s.cookies),
            "has_cookies": bool(s.cookie_header),
            "created_at": s.created_at,
            "finished_at": s.finished_at,
        }

    async def start(
        self,
        *,
        label: str = "gmail-account",
        email: str = "",
        password: str = "",
        headless: bool = False,
        timeout_sec: int = 300,
        login_url: str = "https://www.dola.com/chat/",
        daily_limit: int = 2,
        save_account: Callable | None = None,
    ) -> dict[str, Any]:
        sid = uuid.uuid4().hex[:10]
        session = LoginSession(id=sid, label=label, email_hint=email, status="starting", message="Đang khởi động browser…")
        self._sessions[sid] = session

        asyncio.create_task(
            self._run(
                session,
                email=email,
                password=password,
                headless=headless,
                timeout_sec=timeout_sec,
                login_url=login_url,
                daily_limit=daily_limit,
                save_account=save_account,
            )
        )
        return self._public(session)

    async def _run(
        self,
        session: LoginSession,
        *,
        email: str,
        password: str,
        headless: bool,
        timeout_sec: int,
        login_url: str,
        daily_limit: int,
        save_account: Callable | None,
    ) -> None:
        try:
            result = await capture_cookies_via_browser(
                label=session.label,
                email=email,
                password=password,
                headless=headless,
                timeout_sec=timeout_sec,
                login_url=login_url,
                on_status=lambda status, msg: self._set_status(session, status, msg),
            )
            if not result.get("ok"):
                session.status = "error"
                session.error = result.get("error") or "Login failed"
                session.message = session.error
                session.finished_at = time.time()
                return

            session.cookies = result.get("cookies") or {}
            session.cookie_header = result.get("cookie_header") or ""
            session.email_hint = result.get("email_hint") or email or session.email_hint
            session.status = "capturing"
            session.message = "Đã lấy session — đang lưu account…"

            if save_account:
                account = await save_account(
                    label=session.label,
                    cookies=session.cookie_header,
                    email=session.email_hint,
                    daily_limit=daily_limit,
                )
                session.account_id = (account or {}).get("id")

            session.status = "done"
            session.message = f"Login OK · {len(session.cookies)} cookies"
            session.finished_at = time.time()
        except Exception as e:
            log.exception("login session %s failed", session.id)
            session.status = "error"
            session.error = str(e)
            session.message = str(e)
            session.finished_at = time.time()

    def _set_status(self, session: LoginSession, status: str, message: str) -> None:
        session.status = status
        session.message = message
        log.info("[%s] %s — %s", session.id, status, message)


login_manager = LoginManager()


def _has_session_cookies(cookies: list[dict] | dict[str, str]) -> bool:
    if isinstance(cookies, dict):
        names = set(cookies.keys())
    else:
        names = {c.get("name", "") for c in cookies}
    return bool(names & SESSION_COOKIE_NAMES)


async def capture_cookies_via_browser(
    *,
    label: str = "",
    email: str = "",
    password: str = "",
    headless: bool = False,
    timeout_sec: int = 300,
    login_url: str = "https://www.dola.com/chat/",
    on_status: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """
    Mở browser, tự điều hướng login Gmail trên Dola, capture cookies.

    - Nếu có email/password: thử auto-fill form Google (có thể bị 2FA/CAPTCHA).
    - Nếu không: user login tay trong cửa sổ browser, app tự nhận session.

    Returns:
      {ok, cookies, cookie_header, email_hint, error}
    """

    def status(s: str, msg: str) -> None:
        if on_status:
            on_status(s, msg)
        log.info("%s: %s", s, msg)

    try:
        from playwright.async_api import async_playwright, TimeoutError as PwTimeout
    except ImportError:
        return {
            "ok": False,
            "cookies": {},
            "cookie_header": "",
            "email_hint": "",
            "error": "Chưa cài Playwright. Chạy: pip install playwright && playwright install chromium",
        }

    status("starting", "Đang cài/khởi động Chromium…")

    async with async_playwright() as p:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        try:
            browser = await p.chromium.launch(
                headless=headless,
                args=launch_args,
            )
        except Exception as e:
            # headless fallback nếu không có display
            if not headless:
                status("starting", f"Không mở được headed browser ({e}); thử headless…")
                try:
                    browser = await p.chromium.launch(headless=True, args=launch_args)
                    headless = True
                except Exception as e2:
                    return {
                        "ok": False,
                        "cookies": {},
                        "cookie_header": "",
                        "email_hint": "",
                        "error": (
                            f"Không launch được Chromium: {e2}. "
                            "Chạy: playwright install chromium. "
                            "Nếu server không có GUI, dùng VNC/X11 hoặc cài xvfb."
                        ),
                    }
            else:
                return {
                    "ok": False,
                    "cookies": {},
                    "cookie_header": "",
                    "email_hint": "",
                    "error": f"Không launch được Chromium: {e}",
                }

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="vi-VN",
        )
        # Giảm dấu hiệu automation
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await context.new_page()

        status("browser_open", "Đã mở browser — vào Dola…")
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            await browser.close()
            return {
                "ok": False,
                "cookies": {},
                "cookie_header": "",
                "email_hint": "",
                "error": f"Không mở được dola.com: {e}",
            }

        # Nếu đã login sẵn (cookie profile) — hiếm khi với context mới
        if _has_session_cookies(await context.cookies()):
            status("capturing", "Phát hiện session sẵn có")
        else:
            await _try_start_google_login(page, status)
            if email:
                await _try_fill_google_credentials(page, email, password, status)

            status(
                "waiting_login",
                "Chờ bạn đăng nhập Gmail trên cửa sổ browser… (tự lấy cookie khi xong)",
            )

            deadline = time.time() + timeout_sec
            logged_in = False
            while time.time() < deadline:
                cookies = await context.cookies()
                if _has_session_cookies(cookies):
                    # đợi thêm cookie phụ
                    await asyncio.sleep(2.5)
                    # xác nhận vẫn còn sau redirect
                    cookies = await context.cookies()
                    if _has_session_cookies(cookies):
                        logged_in = True
                        break
                # thỉnh thoảng thử click lại Google nếu user chưa bắt đầu
                if int(time.time()) % 20 == 0:
                    await _try_start_google_login(page, None)
                await asyncio.sleep(1.2)

            if not logged_in:
                await browser.close()
                return {
                    "ok": False,
                    "cookies": {},
                    "cookie_header": "",
                    "email_hint": email or label,
                    "error": (
                        f"Hết thời gian {timeout_sec}s — chưa thấy session sau login. "
                        "Hãy login Gmail xong trên cửa sổ browser rồi thử lại."
                    ),
                }

        status("capturing", "Login thành công — đang lấy Cookies/Session…")
        await asyncio.sleep(1.5)
        raw_cookies = await context.cookies()
        cookies_map = {c["name"]: c["value"] for c in raw_cookies if c.get("name")}

        # Thử lấy email từ trang
        email_hint = email or label or ""
        try:
            # một số UI hiện email trên avatar menu
            content = await page.content()
            import re

            m = re.search(r"([a-zA-Z0-9_.+-]+@gmail\.com)", content)
            if m:
                email_hint = m.group(1)
        except Exception:
            pass

        await browser.close()

    header = cookies_to_header(cookies_map)
    return {
        "ok": bool(header) and _has_session_cookies(cookies_map),
        "cookies": cookies_map,
        "cookie_header": header,
        "email_hint": email_hint,
        "error": "" if header else "Không lấy được cookie",
    }


async def _try_start_google_login(page: Any, status: Callable | None) -> None:
    """Click Log in → Google nếu thấy các nút đó."""
    try:
        # Đóng cookie banner nếu có
        for sel in [
            'button:has-text("Accept")',
            'button:has-text("Accept all")',
            'button:has-text("Đồng ý")',
            'button:has-text("Agree")',
        ]:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=2000)
                await asyncio.sleep(0.5)
                break
    except Exception:
        pass

    # Mở modal login
    for sel in LOGIN_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                if status:
                    status("browser_open", "Bấm nút Login…")
                await loc.click(timeout=3000)
                await asyncio.sleep(1.0)
                break
        except Exception:
            continue

    # Click Google
    for sel in GOOGLE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                if status:
                    status("browser_open", "Bấm Continue with Google…")
                await loc.click(timeout=3000)
                await asyncio.sleep(1.5)
                return
        except Exception:
            continue

    # Fallback: mọi element text chứa Google
    try:
        loc = page.get_by_text("Google", exact=False).first
        if await loc.count():
            await loc.click(timeout=3000)
            if status:
                status("browser_open", "Đã click Google (fallback)")
    except Exception:
        pass


async def _try_fill_google_credentials(
    page: Any,
    email: str,
    password: str,
    status: Callable | None,
) -> None:
    """Best-effort điền form Google. 2FA/CAPTCHA vẫn cần user xử lý tay."""
    try:
        # Chờ trang accounts.google.com
        for _ in range(30):
            url = page.url
            if "accounts.google.com" in url or "google.com" in url:
                break
            # popup?
            if len(page.context.pages) > 1:
                page = page.context.pages[-1]
            await asyncio.sleep(0.5)

        # Email
        email_input = page.locator('input[type="email"], input[name="identifier"]').first
        if await email_input.count():
            if status:
                status("waiting_login", f"Điền email {email}…")
            await email_input.fill(email)
            await asyncio.sleep(0.3)
            next_btn = page.locator(
                '#identifierNext, button:has-text("Next"), button:has-text("Tiếp theo")'
            ).first
            if await next_btn.count():
                await next_btn.click()
            else:
                await page.keyboard.press("Enter")
            await asyncio.sleep(2.0)

        if not password:
            if status:
                status("waiting_login", "Đã điền email — nhập password/2FA trên browser…")
            return

        # Password
        pw_input = page.locator('input[type="password"], input[name="Passwd"]').first
        for _ in range(20):
            if await pw_input.count() and await pw_input.is_visible():
                break
            await asyncio.sleep(0.5)
        if await pw_input.count() and await pw_input.is_visible():
            if status:
                status("waiting_login", "Điền password…")
            await pw_input.fill(password)
            await asyncio.sleep(0.3)
            next_btn = page.locator(
                '#passwordNext, button:has-text("Next"), button:has-text("Tiếp theo")'
            ).first
            if await next_btn.count():
                await next_btn.click()
            else:
                await page.keyboard.press("Enter")
            if status:
                status("waiting_login", "Đã submit — nếu có 2FA hãy xác nhận trên browser…")
    except Exception as e:
        log.warning("auto-fill google failed: %s", e)
        if status:
            status("waiting_login", "Auto-fill thất bại — hãy login tay trên browser…")
