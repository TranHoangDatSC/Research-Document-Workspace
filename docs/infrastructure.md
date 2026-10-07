# Hạ tầng hệ thống

Tài liệu này gom toàn bộ phần hạ tầng của Research Document Workspace vào một
chỗ: container nào chạy, cổng nào mở, dữ liệu nằm ở đâu, bí mật truyền vào
bằng cách nào, triển khai VPS ra sao, sao lưu/khôi phục thế nào, và Redis. Kiến trúc mã nguồn bên trong `app/` xem [architecture.md](architecture.md).

Mục lục:

1. [Tổng quan](#1-tổng-quan)
2. [Các service](#2-các-service)
3. [Mạng và cổng](#3-mạng-và-cổng)
4. [Lưu trữ dữ liệu (volume)](#4-lưu-trữ-dữ-liệu-volume)
5. [Image ứng dụng (Dockerfile)](#5-image-ứng-dụng-dockerfile)
6. [Khởi động và healthcheck](#6-khởi-động-và-healthcheck)
7. [Cấu hình và bí mật](#7-cấu-hình-và-bí-mật)
8. [Giới hạn tài nguyên và log](#8-giới-hạn-tài-nguyên-và-log)
9. [Triển khai production (VPS)](#9-triển-khai-production-vps)
10. [Sao lưu và khôi phục](#10-sao-lưu-và-khôi-phục)
11. [Dịch vụ bên ngoài](#11-dịch-vụ-bên-ngoài)
12. [Giới hạn hiện tại](#12-giới-hạn-hiện-tại)
13. [Redis](#13-redis)
14. [Tệp hạ tầng trong repo](#14-tệp-hạ-tầng-trong-repo)

---

## 1. Tổng quan

Ứng dụng là **một monolith FastAPI** (một container `web`, một tiến trình
uvicorn) cùng ba dịch vụ lưu trữ và Redis, tất cả chạy bằng Docker Compose. Trên VPS có
thêm Caddy làm reverse proxy và HTTPS.

```
                        Internet
                           │ 80/443 (chỉ production)
                     ┌─────▼─────┐
                     │   caddy   │  HTTPS tự động (Let's Encrypt)
                     └─────┬─────┘
                           │ http://web:8000   (mạng nội bộ Compose)
┌──────────────────────────▼──────────────────────────────┐
│ web  (FastAPI + uvicorn, image tự build)  127.0.0.1:8001 │──── redis:6379
└───────┬───────────────────┬─────────────────────┬────────┘     (bộ đếm rate limit,
                                                                  không lưu đĩa)
        │ postgres:5432     │ mongo:27017         │ minio:9000
  ┌─────▼──────┐     ┌──────▼──────┐       ┌──────▼──────┐
  │ PostgreSQL │     │   MongoDB   │       │    MinIO    │ 127.0.0.1:9000/9001
  │ dữ liệu    │     │ metadata,   │       │ tệp gốc     │
  │ cố định    │     │ văn bản,    │       │ (S3)        │
  │            │     │ chat, log AI│       │             │
  └────────────┘     └─────────────┘       └─────────────┘
        │ HTTPS ra ngoài (từ web)
        ├── Gemini / OpenAI-compatible API  (hỏi đáp, đồ thị, phân tích media)
        └── SMTP  (email xác minh, quên mật khẩu, xác thực API key)
```

| Môi trường | Lệnh khởi động | Service |
| --- | --- | --- |
| Local (Windows + Docker Desktop) | `docker compose up -d --build --wait` | web, worker, postgres, mongo, minio, redis |
| VPS (Ubuntu) | `docker compose -f docker-compose.yaml -f docker-compose.prod.yaml up -d --build --wait` | 6 service trên + caddy |
| Khôi phục dữ liệu | `docker compose -f docker-compose.restore.yaml ...` | stack tạm để test restore |

## 2. Các service

| Service | Image | Vai trò | Dữ liệu lưu |
| --- | --- | --- | --- |
| `web` | `research-document-workspace:dev` (build từ `Dockerfile`, nền `python:3.13-slim`) | API JSON, giao diện HTML, gọi LLM | Không lưu gì trên đĩa (stateless) |
| `postgres` | `postgres:16-bookworm` | Dữ liệu có cấu trúc | `users`, `projects`, `documents`, `password_reset_tokens`, `app_settings` |
| `mongo` | `mongo:7.0` | Dữ liệu bán cấu trúc | `document_details` (tags, authors, metadata, văn bản trích xuất, đồ thị thực thể, sha256), `chat_messages`, `llm_usage` |
| `minio` | `quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z` (ghim phiên bản) | Kho đối tượng tương thích S3 | Bucket `MINIO_BUCKET`, object `documents/<id>/original.<ext>` |
| `worker` | cùng image với `web`, lệnh `python -m app.worker` | Chạy job nền: phân tích ảnh/âm thanh/video bằng Gemini (mục 13) | Không lưu gì trên đĩa |
| `redis` | `redis:7.4-alpine` | Rate limit, cache phiên, pub/sub cấu hình, hàng đợi job (mục 13) | Chỉ trong RAM, không volume |
| `caddy` (chỉ prod) | `caddy:2-alpine` | Reverse proxy, HTTPS, nén zstd/gzip | Chứng chỉ trong volume `caddy_data` |

Vì sao ba kho dữ liệu: mỗi kho hợp với một loại dữ liệu (quan hệ, JSON linh
hoạt, tệp nhị phân lớn). Upload ghi lần lượt PostgreSQL (`pending`) → MinIO →
MongoDB → PostgreSQL (`ready`); không có transaction phân tán, lỗi giữa chừng
được dọn bằng bước bù trừ trong `app/services/documents.py`.

## 3. Mạng và cổng

Compose tạo một mạng bridge mặc định (`research-document-workspace_default`).
Các service gọi nhau bằng **tên service** (`postgres`, `mongo`, `minio`, `redis`, `web`),
host được ghi cứng trong `app/storage.py`.

| Cổng | Service | Bind | Ai truy cập được |
| --- | --- | --- | --- |
| 8001 → 8000 | web | `127.0.0.1` | Chỉ máy chủ (local: trình duyệt; VPS: SSH tunnel để debug) |
| 9000 | minio API | `127.0.0.1` | Chỉ máy chủ |
| 9001 | minio console | `127.0.0.1` | Chỉ máy chủ |
| 5432 | postgres | không publish | Chỉ trong mạng Compose |
| 27017 | mongo | không publish | Chỉ trong mạng Compose |
| 6379 | redis | không publish | Chỉ trong mạng Compose (có mật khẩu) |
| 80, 443 | caddy (prod) | `0.0.0.0` | Internet |

Trên VPS, `deploy/ufw-setup.sh` chỉ mở OpenSSH, 80, 443 (lớp bảo vệ thứ hai,
vì các cổng khác vốn đã chỉ bind `127.0.0.1`).

Lưu ý: Docker ghi rule iptables riêng, có thể vượt qua UFW với cổng publish ra
`0.0.0.0`. Hệ thống tránh vấn đề này bằng cách không publish cổng nào ra
`0.0.0.0` ngoài Caddy.

## 4. Lưu trữ dữ liệu (volume)

| Volume | Mount | Nội dung | Cần sao lưu |
| --- | --- | --- | --- |
| `postgres_data` | `/var/lib/postgresql/data` | Toàn bộ CSDL PostgreSQL | Có |
| `mongo_data` | `/data/db` | Dữ liệu MongoDB | Có |
| `mongo_config` | `/data/configdb` | Cấu hình MongoDB | Không bắt buộc |
| `minio_data` | `/data` | Tệp gốc | Có |
| `caddy_data` (prod) | `/data` | Chứng chỉ TLS, khóa ACME | Nên (tránh xin lại cert, dính rate limit Let's Encrypt) |
| `caddy_config` (prod) | `/config` | Cấu hình runtime Caddy | Không |

`docker compose down` giữ volume; `docker compose down -v` **xóa sạch** dữ liệu.
Mật khẩu PostgreSQL/MongoDB/MinIO được ghi vào volume ở lần chạy đầu: đổi trong
`.env` sau đó sẽ làm app không đăng nhập được kho cũ.

## 5. Image ứng dụng (Dockerfile)

```dockerfile
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt   # layer cache: chỉ cài lại khi đổi thư viện
COPY app ./app
EXPOSE 8000
CMD ["sh", "-c", "python -m app.bootstrap && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

- `.dockerignore` loại `.env`, `tests/`, `docs/`, `samples/`, `artifacts/` khỏi
  build context: image không chứa bí mật hay dữ liệu test.
- `exec` để uvicorn thành PID 1, nhận SIGTERM khi `docker stop`.
- Compose chạy 2 worker uvicorn (`WEB_CONCURRENCY`). Được vì rate limit, kiểm
  tra phiên và cấu hình runtime đều dùng chung qua Redis (mục 13). Bootstrap vẫn
  chạy một lần trước uvicorn.
- Thư viện: FastAPI, uvicorn, psycopg 3, pymongo, minio, redis, jinja2, pypdf,
  python-docx, python-pptx, openpyxl (`requirements.txt`). LLM và SMTP gọi bằng
  thư viện chuẩn, không cần SDK.

## 6. Khởi động và healthcheck

Thứ tự:

1. `postgres`, `mongo`, `minio`, `redis` khởi động, Compose chờ cả bốn `healthy`.
   `worker` chỉ khởi động sau khi `web` `healthy` (bootstrap đã tạo xong schema).
2. `web` chạy `app.bootstrap`: kiểm tra bí mật, tạo bảng/index/bucket nếu thiếu
   (idempotent, thử lại 5 lần mỗi kho), seed admin đầu tiên.
3. uvicorn mở cổng 8000; healthcheck của `web` gọi `/health/ready`.
4. (prod) `caddy` chỉ khởi động sau khi `web` `healthy`.

| Service | Lệnh healthcheck | interval / timeout / retries |
| --- | --- | --- |
| web | `urllib.request.urlopen('http://127.0.0.1:8000/health/ready')` | 10s / 20s / 12, `start_period` 30s |
| postgres | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` | 5s / 5s / 20 |
| mongo | `mongosh --eval "db.adminCommand('ping').ok"` | 5s / 10s / 20 |
| minio | `curl -fsS http://127.0.0.1:9000/minio/health/live` | 5s / 5s / 20 |
| redis | `redis-cli ping` (mật khẩu qua `REDISCLI_AUTH`) | 5s / 3s / 20 |
| worker | `python -m app.worker --check`: key heartbeat `rq:worker:<tên>` của worker còn tồn tại | 30s / 10s / 3, `start_period` 20s |

Endpoint ứng dụng:

- `/health/live`: tiến trình còn sống, không gọi kho nào.
- `/health/ready`: kiểm tra cả ba kho và đủ bảng/index/bucket; 503 nếu một kho
  `down`. Redis được báo thêm (`up`/`down`/`disabled`) nhưng không làm 503.
  Hai endpoint này không cần đăng nhập.

Mọi kết nối tới kho dữ liệu có timeout 3 giây (`app/storage.py`; PostgreSQL
thêm `statement_timeout=3000`), nên một kho treo không làm treo request.

## 7. Cấu hình và bí mật

Tất cả cấu hình đi qua biến môi trường, đọc từ `.env` (`env_file:` trong
Compose). `.env` nằm trong `.gitignore` và `.dockerignore`. Mẫu:
[`.env.example`](../.env.example) (local), [`.env.production.example`](../.env.production.example) (VPS).

| Nhóm | Biến | Ghi chú |
| --- | --- | --- |
| PostgreSQL | `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Compose báo lỗi ngay nếu thiếu (`${VAR:?}`) |
| MongoDB | `MONGO_INITDB_ROOT_USERNAME`, `MONGO_INITDB_ROOT_PASSWORD`, `MONGO_DB` | |
| MinIO | `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` (≥ 8 ký tự), `MINIO_BUCKET` | |
| Redis | `REDIS_PASSWORD` (chữ, số, `-`, `_`) | `REDIS_URL` do compose ghép, không đặt trong `.env` |
| Đăng nhập | `SESSION_SECRET` (≥ 32 ký tự), `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Admin chỉ được seed khi username chưa tồn tại |
| Tài khoản | `ALLOW_SIGNUP`, `APP_BASE_URL` | `APP_BASE_URL` bắt đầu bằng `https://` = chế độ production |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Trống `SMTP_HOST` = ghi email ra log thay vì gửi |
| LLM | `LLM_PROVIDER`, `LLM_API_KEY(S)`, `LLM_MODEL(S)`, `LLM_BASE_URL`, `LLM_TIMEOUT_SECONDS`, `LLM_THINKING_BUDGET` | Admin đổi được ở `/admin/settings` |
| AI media | `AI_MEDIA_ANALYSIS`, `AI_MEDIA_MAX_MB` | |
| Upload | `MAX_UPLOAD_MB_<KIND>` | Mặc định: document 50, presentation 100, data 50, image 25, audio 100, video 500, archive 200 MiB |
| Persona AI | `APP_DOMAIN` | Thư mục `app/domains/<tên>/` |
| Caddy | `DOMAIN` | Tên miền đã trỏ DNS A về IP VPS |

Cơ chế bảo vệ:

- `app.bootstrap.check_secrets()` từ chối khởi động khi `APP_BASE_URL` là
  `https://` mà còn giá trị `REPLACE_WITH…` hoặc `SESSION_SECRET` < 32 ký tự.
- Cấu hình LLM đổi từ UI được lưu bảng `app_settings` (PostgreSQL) và ghi đè
  `os.environ` của tiến trình; API key không bao giờ gửi ngược về trình duyệt;
  trang `/admin/keys` cần xác thực lại qua link email (cookie riêng 15 phút).
- Image không chứa bí mật; đổi môi trường chỉ cần đổi `.env`, không build lại.

## 8. Giới hạn tài nguyên và log

| Container | CPU | RAM | restart |
| --- | --- | --- | --- |
| web | 1.0 | 512m | unless-stopped |
| postgres | 1.0 | 512m | unless-stopped |
| mongo | 2.0 | 1g | unless-stopped |
| minio | 1.0 | 1g | unless-stopped |
| worker | 1.0 | 512m | unless-stopped |
| redis | 0.5 | 128m | unless-stopped |
| caddy (prod) | 0.5 | 256m | unless-stopped |
| **Tổng tối đa** | 7.0 | ~3.9 GB | |

Giới hạn là trần, không phải mức chiếm thật; số đo thật lấy bằng
`deploy/resource-usage.sh` (ghi `docker stats`, `free -h`, `df -h` vào
`artifacts/day-06/`).

Vì sao `web` chỉ cần 512 MB dù cho upload video 500 MiB: upload stream sang
MinIO theo part 8 MiB, download/preview stream 256 KiB mỗi lần, chỉ bước trích
xuất văn bản đọc cả tệp vào RAM và bị chặn ở 50 MiB (`extractors.MAX_INPUT_BYTES`).

Log: driver `json-file`, tối đa 10 MB × 3 tệp mỗi container (≤ 30 MB/container),
xem bằng `docker compose logs --tail 100 <service>`. Ứng dụng ghi log dạng
`event key=value`, không bao giờ ghi mật khẩu, token hay API key.

## 9. Triển khai production (VPS)

| Mục | Giá trị |
| --- | --- |
| Nhà cung cấp | `<điền: ví dụ BKNS>` |
| Gói / cấu hình | `<điền: vCPU, RAM, ổ đĩa>` |
| Hệ điều hành | Ubuntu `<điền phiên bản>` |
| Địa chỉ IP | `<điền>` |
| Tên miền | `<điền>` |

Các bước (chi tiết: hướng dẫn deploy cục bộ, không commit):

1. `ssh root@<IP>`; cài Docker bằng `curl -fsSL https://get.docker.com | sh`.
2. `git clone` mã nguồn; tạo `.env` từ `.env.production.example` với mật khẩu
   **mới** (không dùng lại bản local); đặt `DOMAIN`, `APP_BASE_URL=https://…`,
   `ALLOW_SIGNUP=false`.
3. Trỏ bản ghi DNS A của tên miền về IP VPS; kiểm tra bằng `nslookup`.
4. `sudo bash deploy/ufw-setup.sh` (mở SSH, 80, 443; bật UFW).
5. `docker compose -f docker-compose.yaml -f docker-compose.prod.yaml up -d --build --wait`.
6. Caddy tự xin và gia hạn chứng chỉ Let's Encrypt; kiểm tra
   `curl -I https://<domain>/health/ready`.
7. Từ máy khác mạng: `python deploy/external-smoke-test.py https://<domain> --username admin`
   (HTTPS + readiness, đăng nhập, tạo project, upload, so SHA-256, tải về).
8. Đo tài nguyên: `bash deploy/resource-usage.sh`.

Chi tiết HTTPS phía ứng dụng:

- Caddy gửi `X-Forwarded-Proto` và `X-Forwarded-For`; ứng dụng dùng chúng để
  đặt cookie `Secure`, header HSTS và lấy IP thật cho rate limit.
- Kiểm tra CSRF so khớp **host** của `Origin` (không so scheme, vì uvicorn
  thấy `http` sau Caddy).
- Header bảo mật mọi response: `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy: same-origin`, `Permissions-Policy`, CSP `frame-ancestors 'none'`.

Cập nhật phiên bản: `git pull` rồi chạy lại lệnh ở bước 5 (chỉ `web` được build
lại; các kho dữ liệu giữ nguyên).

## 10. Sao lưu và khôi phục

Đã kiểm chứng ở Ngày 4 (bằng chứng: `docs/evidence/day-04/`).

| Kho | Công cụ sao lưu | Khôi phục |
| --- | --- | --- |
| PostgreSQL | `pg_dump` chạy trong container `postgres` | `tests/integration/day4_restore_postgres.py` |
| MongoDB | `mongodump` chạy trong container `mongo` | `tests/integration/day4_restore_mongo.py` |
| MinIO | Xuất object + checksum qua container ứng dụng | `tests/integration/day4_restore_minio.py` |

- Tạo bản sao lưu: `python tests/integration/day4_backup.py` (ghi vào
  `artifacts/backups/<thời điểm>-<id>/`, kèm `backup-report.json`).
- Khôi phục được thử trên stack riêng `docker-compose.restore.yaml` (cùng image,
  volume riêng, không đụng dữ liệu đang chạy), service `restore_tools` mount
  thư mục backup chỉ đọc.
- Kiểm tra sau khôi phục: `day4_verify_restore.py` so số bản ghi và SHA-256.

Chưa có: lịch sao lưu tự động (cron) và lưu bản sao ra ngoài VPS. Đề xuất: cron
hằng ngày chạy script sao lưu, đồng bộ thư mục `artifacts/backups/` sang một
bucket S3/ổ khác, giữ 7 bản gần nhất.

## 11. Dịch vụ bên ngoài

| Dịch vụ | Dùng cho | Bắt buộc | Khi không có |
| --- | --- | --- | --- |
| Gemini API (hoặc OpenAI-compatible, kể cả Ollama nội bộ) | Hỏi đáp, đồ thị thực thể | Không | Nút hỏi đáp báo lỗi rõ ràng, phần còn lại chạy bình thường |
| Gemini Files API | Phân tích ảnh/âm thanh/video | Không | Tắt bằng `AI_MEDIA_ANALYSIS=false` |
| SMTP (Gmail, Brevo, Resend…) | Email xác minh, quên mật khẩu, mở khóa API key | Không | Email ghi ra log `web` |
| Let's Encrypt | Chứng chỉ HTTPS | Chỉ prod | Caddy báo lỗi ACME |

Mỗi lượt gọi LLM được ghi vào `mongo.llm_usage` (thống kê ở `/admin/stats`).
Ứng dụng tự xoay key × model khi gặp 429/503.

## 12. Giới hạn hiện tại

- **Một máy:** 2 worker trong một container. Chạy nhiều replica `web` cần thêm
  load balancer phía trước; trạng thái chung đã nằm trong Redis/PostgreSQL.
- **RAM của `web` với 2 worker:** vẫn giới hạn 512m; mỗi worker có thể giữ một
  tệp ≤ 50 MiB khi trích xuất văn bản. Đo lại bằng `deploy/resource-usage.sh`.
- **Mỗi hàm repository mở kết nối PostgreSQL mới** (chưa có connection pool).
  Kiểm tra phiên đăng nhập đã được cache trong Redis 30 giây.
- **Hàng đợi job không lưu đĩa:** Redis khởi động lại thì job chưa chạy bị mất;
  tài liệu đó hiện "đang chờ" tới khi quá hạn (20 phút) rồi bấm lại được.
- Upload qua 3 kho không phải transaction phân tán (có bước bù trừ).
- Chưa có migration tool; schema thay đổi kiểu cộng thêm trong `bootstrap.py`.
- Chưa có giám sát tập trung (metrics/alert), chưa có sao lưu tự động.

## 13. Redis

### 13.1. Vì sao thêm Redis

Rate limiter (`app/ratelimit.py`) trước đây đếm trong bộ nhớ tiến trình: chỉ
đúng với một worker uvicorn, và bộ đếm về 0 mỗi lần `web` khởi động lại. Redis
lưu bộ đếm ở một service riêng, dùng chung cho mọi tiến trình `web`. Đây là bước
cần trước khi chạy nhiều worker.

Redis không phải kho dữ liệu chính: mọi thứ trong đó tạo lại được, nên mất
Redis không mất dữ liệu người dùng.

### 13.2. Các giai đoạn

| Giai đoạn | Việc | Thay cho | Trạng thái |
| --- | --- | --- | --- |
| 1 | Rate limiter dùng chung | `deque` trong bộ nhớ | **Đã làm** |
| 2 | Cache kiểm tra phiên đăng nhập (`session:<user_id>`, TTL 30 s) | 1 truy vấn PostgreSQL mỗi request | **Đã làm** |
| 3 | Pub/sub `settings:changed` để mọi worker nạp lại cấu hình runtime; chạy 2 worker | `os.environ` riêng từng tiến trình, 1 worker | **Đã làm** |
| 4 | Hàng đợi job nền (RQ) + service `worker` cho phân tích ảnh/âm thanh/video | Chạy trong request, chờ vài phút | **Đã làm** |

Số worker đặt bằng `WEB_CONCURRENCY: "2"` trong service `web` của compose (uvicorn tự
đọc biến này). Image chạy riêng, không qua compose, vẫn 1 worker.

### 13.3. Service `redis` trong `docker-compose.yaml`

```yaml
  redis:
    image: redis:7.4-alpine
    environment:
      REDISCLI_AUTH: ${REDIS_PASSWORD:?Missing REDIS_PASSWORD}
    command:
      - sh
      - -c
      - exec redis-server --requirepass "$$REDISCLI_AUTH" --maxmemory 64mb --maxmemory-policy volatile-lru --save '' --appendonly no
    cpus: "0.5"
    mem_limit: 128m
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
```

Service `web` nhận `REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0` (ghép
trong compose, nên mật khẩu chỉ viết một lần trong `.env`) và
`depends_on: redis: condition: service_healthy`.

Quyết định thiết kế:

- **Không publish cổng** (giống PostgreSQL/MongoDB): chỉ `web` gọi qua
  `redis:6379`. Vẫn đặt mật khẩu phòng khi có container khác lọt vào mạng.
- **Mật khẩu qua `REDISCLI_AUTH`**: `redis-cli` tự đọc biến này nên healthcheck
  không cần `-a`; `command` chỉ chứa `$REDISCLI_AUTH`, shell trong container mới
  thay bằng giá trị thật.
- **Mật khẩu chỉ gồm chữ, số, `-`, `_`** vì được ghép vào URL (`secrets.token_urlsafe`).
- **Không lưu đĩa** (`--save ''`, `--appendonly no`), không volume, không sao lưu.
  Bộ đếm sống qua lần khởi động lại `web`; chỉ mất khi chính Redis khởi động lại.
- **`maxmemory 64mb` + `volatile-lru`**: khi đầy, Redis chỉ xóa key có TTL (bộ
  đếm, cache phiên), không bao giờ xóa job đang chờ trong hàng đợi; không chạm
  `mem_limit` 128m. (Bản đầu dùng `allkeys-lru`, đổi khi thêm hàng đợi ở giai đoạn 4.)

### 13.4. Mã nguồn

| Tệp | Nội dung |
| --- | --- |
| `requirements.txt` | `redis>=5,<6` |
| `app/storage.py` | `redis_client()`: một client dùng chung (có pool), timeout 1 s; `None` khi không đặt `REDIS_URL` |
| `app/ratelimit.py` | Cửa sổ trượt trên sorted set `rl:<bucket>:<ip hoặc user_id>`: pipeline `ZREMRANGEBYSCORE` → `ZCARD` → `ZRANGE` (lượt cũ nhất, để tính `Retry-After`), rồi `ZADD` + `EXPIRE`. Mọi lỗi Redis → log `ratelimit_redis_unavailable`, đếm trong bộ nhớ |
| `app/bootstrap.py` | `check_redis()` trả `up`/`disabled`; `REDIS_PASSWORD` nằm trong danh sách bí mật bị chặn giá trị mẫu trên production |
| `app/api/health.py` | `/health/ready` báo `redis`: `up`, `down` hoặc `disabled`, không tính vào `ready` |
| `app/templates/index.html`, `base.html` | Trạng thái Redis trên trang chủ và sidebar |

Hai bước kiểm tra trong `_redis_hit` không nguyên tử: hai request đến đúng lúc
chạm giới hạn có thể cùng lọt. Chấp nhận được với rate limit.

Cache phiên đăng nhập (giai đoạn 2):

| Tệp | Nội dung |
| --- | --- |
| `app/cache.py` | `get`/`set`/`delete` giá trị JSON trên Redis. Mọi lỗi coi như cache miss; cảnh báo `cache_redis_unavailable` tối đa 1 lần/phút (cache được đọc ở mọi request) |
| `app/services/auth.py` | `session_user` đọc `session:<user_id>` = `{"version", "user"}` trước khi hỏi PostgreSQL; chỉ dùng khi `version` trùng `session_version` trong cookie. Chỉ cache phiên hợp lệ |
| `app/services/auth.py` | `_forget_session` xóa key sau đổi mật khẩu, đặt lại mật khẩu, đăng xuất mọi nơi, đổi role, khóa/mở khóa |

Đồng bộ cấu hình giữa các worker (giai đoạn 3):

| Tệp | Nội dung |
| --- | --- |
| `app/settings.py` | `update()` lưu PostgreSQL, áp dụng trong worker hiện tại rồi `PUBLISH settings:changed <tên key>` (không gửi giá trị: API key là bí mật). `start_listener()` chạy một thread mỗi worker: `SUBSCRIBE`, nhận tên key thì đọc lại key đó từ PostgreSQL. Mất kết nối thì thử lại sau 5 s và đọc lại **mọi** key để bù tin nhắn bị lỡ |
| `app/main.py` | lifespan gọi `start_listener()` / `stop_listener()` |
| `app/ui/routes.py` | `asset_version` lấy mtime mới nhất của `static/` thay vì giờ khởi động, để 2 worker trả cùng một giá trị |
| `docker-compose.yaml` | `WEB_CONCURRENCY: "2"` |

Hàng đợi job nền (giai đoạn 4):

| Tệp | Nội dung |
| --- | --- |
| `app/jobs.py` | `enqueue()` đưa job vào hàng đợi RQ `media` (timeout 15 phút). Không có Redis hoặc lỗi → trả `False` để nơi gọi chạy ngay trong request |
| `app/worker.py` | Tiến trình của service `worker`: nạp cấu hình đã lưu, nghe pub/sub cấu hình, chạy `rq.Worker` với tên `<hostname>-<ngẫu nhiên>` (ghi vào `/tmp/rq-worker-name`). `--check` kiểm tra key heartbeat của tên đó. Dùng client Redis riêng không `socket_timeout` vì worker chờ hàng đợi lâu |
| `app/services/documents.py` | `extract_document` với ảnh/âm thanh/video: ghi `ai_job = {status: queued, job_id, queued_at}` vào MongoDB **trước**, rồi enqueue; trả ngay tài liệu với `ai_job_pending: true`. Đang chờ mà bấm lại → 409. `run_ai_analysis` (chạy trong worker) đánh dấu `running`, gọi Gemini, xóa `ai_job` khi xong hoặc ghi `failed` + lý do. Job bị thay bằng job mới hơn thì bỏ qua |
| `app/templates/document_detail.html`, `app/static/app.js` | Thông báo "đang phân tích trong nền", khóa nút; JS hỏi `GET /documents/{id}` mỗi 5 s, xong thì tải lại trang. Hiện lỗi của lần trước nếu có |
| `docker-compose.yaml` | Service `worker`, `depends_on: web: service_healthy`; tăng số worker: `docker compose up -d --scale worker=2` |

Healthcheck không dùng `Worker.all()` (đọc tập `rq:workers`): lần chạy thật đầu
tiên, sau bước dừng Redis của `redis_test.py`, Redis khởi động lại trống trơn
(không lưu đĩa), tập `rq:workers` mất hẳn còn worker vẫn chạy job bình thường, nên
container bị báo `unhealthy` sai. Key heartbeat thì được tạo lại ở lần heartbeat
kế tiếp. Tên worker có phần ngẫu nhiên vì RQ từ chối khởi động nếu tên đó còn
heartbeat sống (container bị kill cứng rồi chạy lại).

Job bị kẹt (worker chết giữa chừng) giữ `running` trong MongoDB; sau 20 phút
(`JOB_TIMEOUT_SECONDS` + 5 phút) `ai_job_pending` thành `false` và bấm lại được.

Vì sao key không chứa version: bản đầu dùng `session:<user_id>:<version>` và
không xóa gì, dựa vào việc version tăng khi thu hồi phiên. Unit test cho thấy
lỗ hổng: cookie **cũ** vẫn khớp đúng entry của chính nó thêm tối đa 30 giây sau
khi tài khoản bị khóa. Thiết kế hiện tại xóa entry ngay khi thu hồi, nên khóa
tài khoản vẫn có hiệu lực ngay. Nếu Redis mất kết nối đúng lúc xóa, entry cũ tồn
tại tối đa 30 giây (TTL).

### 13.5. Kiểm thử

Unit, không cần Redis thật (`tests/unit/test_ratelimit.py`, Redis giả trong file):

- Lượt thứ `limit + 1` trả 429 kèm `Retry-After`; khóa có TTL bằng cửa sổ.
- Bộ đếm còn nguyên sau khi xóa bộ nhớ tiến trình (giả lập `web` khởi động lại).
- Mỗi IP đếm riêng.
- Redis lỗi → vẫn giới hạn bằng bộ nhớ, có log cảnh báo, không trả 500.
- Không có `REDIS_URL` → đếm trong bộ nhớ.
- `/health/ready` 200 khi Redis `down`; `disabled` khi không cấu hình.

`tests/unit/test_settings_sync.py`: `update` chỉ publish tên key, không publish
giá trị; Redis lỗi thì vẫn lưu và áp dụng; tin nhắn từ worker khác áp dụng giá
trị đã lưu, giá trị rỗng khôi phục `.env`; key lạ bị bỏ qua; listener nhận tin
nhắn, mất kết nối thì kết nối lại và đọc lại toàn bộ.

`tests/unit/test_ai_jobs.py`: bấm phân tích trả về ngay và không gọi Gemini;
worker lưu văn bản và xóa `ai_job`; bấm lại khi đang chờ → 409; job lỗi được ghi
và hiện trên trang, bấm lại được; không có hàng đợi thì chạy ngay; job quá hạn
không chặn nữa; job bị thay thế không chạy; tệp văn bản thường không vào hàng đợi.

`tests/unit/test_session_cache.py`: lần kiểm tra thứ hai không đọc PostgreSQL;
khóa tài khoản và đổi role chấm dứt phiên đang cache ngay; cookie cũ không khớp
entry của version mới; phiên không hợp lệ không được cache; Redis lỗi thì đọc
PostgreSQL và chỉ cảnh báo một lần.

Tích hợp trên stack thật: `python tests/integration/redis_test.py` chạy các
kịch bản của Bảng 4.2 trong báo cáo (PING, cache phiên có TTL ≤ 30 s và bị xóa khi
đăng xuất mọi nơi, `web` có ≥ 2 worker, đổi cấu hình một lần thì 30/30 lần đọc
thấy giá trị mới, `worker` healthy và chạy được một job thử do `web` đưa vào hàng
đợi, 21 lần đăng nhập sai → 429, restart
`web` vẫn 429, dừng Redis vẫn đăng nhập được, `/health/ready` 200 với redis
`down`, log fallback, RAM Redis), bật lại Redis, xóa bộ đếm thử nghiệm và ghi
kết quả vào `artifacts/redis/`.

### 13.6. Rủi ro

| Rủi ro | Xử lý |
| --- | --- |
| Redis chết làm hỏng đăng nhập | Fallback in-memory; healthcheck + `restart: unless-stopped` |
| Thêm RAM trên VPS gói nhỏ | `mem_limit 128m`, `maxmemory 64mb` |
| Lộ mật khẩu Redis | Chỉ trong `.env`; bootstrap chặn giá trị mẫu trên production |
| Cache phiên cũ sau khi khóa tài khoản | Xóa entry khi thu hồi phiên + so `version`; TTL 30 s nếu xóa thất bại |

## 14. Tệp hạ tầng trong repo

| Tệp | Nội dung |
| --- | --- |
| `Dockerfile` | Image ứng dụng |
| `.dockerignore` | Loại bí mật/test/docs khỏi build context |
| `docker-compose.yaml` | 6 service, volume, healthcheck, giới hạn tài nguyên, log |
| `docker-compose.prod.yaml` | Overlay thêm Caddy (dùng kèm, không dùng riêng) |
| `docker-compose.restore.yaml` | Stack tạm để thử khôi phục |
| `Caddyfile` | `{$DOMAIN}` → `reverse_proxy web:8000`, nén zstd/gzip |
| `.env.example`, `.env.production.example` | Mẫu cấu hình |
| `deploy/ufw-setup.sh` | Firewall VPS |
| `deploy/resource-usage.sh` | Chụp số đo RAM/CPU/disk |
| `deploy/external-smoke-test.py` | Kiểm thử từ mạng ngoài |
| `tests/integration/day4_*.py` | Sao lưu, khôi phục, kiểm tra khôi phục |
| `tests/integration/redis_test.py` | Kiểm thử Redis trên stack thật |
