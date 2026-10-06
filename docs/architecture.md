# Kiến trúc

Một ứng dụng FastAPI (monolith, 1 container `web`) và 3 dịch vụ lưu trữ.
Các module bên trong chỉ để tách trách nhiệm, không phải microservices.

```
Client ──HTTP──> web (FastAPI :8000)
                  ├── PostgreSQL  projects, documents (id, tên, kích thước, status)
                  ├── MongoDB     document_details (tags, authors, metadata, sha256)
                  │               chat_messages (lịch sử hỏi đáp AI theo project + user)
                  └── MinIO       bucket MINIO_BUCKET: documents/<id>/original.<ext>
```

Chỉ `web` (host 8001 → container 8000) và MinIO (9000, console 9001) mở ra `127.0.0.1`.
PostgreSQL và MongoDB chỉ truy cập được trong network Compose.

## Các lớp trong `app/`

Luồng phụ thuộc một chiều: `api/`, `ui/` → `services/` → `repositories/` → `storage.py`.

| Lớp | Trách nhiệm |
| --- | --- |
| `main.py` | Tạo app, gắn router, 3 middleware (header bảo mật, đăng nhập, chống CSRF form) |
| `api/` | API JSON: path, method, validate query/path, mã HTTP |
| `ui/` | Trang HTML (Jinja2) và trang quản trị; gọi thẳng `services/`, không gọi lại API qua HTTP |
| `schemas/` | Pydantic model của project |
| `services/` | Nghiệp vụ: `documents` (thứ tự ghi 3 storage, dọn dẹp khi lỗi), `projects`, `auth` (tài khoản, phiên, email), `rag` (hỏi đáp), `usage_stats` |
| `repositories/` | Câu lệnh SQL và MongoDB; mỗi hàm một kết nối/transaction |
| `storage.py` | Tạo client, timeout 3 giây, host cố định theo tên service Compose |
| `bootstrap.py` | Tạo bảng/index/bucket (idempotent) và các hàm kiểm tra dùng cho `/health/ready` |
| `access.py` | Người dùng của request hiện tại (ContextVar); service dùng để lọc dữ liệu theo chủ sở hữu |
| Module thuần | `auth.py` (băm mật khẩu, ký cookie), `extractors.py`, `rag.py`, `graph.py`, `file_types.py`: không truy cập kho dữ liệu |
| Gọi ra ngoài | `llm.py`, `media_ai.py` (Gemini/OpenAI), `mailer.py` (SMTP) |
| `settings.py`, `ratelimit.py` | Cấu hình runtime (bộ nhớ tiến trình) và rate limit (Redis, dự phòng bộ nhớ) |

Chưa dùng ORM nên không có `models/`; schema DB nằm trong `bootstrap.py` (chưa có migration).

## Khởi động

1. Compose chờ PostgreSQL, MongoDB, MinIO, Redis `healthy`.
2. `web` chạy `python -m app.bootstrap` (thử lại 5 lần), rồi mới chạy uvicorn.
3. Healthcheck của `web` gọi `/health/ready`: chỉ `healthy` khi cả 3 storage
   phản hồi và đủ bảng/index/bucket.

## Triển khai production (VPS)

```
Internet ──HTTPS──> Caddy (80/443, container)
                       │ reverse_proxy web:8000 (network nội bộ Compose)
                       ▼
                      web (:8000, chỉ publish 127.0.0.1:8001 để debug qua SSH tunnel)
                       ├── PostgreSQL, MongoDB, MinIO (giữ nguyên 127.0.0.1, không đổi)
```

Caddy là service duy nhất thêm vào, qua overlay `docker-compose.prod.yaml`
(không sửa `docker-compose.yaml`). Caddy tự xin và gia hạn chứng chỉ Let's
Encrypt cho domain trong biến `DOMAIN`; không cần cấu hình TLS thủ công. Chi
tiết từng bước: [day-06-huong-dan-trien-khai-vps.docx](day-06-huong-dan-trien-khai-vps.docx).

## Giới hạn đã biết

- Upload ghi 3 storage **không** phải transaction phân tán; xem [api.md](api.md#ghi-và-lỗi-giữa-chừng).
- Chưa có migration schema, chỉ lọc file theo đuôi.
- Cấu hình runtime nằm trong bộ nhớ tiến trình: chỉ chạy 1 worker.

Toàn bộ phần hạ tầng (service, cổng, volume, bí mật, VPS, sao lưu,
Redis): [infrastructure.md](infrastructure.md).
