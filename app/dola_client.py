"""
Dola (ByteDance Flow) live internal API client — production.

Auth: session cookies (Gmail OAuth on dola.com via browser login).
Video: Seedance 2.0 skill (VideoGeneration = 17).

Live pipeline:
  1) bootstrap (anon id / launch)
  2) thread/main/create_or_get → conversation
  3) pre_handle_v2_without_conv / pre_handle_v2 → submit skill
  4) poll query_video_gen_info + message list + artifact
  5) get_play_info → download
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("dola.client")

SESSION_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "sid_guard",
    "uid_tt",
    "uid_tt_ss",
}


def parse_cookie_header(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}

    if raw.startswith("["):
        try:
            items = json.loads(raw)
            out: dict[str, str] = {}
            for it in items:
                if isinstance(it, dict) and it.get("name"):
                    out[str(it["name"])] = str(it.get("value", ""))
            return out
        except json.JSONDecodeError:
            pass

    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items()}
        except json.JSONDecodeError:
            pass

    if "\t" in raw and ("TRUE" in raw or "FALSE" in raw):
        out = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5]] = parts[6]
        if out:
            return out

    out = {}
    for part in re.split(r";\s*", raw):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _new_local_msg_id() -> str:
    return f"local_{uuid.uuid4().hex}"


class DolaClient:
    def __init__(
        self,
        cookies: str | dict[str, str],
        extra_headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> None:
        if isinstance(cookies, str):
            self.cookies = parse_cookie_header(cookies)
        else:
            self.cookies = dict(cookies)
        self.extra_headers = extra_headers or {}
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self.app_id = settings.app_id
        self.bot_id = settings.bot_id
        self._client: httpx.AsyncClient | None = None
        self._web_id: str = ""
        self._device_id: str = self.cookies.get("ttwid") or self.cookies.get("odin_tt") or ""
        self._bootstrapped = False

    async def __aenter__(self) -> "DolaClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_sec, connect=25.0),
            follow_redirects=True,
            headers=self._default_headers(),
            cookies=self.cookies,
            http2=False,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _default_headers(self) -> dict[str, str]:
        csrf = (
            self.cookies.get("tt_csrf_token")
            or self.cookies.get("passport_csrf_token")
            or self.cookies.get("passport_csrf_token_default")
            or ""
        )
        h = {
            "User-Agent": settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/chat/create-video",
            "Content-Type": "application/json",
            "agw-js-conv": "str",
            "x-flow-app-id": self.app_id,
            "x-app-id": self.app_id,
        }
        if csrf:
            h["x-tt-passport-csrf-token"] = csrf
            h["X-CSRFToken"] = csrf
        h.update(self.extra_headers)
        return h

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("Client not started — use async with DolaClient(...)")
        return self._client

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _common_params(self) -> dict[str, str]:
        fp = self.cookies.get("s_v_web_id") or self.cookies.get("ttwid") or ""
        device_id = (
            self._device_id
            or self.cookies.get("ttwid")
            or ""
        )
        # numeric device id preferred (from launch); fall back to empty
        return {
            "aid": self.app_id,
            "real_aid": self.app_id,
            "device_id": str(device_id)[:40] if device_id else "",
            "device_platform": "web",
            "doubao_device_platform": "web",
            "doubao_pc_version": settings.pc_version,
            "fp": fp,
            "language": "en",
            "pkg_type": "release_version",
            "region": settings.region,
            "samantha_web": "1",
            "use_olympus_account": "1",
            "version_code": settings.version_code,
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        headers: dict | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        q = {**self._common_params(), **(params or {})}
        try:
            resp = await self.client.request(
                method,
                self._url(path),
                json=json_body,
                params=q,
                headers=headers,
            )
        except httpx.HTTPError as e:
            return {
                "_ok": False,
                "_http_status": 0,
                "code": -1,
                "msg": f"network error: {e}",
                "message": str(e),
            }

        # merge set-cookie into local map
        try:
            for c in resp.cookies.jar:
                self.cookies[c.name] = c.value
                self.client.cookies.set(c.name, c.value, domain=c.domain or None)
        except Exception:
            pass

        text = resp.text
        try:
            data = resp.json()
        except Exception:
            data = {"_raw": text[:4000], "_status": resp.status_code}

        if isinstance(data, dict):
            data.setdefault("_http_status", resp.status_code)
            data.setdefault("_ok", resp.is_success)
            return data
        return {"data": data, "_http_status": resp.status_code, "_ok": resp.is_success}

    # ── bootstrap ─────────────────────────────────────────────

    async def bootstrap(self) -> dict[str, Any]:
        if self._bootstrapped:
            return {"ok": True, "cached": True}

        anon = await self.request("POST", "/alice/user/get_web_anon_id", json_body={})
        self._web_id = str(anon.get("web_id") or anon.get("uid") or self._web_id or "")

        launch = await self.request(
            "POST",
            "/alice/basic/launch",
            json_body={},
        )
        core = await self.request(
            "POST",
            "/alice/user/launch/core",
            json_body={"user_landing_tag": "", "is_first_cold_start": False},
        )
        im = await self.request(
            "POST",
            "/alice/im/launch",
            json_body={"im_config": True},
        )

        self._bootstrapped = True
        return {"ok": True, "anon": anon, "launch": launch, "core": core, "im": im}

    # ── account ───────────────────────────────────────────────

    async def get_account_info(self) -> dict[str, Any]:
        return await self.request(
            "GET",
            "/passport/account/info/v2/",
            params={"aid": self.app_id},
        )

    async def validate_session(self) -> dict[str, Any]:
        if settings.demo_mode:
            return {
                "ok": True,
                "email": "demo@example.com",
                "user_id": "demo",
                "error": "",
                "raw": {"demo": True},
            }

        try:
            await self.bootstrap()
        except Exception as e:
            log.warning("bootstrap failed: %s", e)

        # Try primary + alt app ids (web uses 495671)
        infos: list[dict[str, Any]] = []
        for aid in [self.app_id, settings.app_id_alt]:
            prev = self.app_id
            self.app_id = aid
            info = await self.get_account_info()
            infos.append(info)
            data = info.get("data") if isinstance(info.get("data"), dict) else {}
            user_id = data.get("user_id") or data.get("userId")
            if user_id and str(user_id) not in ("0", "", "None"):
                email = (
                    data.get("email")
                    or data.get("user_name")
                    or data.get("screen_name")
                    or data.get("name")
                    or data.get("nickname")
                    or ""
                )
                if data.get("device_id"):
                    self._device_id = str(data["device_id"])
                return {
                    "ok": True,
                    "email": str(email),
                    "user_id": str(user_id),
                    "error": "",
                    "raw": info,
                }
            # message success + session_key also means logged in (partial payload)
            if info.get("message") == "success" and data.get("session_key"):
                self.app_id = prev if prev else aid
                # keep working aid that returned richer data if possible
                if data.get("email") or data.get("user_id"):
                    self.app_id = aid
                return {
                    "ok": True,
                    "email": str(data.get("email") or ""),
                    "user_id": str(data.get("user_id") or data.get("session_key") or ""),
                    "error": "",
                    "raw": info,
                }
            self.app_id = prev

        # fallback: can we create thread? (auth-gated success)
        th = await self.request(
            "POST",
            "/samantha/thread/main/create_or_get",
            json_body={},
        )
        if th.get("code") in (0, "0") and (
            self._dig(th, "thread.conversation.conversation_id", "data.thread_info.conversation.conversation_id")
        ):
            return {
                "ok": True,
                "email": "",
                "user_id": "",
                "error": "",
                "raw": {"thread": th, "accounts": infos},
            }

        info = infos[0] if infos else {}
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        err = (
            (data or {}).get("description")
            or "session invalid"
        )
        return {
            "ok": False,
            "email": "",
            "user_id": "",
            "error": str(err),
            "raw": {"accounts": infos, "thread": th},
        }

    # ── conversation ──────────────────────────────────────────

    async def get_or_create_main_conversation(self) -> dict[str, Any]:
        """Return {conversation_id, section_id, raw}."""
        # Prefer thread main
        main = await self.request("POST", "/samantha/thread/main/create_or_get", json_body={})
        conv_id = self._dig(
            main,
            "data.conversation_id",
            "data.conversation.conversation_id",
            "conversation_id",
            "data.thread.conversation_id",
        )
        section_id = self._dig(
            main,
            "data.section_id",
            "data.conversation.last_section_id",
            "data.last_section_id",
            "section_id",
        )

        if not conv_id:
            # list existing
            listing = await self.request(
                "POST",
                "/samantha/im/conversation/list",
                json_body={"cursor": "0", "batch_size": 20, "conversation_types": [3]},
            )
            conv_id = self._first_conv_id(listing)
            section_id = section_id or "0"

        if not conv_id:
            listing2 = await self.request(
                "POST",
                "/alice/conversation/list",
                json_body={"cursor": 0, "count": 20},
            )
            conv_id = self._first_conv_id(listing2)

        return {
            "conversation_id": str(conv_id or "0"),
            "section_id": str(section_id or "0"),
            "raw": main,
        }

    def _first_conv_id(self, payload: dict) -> str:
        blob = json.dumps(payload, ensure_ascii=False)
        # prefer long numeric ids
        ids = re.findall(r'"conversation_id"\s*:\s*"?(\d{8,})"?', blob)
        return ids[0] if ids else ""

    # ── video generation ──────────────────────────────────────

    def _build_content_obj(
        self,
        prompt: str,
        *,
        mode: str,
        ratio: str,
        duration: int,
        resolution: str,
        model: str,
        image_url: str,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {
            "text": prompt,
            "skill_type": 17,
            "skill_key": "video_generation",
            "model": model,
            "model_key": model,
            "video_option": {
                "ratio": ratio,
                "aspect_ratio": ratio,
                "duration": int(duration),
                "resolution": resolution,
                "model": model,
                "model_name": "Seedance 2.0",
                "seedance_model": "seedance_2.0",
            },
            "creation_option": {
                "type": "video",
                "mode": mode,
                "ratio": ratio,
                "duration": int(duration),
                "resolution": resolution,
            },
            "ext": {
                "skill_type": "17",
                "input_skill": "video_generation",
                "chat_scene": "create-video",
            },
        }
        if image_url and mode in ("image2video", "ref2video"):
            content["attachments"] = [
                {"type": "image", "url": image_url, "image_url": image_url}
            ]
            content["ref_images"] = [image_url]
            content["image_list"] = [image_url]
        return content

    def _build_uplink(
        self,
        prompt: str,
        *,
        mode: str,
        ratio: str,
        duration: int,
        resolution: str,
        model: str,
        image_url: str,
        conversation_id: str,
        section_id: str,
        local_message_id: str,
    ) -> dict[str, Any]:
        """
        Server-side thrift/binding requires at least:
          entity_type, entity_content, identifier
        (verified live: missing fields → HTTP 400 binding errors)
        """
        content_obj = self._build_content_obj(
            prompt,
            mode=mode,
            ratio=ratio,
            duration=duration,
            resolution=resolution,
            model=model,
            image_url=image_url,
        )
        return {
            # required by /alice/message/pre_handle_v2*
            "entity_type": 1,
            "identifier": local_message_id,
            "entity_content": content_obj,
            # chat context
            "conversation_id": conversation_id or "0",
            "section_id": section_id or "0",
            "bot_id": self.bot_id,
            "local_message_id": local_message_id,
            "content_type": 2001,
            "content": json.dumps(content_obj, ensure_ascii=False),
            "content_obj": content_obj,
            "ext": {
                "skill_type": "17",
                "skill_id": "17",
                "use_skill": "true",
                "input_skill": "video_generation",
                "chat_scene": "create-video",
            },
        }

    def _build_completion_body(
        self,
        prompt: str,
        *,
        model: str,
        duration: int,
        ratio: str = "16:9",
        mode: str = "text2video",
        image_url: str = "",
        image_uri: str = "",
        image_key: str = "",
    ) -> dict[str, Any]:
        """Exact web client payload captured from dola.com/chat/create-video."""
        local_conv = f"local_{int(time.time() * 1000) % 10**16}"
        local_msg = str(uuid.uuid4())
        block_id = str(uuid.uuid4())
        is_i2v = mode in ("image2video", "ref2video") and (image_url or image_uri or image_key)
        if is_i2v:
            text = (
                prompt
                if prompt.lower().startswith("generated video")
                else f"Generated video from image: {prompt}"
            )
        else:
            text = (
                prompt
                if prompt.lower().startswith("generated video")
                else f"Generated video: {prompt}"
            )
        # skill pack model id: seedance_v2.0
        if model in ("seedance_2.0", "seedance2", "Seedance 2.0"):
            model = "seedance_v2.0"
        ability: dict[str, Any] = {"model": model, "duration": int(duration)}
        if ratio:
            ability["ratio"] = ratio
        if is_i2v:
            # common creation fields for image reference
            ref: dict[str, Any] = {}
            if image_url:
                ref["url"] = image_url
                ref["image_url"] = image_url
            if image_uri:
                ref["uri"] = image_uri
                ref["image_uri"] = image_uri
            if image_key:
                ref["key"] = image_key
            ability["images"] = [ref]
            ability["image_list"] = [ref]
            ability["ref_images"] = [image_url or image_uri]
            ability["input_image"] = image_url or image_uri

        content_blocks: list[dict[str, Any]] = [
            {
                "block_type": 10000,
                "content": {
                    "text_block": {
                        "text": text,
                        "icon_url": "",
                        "icon_url_dark": "",
                        "summary": "",
                    },
                    "pc_event_block": "",
                },
                "block_id": block_id,
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
            }
        ]
        # attach image content block for i2v
        if is_i2v and (image_url or image_uri):
            img_url = image_url or image_uri
            content_blocks.append(
                {
                    "block_type": 10006,
                    "content": {
                        "image_block": {
                            "image_ori": {"url": img_url},
                            "image_thumb": {"url": img_url},
                            "url": img_url,
                            "uri": image_uri or "",
                            "key": image_key or "",
                        }
                    },
                    "block_id": str(uuid.uuid4()),
                    "parent_id": "",
                    "meta_info": [],
                    "append_fields": [],
                }
            )

        now_ms = int(time.time() * 1000)
        fp = self.cookies.get("s_v_web_id") or ""
        user_context: list[dict[str, Any]] = []
        if is_i2v and (image_url or image_uri):
            user_context.append(
                {
                    "type": "image",
                    "url": image_url or image_uri,
                    "uri": image_uri or image_url,
                    "key": image_key or "",
                }
            )
        return {
            "client_meta": {
                "local_conversation_id": local_conv,
                "conversation_id": "",
                "bot_id": self.bot_id,
                "last_section_id": "",
                "last_message_index": None,
            },
            "messages": [
                {
                    "local_message_id": local_msg,
                    "content_block": content_blocks,
                    "message_status": 0,
                }
            ],
            "option": {
                "send_message_scene": "create-video" if is_i2v else "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "tts_switch": False,
                "need_deep_think": 0,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "is_select_text": False,
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": True,
                "conversation_init_option": {"need_ack_conversation": True},
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "is_ai_playground": False,
                "is_old_user": False,
                "recovery_option": {
                    "is_recovery": False,
                    "req_create_time_sec": int(time.time()),
                    "append_sse_event_scene": 0,
                },
                "message_storage_type": 0,
            },
            "chat_ability": {
                "ability_type": 17,
                "ability_param": json.dumps(ability, separators=(",", ":")),
            },
            "user_context": user_context,
            "ext": {
                "answer_with_suggest": "0",
                "fp": fp,
                "sub_conv_firstmet_type": "1",
                "collection_id": "",
                "conversation_init_option": '{"need_ack_conversation":true}',
                "commerce_credit_config_enable": "0",
            },
        }

    async def resolve_image_ref(
        self,
        *,
        image_url: str = "",
        image_path: str = "",
    ) -> dict[str, str]:
        """
        Return {image_url, image_uri, image_key} for i2v.
        Prefers public URL; local files get a best-effort Dola upload, else file:// note.
        """
        out = {"image_url": image_url or "", "image_uri": "", "image_key": ""}
        if image_url and image_url.startswith(("http://", "https://", "data:")):
            return out
        if not image_path:
            return out
        from pathlib import Path

        p = Path(image_path)
        if not p.exists():
            return out
        # try Dola upload best-effort
        uploaded = await self.upload_local_image(str(p))
        if uploaded.get("url") or uploaded.get("uri"):
            out["image_url"] = str(uploaded.get("url") or uploaded.get("uri") or "")
            out["image_uri"] = str(uploaded.get("uri") or "")
            out["image_key"] = str(uploaded.get("key") or "")
            return out
        # fallback: data URI (some endpoints accept; Dola may reject large)
        import base64
        import mimetypes

        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        # cap ~1.5MB raw to avoid huge SSE payload
        if p.stat().st_size <= 1_500_000:
            out["image_url"] = f"data:{mime};base64,{b64}"
        return out

    async def upload_local_image(self, path: str) -> dict[str, Any]:
        """Best-effort upload local image to Dola; returns {url,uri,key,raw}."""
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return {"error": "file not found"}
        data = p.read_bytes()
        name = p.name
        # Strategy: multipart to known upload endpoints
        endpoints = [
            ("/samantha/pages/upload_image", {"file": (name, data)}),
            ("/alice/upload/file", {"file": (name, data)}),
        ]
        for path_ep, _ in endpoints:
            try:
                # httpx multipart via raw request
                files = {"file": (name, data, "application/octet-stream")}
                resp = await self.client.post(
                    self._url(path_ep),
                    params=self._common_params(),
                    files=files,
                    headers={
                        k: v
                        for k, v in self._default_headers().items()
                        if k.lower() != "content-type"
                    },
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {"_raw": resp.text[:500], "_http_status": resp.status_code}
                blob = json.dumps(body, ensure_ascii=False)
                url = self._extract_any_url(blob)
                uri = ""
                m = re.search(r'"(?:uri|image_uri|tos_uri)"\s*:\s*"([^"]+)"', blob)
                if m:
                    uri = m.group(1)
                key = ""
                m2 = re.search(r'"(?:key|image_key|resource_id)"\s*:\s*"([^"]+)"', blob)
                if m2:
                    key = m2.group(1)
                if url or uri or (isinstance(body, dict) and body.get("code") in (0, "0")):
                    if url or uri:
                        return {"url": url, "uri": uri, "key": key, "raw": body}
            except Exception as e:
                log.warning("upload %s failed: %s", path_ep, e)
        return {"error": "upload failed", "url": "", "uri": "", "key": ""}

    @staticmethod
    def _extract_any_url(blob: str) -> str:
        urls = re.findall(r'https?://[^"\s\\]+', blob)
        for u in urls:
            if any(x in u for x in ("image", "tos-", "byteimg", "cdn", "upload")):
                return u.rstrip("\\")
        return urls[0].rstrip("\\") if urls else ""

    async def create_video(
        self,
        prompt: str,
        *,
        mode: str = "text2video",
        ratio: str = "16:9",
        duration: int = 5,
        resolution: str = "720p",
        model: str = "seedance_v2.0",
        image_url: str = "",
        image_path: str = "",
        custom_body: dict | None = None,
    ) -> dict[str, Any]:
        if settings.demo_mode:
            tid = f"demo_{uuid.uuid4().hex[:10]}"
            return {
                "ok": True,
                "conversation_id": f"conv_{tid}",
                "message_id": f"msg_{tid}",
                "task_id": tid,
                "error": "",
                "raw": {"demo": True},
                "request_body": {"prompt": prompt, "mode": mode, "image_url": image_url},
            }

        await self.bootstrap()
        # Enrich device_id from profile if possible
        try:
            prof = await self.request("POST", "/alice/profile/self_brief", json_body={})
            did = self._dig(prof, "data.device_id", "device_id")
            if did:
                self._device_id = str(did)
        except Exception:
            pass

        img_ref = {"image_url": image_url or "", "image_uri": "", "image_key": ""}
        if mode in ("image2video", "ref2video"):
            img_ref = await self.resolve_image_ref(
                image_url=image_url, image_path=image_path
            )

        body = custom_body or self._build_completion_body(
            prompt,
            model=model or settings.default_model,
            duration=duration,
            ratio=ratio,
            mode=mode,
            image_url=img_ref.get("image_url") or image_url,
            image_uri=img_ref.get("image_uri") or "",
            image_key=img_ref.get("image_key") or "",
        )
        _ = resolution

        sse = await self._chat_completion_sse(body)
        conv_id = (
            self._dig(
                sse,
                "conversation_id",
                "data.conversation_id",
                "data.ack_client_meta.conversation_id",
            )
            or ""
        )
        msg_id = (
            self._dig(sse, "message_id", "data.message_id", "question_id") or ""
        )
        brief = str(sse.get("brief") or "")
        ok = bool(sse.get("_ok")) and not self._is_auth_error(sse)
        # success if SSE accepted generation
        if "Seedance" in brief or "seedance" in brief.lower() or "Generating" in brief:
            ok = True
        if sse.get("code") in (0, "0", None) and conv_id:
            ok = True
        if sse.get("code") in (710012001, "710012001", 710020202, "710020202"):
            ok = False

        err = ""
        if not ok:
            err = str(sse.get("msg") or sse.get("message") or "chat/completion failed")

        quota_info = parse_quota_from_text(brief + "\n" + str(sse.get("_raw_sse") or ""))
        # no-quota errors from stream
        if not ok and _NO_QUOTA_RE.search(err or ""):
            quota_info = {
                "found": True,
                "quota_left": 0,
                "has_quota": False,
                "note": err,
            }

        return {
            "ok": ok,
            "conversation_id": str(conv_id or ""),
            "message_id": str(msg_id or ""),
            "task_id": str(msg_id or conv_id or uuid.uuid4().hex),
            "local_message_id": body.get("messages", [{}])[0].get("local_message_id", ""),
            "error": err,
            "brief": brief,
            "quota": quota_info,
            "raw": sse,
            "request_body": body,
        }

    async def check_quota(self) -> dict[str, Any]:
        """
        Estimate remaining daily video quota without burning a full gen when possible.

        Strategy:
          1) rate_limit probe
          2) skill pack (model availability)
          3) lightweight chat completion dry-run is expensive — skip
          4) return structured result for DB persistence
        """
        if settings.demo_mode:
            return {
                "ok": True,
                "has_quota": True,
                "quota_left": 2,
                "quota_total": 2,
                "note": "demo quota",
                "raw": {"demo": True},
            }

        await self.bootstrap()
        session = await self.validate_session()
        if not session.get("ok"):
            return {
                "ok": False,
                "has_quota": False,
                "quota_left": 0,
                "quota_total": None,
                "note": f"session invalid: {session.get('error')}",
                "raw": session,
            }

        raw: dict[str, Any] = {"session": {"email": session.get("email")}}

        # rate_limit
        rl = await self.request("POST", "/alice/message/rate_limit", json_body={})
        raw["rate_limit"] = {
            "code": rl.get("code"),
            "msg": rl.get("msg") or rl.get("message"),
            "data": rl.get("data"),
        }
        blob = json.dumps(rl, ensure_ascii=False)
        parsed = parse_quota_from_text(blob)
        left = parsed.get("quota_left")
        # try numeric fields
        data = rl.get("data") if isinstance(rl.get("data"), dict) else {}
        if data.get("is_limit") is True:
            left = 0
            parsed = {
                "found": True,
                "quota_left": 0,
                "has_quota": False,
                "note": str(data.get("limit_tips") or "rate_limit.is_limit=true"),
            }
        for k in (
            "remain",
            "remaining",
            "left",
            "quota",
            "free_count",
            "point",
            "points",
            "credit",
            "credits",
            "daily_remain",
            "remain_count",
        ):
            if data.get(k) is not None:
                try:
                    left = int(data[k])
                    parsed = {
                        "found": True,
                        "quota_left": left,
                        "has_quota": left > 0,
                        "note": f"rate_limit.{k}={left}",
                    }
                    break
                except (TypeError, ValueError):
                    pass

        pack = await self.request(
            "POST",
            "/samantha/skill/pack",
            json_body={"skill_type": 17, "condition": {}},
        )
        raw["skill_pack_code"] = pack.get("code")

        has_quota = True
        note = parsed.get("note") or "quota unknown — assume available until gen fails"
        if parsed.get("found"):
            has_quota = bool(parsed.get("has_quota"))
            note = str(parsed.get("note") or note)
            left = parsed.get("quota_left")

        # If rate_limit hard-fails with limit wording
        msg = str(rl.get("msg") or rl.get("message") or "")
        if _NO_QUOTA_RE.search(msg):
            has_quota = False
            left = 0
            note = msg

        return {
            "ok": True,
            "has_quota": has_quota,
            "quota_left": left,
            "quota_total": None,
            "note": note,
            "email": session.get("email") or "",
            "user_id": session.get("user_id") or "",
            "raw": raw,
        }


    async def query_video_gen_info(self, body: dict | None = None) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/samantha/video/query_video_gen_info",
            json_body=body or {},
        )

    async def get_play_info(self, vid: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/samantha/video/get_play_info",
            json_body={"vid": vid},
        )

    async def list_messages(self, conversation_id: str) -> dict[str, Any]:
        # Real working endpoint (verified live): index_list
        idx = await self.request(
            "POST",
            "/alice/message/index_list",
            json_body={
                "conversation_id": conversation_id,
                "message_index_list": list(range(0, 40)),
            },
        )
        if idx.get("code") in (0, "0"):
            return idx
        return await self.request(
            "POST",
            "/alice/message/list/v2",
            json_body={
                "conversation_id": conversation_id,
                "start_index": 0,
                "batch_size": 30,
                "is_reverse": True,
            },
        )

    async def get_artifact(self, conversation_id: str, message_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"conversation_id": conversation_id}
        if message_id:
            body["message_id"] = message_id
        return await self.request(
            "POST",
            "/samantha/creation/artifact/get",
            json_body=body,
        )

    async def poll_video_status(
        self,
        *,
        task_id: str = "",
        conversation_id: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        if settings.demo_mode:
            return {
                "status": "success",
                "progress": 100,
                "video_url": "https://example.com/demo-seedance.mp4",
                "vid": task_id or "demo_vid",
                "error": "",
                "raw": {"demo": True},
            }

        body: dict[str, Any] = {}
        if task_id:
            body.update({"task_id": task_id, "video_task_id": task_id})
        if conversation_id:
            body["conversation_id"] = conversation_id
        if message_id:
            body["message_id"] = message_id

        info = await self.query_video_gen_info(body)
        msgs = await self.list_messages(conversation_id) if conversation_id else {}
        artifact = (
            await self.get_artifact(conversation_id, message_id) if conversation_id else {}
        )

        if self._is_auth_error(info) and self._is_auth_error(msgs):
            return {
                "status": "failed",
                "progress": 0,
                "video_url": "",
                "vid": "",
                "error": "Session expired",
                "raw": {"info": info},
            }

        combined = {
            "info": info,
            "messages": msgs,
            "artifact": artifact,
        }
        blob = json.dumps(combined, ensure_ascii=False)

        # error signals
        if re.search(r'"error_code"\s*:\s*[1-9]', blob) and "safety" in blob.lower():
            # may still be generating elsewhere
            pass

        video_url = self._extract_video_url(blob)
        vid = self._extract_vid(blob)

        status = "running"
        progress = 35.0
        error = ""

        data = info.get("data") if isinstance(info.get("data"), dict) else info
        if isinstance(data, dict):
            st = str(
                data.get("status")
                or data.get("state")
                or data.get("gen_status")
                or data.get("task_status")
                or ""
            ).lower()
            if st in ("success", "succeed", "done", "finished", "complete", "completed"):
                status = "success"
                progress = 100
            elif st in ("fail", "failed", "error", "cancelled", "canceled"):
                status = "failed"
                error = str(data.get("error") or data.get("msg") or data.get("message") or "failed")
            elif st in ("pending", "queued", "waiting", "init"):
                status = "pending"
                progress = float(data.get("progress") or 10)
            elif st:
                status = "running"
                try:
                    progress = float(data.get("progress") or 40)
                except (TypeError, ValueError):
                    progress = 40
            if data.get("vid") or data.get("video_id"):
                vid = str(data.get("vid") or data.get("video_id"))
            if data.get("video_url") or data.get("play_url"):
                video_url = str(data.get("video_url") or data.get("play_url"))

        if vid and not video_url:
            play = await self.get_play_info(vid)
            video_url = self._extract_video_url(json.dumps(play, ensure_ascii=False)) or video_url
            if isinstance(play.get("data"), dict):
                video_url = video_url or str(
                    play["data"].get("play_url")
                    or play["data"].get("main_url")
                    or play["data"].get("url")
                    or ""
                )
            combined["play"] = play

        if video_url:
            status = "success"
            progress = 100
        elif re.search(r"Your video is ready|video is ready", blob, re.I):
            # message arrived but URL parse failed — still mark nearly done
            status = "running"
            progress = max(progress, 90)

        # heuristic: still generating keywords
        if status != "success" and status != "failed":
            if re.search(r"(generating|processing|in_progress|running|1-3 minutes)", blob, re.I):
                status = "running"
                progress = max(progress, 50)

        return {
            "status": status,
            "progress": progress,
            "video_url": video_url,
            "vid": vid,
            "error": error,
            "raw": combined,
        }


    async def download_video(self, url: str, dest_path: str) -> str:
        if settings.demo_mode:
            with open(dest_path, "wb") as f:
                f.write(b"DEMO_VIDEO_PLACEHOLDER")
            return dest_path

        # signed CDN URLs usually don't need cookies; still send them
        async with self.client.stream("GET", url, headers={"Referer": self.base_url + "/"}) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
        return dest_path

    async def send_raw(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        return await self.request(method.upper(), path, json_body=body)

    async def _chat_completion_sse(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /chat/completion — SSE stream (verified live video create path)."""
        q = self._common_params()
        try:
            async with self.client.stream(
                "POST",
                self._url("/chat/completion"),
                json=body,
                params=q,
                headers={"Accept": "text/event-stream, application/json"},
            ) as resp:
                text_parts: list[str] = []
                events: list[dict[str, Any]] = []
                conversation_id = ""
                section_id = ""
                message_id = ""
                brief = ""
                err_code = None
                err_msg = ""
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    text_parts.append(line)
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "{}":
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        events.append({"_raw": raw[:500]})
                        continue
                    events.append(ev)
                    if isinstance(ev, dict):
                        if "error_code" in ev:
                            err_code = ev.get("error_code")
                            err_msg = str(ev.get("error_msg") or "")
                        ack = ev.get("ack_client_meta") or {}
                        if ack.get("conversation_id"):
                            conversation_id = str(ack["conversation_id"])
                        if ack.get("section_id"):
                            section_id = str(ack["section_id"])
                        for qitem in ev.get("query_list") or []:
                            if qitem.get("question_id"):
                                message_id = str(qitem["question_id"])
                        msg = ev.get("message")
                        if isinstance(msg, dict) and msg.get("message_id"):
                            # keep user question id prefer; reply is also useful
                            if not message_id:
                                message_id = str(msg["message_id"])
                        mfa = ev.get("msg_finish_attr") or {}
                        if mfa.get("brief"):
                            brief = str(mfa["brief"])
                joined = "\n".join(text_parts)
                code = (
                    err_code
                    if err_code is not None
                    else (0 if resp.status_code < 400 else resp.status_code)
                )
                ok = (
                    resp.status_code < 400
                    and err_code not in (710012001, "710012001", 710020202, "710020202")
                    and (bool(conversation_id) or "Seedance" in brief or "Generating" in brief)
                )
                return {
                    "code": code if not ok else 0,
                    "msg": err_msg or ("ok" if ok else "stream error"),
                    "message": err_msg,
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "section_id": section_id,
                    "brief": brief,
                    "data": {
                        "events": events[:30],
                        "ack_client_meta": {
                            "conversation_id": conversation_id,
                            "section_id": section_id,
                        },
                    },
                    "_http_status": resp.status_code,
                    "_ok": ok,
                    "_raw_sse": joined[:8000],
                }
        except httpx.HTTPError as e:
            return {
                "code": -1,
                "msg": str(e),
                "_http_status": 0,
                "_ok": False,
            }


    # ── helpers ───────────────────────────────────────────────

    @staticmethod
    def _extract_video_url(blob: str) -> str:
        # Prefer Dola / ByteDance VOD hosts (verified live download)
        patterns = [
            r'https?://v16-dola\.dola\.com/[^"\s\\]+',
            r'https?://vod-urls[^"\s\\]+',
            r'https?://[^"\s\\]+?\.(?:mp4|m3u8|mov)(?:\?[^"\s\\]*)?',
            r'https?://[^"\s\\]*(?:bytevcloud|vlabvod|/video/tos/)[^"\s\\]+',
        ]
        urls: list[str] = []
        for pat in patterns:
            urls.extend(re.findall(pat, blob, flags=re.I))
        if not urls:
            return ""
        # pick longest (usually fully signed)
        u = max(urls, key=len)
        try:
            u = u.encode().decode("unicode_escape")
        except Exception:
            pass
        u = u.rstrip("\\").rstrip(",")
        if u.startswith("http://"):
            u = "https://" + u[len("http://") :]
        return u

    @staticmethod
    def _extract_vid(blob: str) -> str:
        m = re.search(r'"vid"\s*:\s*"([^"]+)"', blob)
        if m:
            return m.group(1)
        m = re.search(r'"video_id"\s*:\s*"([^"]+)"', blob)
        return m.group(1) if m else ""

    @staticmethod
    def _is_auth_error(resp: dict) -> bool:
        if not isinstance(resp, dict):
            return False
        code = resp.get("code")
        msg = str(resp.get("msg") or resp.get("message") or "").lower()
        if code in (710012001, "710012001", 13, "13"):
            return True
        if any(k in msg for k in ("session expired", "login invalid", "sign in again", "not login")):
            return True
        data = resp.get("data")
        if isinstance(data, dict) and data.get("error_code") == 13:
            return True
        return False

    @staticmethod
    def _is_hard_fail(resp: dict) -> bool:
        if not isinstance(resp, dict):
            return True
        code = resp.get("code")
        if code is None:
            # no code — check http
            st = resp.get("_http_status")
            if st and int(st) >= 400:
                return True
            return False
        if code in (0, "0"):
            return False
        return True

    @staticmethod
    def _dig(obj: Any, *paths: str) -> Any:
        for path in paths:
            cur = obj
            ok = True
            for part in path.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
        return None


# ── quota helpers (module-level) ──────────────────────────────

_POINTS_LEFT_RE = re.compile(
    r"(?:you have|còn)\s+(\d+)\s+(?:point|points|lượt|credit)",
    re.I,
)
_NO_QUOTA_RE = re.compile(
    r"(no (?:more )?points|out of points|quota|limit reached|hết lượt|không còn|0 point)",
    re.I,
)


def parse_quota_from_text(text: str) -> dict[str, Any]:
    """Parse free daily points from Dola SSE / chat text."""
    if not text:
        return {"found": False}
    m = _POINTS_LEFT_RE.search(text)
    if m:
        left = int(m.group(1))
        return {
            "found": True,
            "quota_left": left,
            "has_quota": left > 0,
            "note": m.group(0),
        }
    if _NO_QUOTA_RE.search(text):
        return {
            "found": True,
            "quota_left": 0,
            "has_quota": False,
            "note": "no quota / limit",
        }
    if re.search(r"seedance|generating video|your video is ready", text, re.I):
        # generation started — at least had quota at submit time; left unknown
        return {
            "found": False,
            "has_quota": True,
            "note": "generation accepted (quota was available)",
        }
    return {"found": False}


async def validate_cookies(cookies: str, headers: dict | None = None) -> dict[str, Any]:
    async with DolaClient(cookies, extra_headers=headers) as client:
        return await client.validate_session()


async def check_account_quota(cookies: str, headers: dict | None = None) -> dict[str, Any]:
    async with DolaClient(cookies, extra_headers=headers) as client:
        return await client.check_quota()
