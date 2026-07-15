# Dola Seedance 2 — Multi-Account Batch

Ứng dụng local để **quản lý nhiều tài khoản Dola AI** (login Gmail) và **tạo video Seedance 2.0 hàng loạt** qua **API internal** của `dola.com` (ByteDance Flow / Cici).

## Tính năng

- Multi-account: thêm session cookie sau khi login Gmail trên Dola
- Browser Login: mở Chromium để login tay → auto capture cookies (Playwright)
- Batch queue: nhiều prompt, auto phân phối theo account còn quota
- Round-robin / gán account cụ thể
- Poll trạng thái video + download `.mp4` về máy
- Escape hatch `POST /api/raw` nếu payload internal đổi

## Cài đặt

```bash
cd dola-seedance-batch
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Tuỳ chọn — browser login Gmail
playwright install chromium
```

## Chạy LIVE production

```bash
cd dola-seedance-batch
chmod +x run-prod.sh
./run-prod.sh
```

Hoặc:

```bash
source .venv/bin/activate
# .env đã set DOLA_DEMO_MODE=false + DOLA_ENV=production
python -m uvicorn app.main:app --host 0.0.0.0 --port 8787 --workers 1
```

Docker:

```bash
docker compose up -d --build
```

Mở: <http://localhost:8787> — badge **● LIVE PRODUCTION** phải hiện xanh.

> **Bắt buộc `--workers 1`**: batch worker chạy in-process.

### Demo (dev only)

```bash
DOLA_DEMO_MODE=true DOLA_ENV=development python -m uvicorn app.main:app --port 8787
```

## Thêm tài khoản (Gmail) — tự login, tự lấy session

**Không cần copy cookie thủ công.**

1. Trên UI bấm **Đăng nhập Gmail (tự lấy session)**
2. (Tuỳ chọn) điền Gmail + password để app auto-fill form Google
3. App mở Chromium → vào dola.com → tự bấm Google login
4. Bạn chỉ xác nhận Gmail / 2FA trên cửa sổ browser
5. App **tự capture cookies/session** và lưu account

Cần: `playwright install chromium` + máy có GUI (hoặc VNC/X11).

### Fallback — dán cookie

Mở **Nâng cao: dán cookie thủ công** trên UI nếu browser login không chạy được (server headless không display).

## Tạo video hàng loạt

1. Thêm ≥1 account hợp lệ
2. Dán danh sách prompt (mỗi dòng 1 video)
3. Chọn ratio / duration / resolution
4. **Đưa vào hàng đợi** — worker tự assign account, gọi API, poll, download

File video: `downloads/<job_id>.mp4`

## API nội bộ chính (Dola)

| Endpoint | Mục đích |
|----------|----------|
| `GET /passport/account/info/v2/` | Kiểm tra session |
| `POST /alice/message/pre_handle_v2` | Gửi skill video generation |
| `POST /chat/completion` | Fallback chat completion |
| `POST /samantha/video/query_video_gen_info` | Poll trạng thái gen |
| `POST /samantha/video/get_play_info` | Lấy URL phát video |
| `POST /samantha/creation/artifact/get` | Artifact / result |

App id mặc định: `482431` · Bot id: `7339470689562525703` · Skill video: `17`

Có thể override bằng env:

```bash
export DOLA_BASE_URL=https://www.dola.com
export DOLA_APP_ID=482431
export DOLA_BOT_ID=7339470689562525703
export DOLA_DEMO_MODE=false
```

## Khi API payload đổi

1. Trên dola.com tạo 1 video tay
2. DevTools → Network → copy request body của `pre_handle_v2` / `completion`
3. Gọi:

```bash
curl -X POST http://localhost:8787/api/raw \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": "YOUR_ACC_ID",
    "method": "POST",
    "path": "/alice/message/pre_handle_v2",
    "body": { ... payload đã capture ... }
  }'
```

## REST API (local)

| Method | Path | Mô tả |
|--------|------|--------|
| GET | `/api/health` | Health |
| GET | `/api/stats` | Stats |
| GET/POST | `/api/accounts` | List / tạo account |
| POST | `/api/accounts/{id}/check` | Validate session |
| POST | `/api/accounts/browser-login` | Playwright capture |
| POST | `/api/jobs` | 1 job |
| POST | `/api/jobs/batch` | Nhiều jobs |
| GET | `/api/jobs/{id}/download` | Tải video |

## Lưu ý

- Chỉ dùng **tài khoản bạn sở hữu**. Multi-account free-tier có thể vi phạm ToS Dola/ByteDance.
- Session cookie nhạy cảm — lưu local (`data/app.db`), không commit lên git.
- Payload `uplink_entity` reverse-engineer best-effort; nếu Dola đổi schema, cập nhật `app/dola_client.py` hoặc dùng `/api/raw`.
- Region-lock: một số IP/region có thể không vào được dola.com.

## Cấu trúc

```
dola-seedance-batch/
  app/
    main.py          # FastAPI
    dola_client.py   # Internal API client
    worker.py        # Batch worker
    db.py            # SQLite
    browser_login.py # Playwright Gmail login
    config.py
  static/            # UI
  data/              # SQLite
  downloads/         # Video output
```
