# Research Document Workspace

Ứng dụng FastAPI quản lý project và tài liệu nghiên cứu (đồ án Cloud).

| Thành phần | Vai trò |
| --- | --- |
| `web` (FastAPI) | API, cổng `127.0.0.1:8001` |
| PostgreSQL | Project + thông tin cố định của tài liệu |
| MongoDB | Metadata linh hoạt (tags, authors, custom metadata, SHA-256) |
| MinIO | File gốc, console `127.0.0.1:9001` |

Tất cả chạy bằng Docker Compose. Không cần cài Python/DB trên Windows để chạy app.

---

## 1. Setup lần đầu (máy mới)

Yêu cầu: Docker Desktop (Linux containers, Compose v2). Python ≥ 3.11 chỉ cần khi chạy test.

```powershell
# 1. Tạo .env — CHỈ khi chưa có .env
Copy-Item .env.example .env
notepad .env        # thay 3 dòng REPLACE_WITH_STRONG_PASSWORD

# 2. Kiểm tra cấu hình rồi khởi động
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
```

Container `web` tự chạy bootstrap (tạo bảng, index, bucket nếu chưa có) trước khi
mở API, nên không cần bước khởi tạo thủ công. Lệnh `up` trả về khi cả 4 service
`healthy`.

> ⚠️ Mật khẩu/tên DB được ghi vào volume **ở lần chạy đầu**. Sau đó đổi giá trị
> trong `.env` sẽ làm app không đăng nhập được DB cũ. Muốn đổi thì phải đổi trong DB,
> hoặc xoá dữ liệu bằng `docker compose down -v` (mất hết dữ liệu).

## 2. Mở lại máy / làm việc hằng ngày

1. Mở Docker Desktop, đợi nó báo *Engine running*.
2. Các container có `restart: unless-stopped` nên thường tự chạy lại. Để chắc chắn:

```powershell
docker compose up -d --wait --wait-timeout 180
```

3. Mở http://127.0.0.1:8001/docs (Swagger).

Sửa code trong `app/` → thêm `--build` vào lệnh trên. Sửa `.env` → chạy lại `up -d`
(`restart` không đọc lại `.env`).

## 3. Lệnh hay dùng

| Việc | Lệnh |
| --- | --- |
| Xem trạng thái | `docker compose ps` |
| Xem log web | `docker compose logs --tail 100 web` |
| Kiểm tra sẵn sàng | `curl.exe http://127.0.0.1:8001/health/ready` |
| Tạm dừng (giữ container) | `docker compose stop` |
| Tắt hẳn (giữ dữ liệu) | `docker compose down` |
| **Xoá toàn bộ dữ liệu** | `docker compose down -v` — không dùng nếu muốn giữ dữ liệu |

Endpoint: `/docs` (Swagger), `/health/live`, `/health/ready`. Chi tiết API: [docs/api.md](docs/api.md).

## 4. Triển khai lên VPS (tên miền + HTTPS)

Hướng dẫn từng bước (thuê VPS, Ubuntu, firewall, Caddy, domain, HTTPS, đo tài
nguyên, test từ mạng ngoài, chốt đồ án): [docs/day-06-huong-dan-trien-khai-vps.docx](docs/day-06-huong-dan-trien-khai-vps.docx).
File cấu hình đã chuẩn bị sẵn, chỉ cần copy lên VPS: `Caddyfile`,
`docker-compose.prod.yaml`, `.env.production.example`, `deploy/`.

## 5. Sự cố thường gặp

| Triệu chứng | Cách xử lý |
| --- | --- |
| `web` **unhealthy**, `/health/ready` báo 1 service `down` | `docker compose logs web` xem service nào lỗi, rồi `docker compose up -d --force-recreate <service>` |
| Log web `NameResolutionError`, `docker compose ps` thấy MinIO/DB **không có port** | Container bị rời network (hay gặp sau khi Docker Desktop khởi động lại): `docker compose up -d --force-recreate --wait` |
| Port 8001/9000/9001 bị chiếm | Tắt container/app khác dùng port đó (`docker ps`), không chạy MinIO riêng bằng `docker run` |
| Web thoát ngay, log `initialization failed` | Sai mật khẩu so với volume cũ → khôi phục đúng giá trị `.env` cũ |
| `Missing POSTGRES_DB` khi chạy compose | Chưa có `.env` hoặc thiếu biến → so với `.env.example` |

## 6. Cấu trúc thư mục

```
app/
  main.py            tạo FastAPI, gắn router, middleware đăng nhập
  api/               route HTTP: health, projects, documents, rag
  schemas/           Pydantic model request/response
  services/          điều phối upload/download/trích xuất/hỏi đáp giữa storage
  repositories/      SQL PostgreSQL + truy vấn MongoDB
  extractors.py      trích xuất văn bản .txt/.pdf/.docx (hàm thuần, không I/O)
  rag.py             chọn đoạn văn bản liên quan (từ khóa, không embedding)
  llm.py             gọi Gemini/OpenAI qua urllib (hàm thuần I/O, không SDK)
  auth.py            băm mật khẩu + ký/xác minh cookie session (hàm thuần)
  storage.py         tạo kết nối PostgreSQL/MongoDB/MinIO
  bootstrap.py       tạo + kiểm tra bảng/index/bucket, seed admin đầu tiên
tests/integration/   test tích hợp theo ngày (ngày 1–6)
samples/             file mẫu dùng khi test
docs/                api.md, architecture.md, testing.md, hướng dẫn triển khai VPS
docs/evidence/       bằng chứng test đã lưu (lịch sử, không sửa)
artifacts/            output test mới nhất (Git ignore)
deploy/               script chạy trên VPS: firewall, đo tài nguyên, test từ mạng ngoài
Caddyfile              cấu hình reverse proxy + HTTPS tự động (dùng ở VPS)
docker-compose.prod.yaml   overlay thêm Caddy, dùng cùng docker-compose.yaml
```

Tài liệu thêm: [Kiến trúc](docs/architecture.md) · [API](docs/api.md) · [Kiểm thử](docs/testing.md)
