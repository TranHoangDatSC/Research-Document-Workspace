# Kiến trúc

Một ứng dụng FastAPI (monolith, 1 container `web`) và 3 dịch vụ lưu trữ.
Các module bên trong chỉ để tách trách nhiệm, không phải microservices.

```
Client ──HTTP──> web (FastAPI :8000)
                  ├── PostgreSQL  projects, documents (id, tên, kích thước, status)
                  ├── MongoDB     document_details (tags, authors, metadata, sha256)
                  └── MinIO       bucket MINIO_BUCKET: documents/<id>/original.<ext>
```

Chỉ `web` (host 8001 → container 8000) và MinIO (9000, console 9001) mở ra `127.0.0.1`.
PostgreSQL và MongoDB chỉ truy cập được trong network Compose.

## Các lớp trong `app/`

| Lớp | Trách nhiệm |
| --- | --- |
| `api/` | Path, method, validate query/path, mã HTTP |
| `schemas/` | Pydantic model của project |
| `services/documents.py` | Validate file, thứ tự ghi 3 storage, dọn dẹp khi lỗi |
| `repositories/` | Câu lệnh SQL và MongoDB; mỗi hàm một kết nối/transaction |
| `storage.py` | Tạo client, timeout 3 giây, host cố định theo tên service Compose |
| `bootstrap.py` | Tạo bảng/index/bucket (idempotent) và các hàm kiểm tra dùng cho `/health/ready` |

Project đơn giản nên `api/projects.py` gọi thẳng repository. Chưa dùng ORM nên
không có `models/`; schema DB nằm trong `bootstrap.py` (chưa có migration).

## Khởi động

1. Compose chờ PostgreSQL, MongoDB, MinIO `healthy`.
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
- Chưa có xác thực, chưa có migration schema, chỉ lọc file theo đuôi.
