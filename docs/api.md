# API

Swagger đầy đủ: http://127.0.0.1:8001/docs

## Endpoint

| Method | Path | Kết quả |
| --- | --- | --- |
| GET | `/health/live` | 200 nếu process web còn sống |
| GET | `/health/ready` | 200 nếu cả 3 storage sẵn sàng, ngược lại 503 + service nào `down` |
| POST | `/projects` | Tạo project `{"name", "description"}` → 201 |
| GET | `/projects?limit=20&offset=0` | Danh sách project |
| GET | `/projects/{project_id}` | Một project |
| POST | `/projects/{project_id}/documents` | Upload multipart → 201 |
| GET | `/projects/{project_id}/documents?limit=20&offset=0` | Danh sách tài liệu (cả `pending`/`failed`) |
| GET | `/documents/{document_id}` | Metadata PostgreSQL + MongoDB (chỉ tài liệu `ready`) |
| GET | `/documents/{document_id}/download` | Tải file gốc |
| POST | `/documents/{document_id}/extract` | Trích xuất văn bản, lưu vào MongoDB, trả metadata đầy đủ |

Upload multipart: `file` (bắt buộc), `tags`, `authors` (phân cách bằng dấu phẩy),
`custom_metadata` (chuỗi JSON object, mặc định `{}`).

Ví dụ:

```powershell
curl.exe -F "file=@samples/day2-sample.txt" -F "tags=cloud,database" `
  http://127.0.0.1:8001/projects/<project_id>/documents
```

## Quy tắc kiểm tra

| Trường hợp | Mã |
| --- | --- |
| ID sai định dạng UUID, file rỗng, JSON metadata sai, >50 tag/author | 422 |
| Project/tài liệu không tồn tại | 404 |
| Đuôi file không phải `.txt`, `.pdf`, `.docx` | 415 |
| File > 10 MiB | 413 |
| Tài liệu chưa `ready` (đang `pending` hoặc `failed`) | 409 |
| Storage không phản hồi | 503 |

Chỉ kiểm tra đuôi file, không kiểm tra nội dung hay quét mã độc. Giới hạn 10 MiB
áp dụng trong endpoint, không phải giới hạn kích thước request toàn cục.
Hai file trùng tên vẫn lưu riêng vì object key theo UUID.

## Trích xuất văn bản

`POST /documents/{document_id}/extract` tải lại file gốc từ MinIO, trích xuất
văn bản (`app/extractors.py`, hàm thuần không I/O) rồi ghi đè trường
`extracted_text` trong `document_details` (MongoDB). Gọi lại nhiều lần chỉ ghi
đè, không tạo dòng mới. Trả về tài liệu đầy đủ (như `GET /documents/{id}`).

`extracted_text` sau khi trích xuất: `{"text", "method", "character_count",
"word_count", "truncated", "extracted_at"}`. `method` là `plain_text` (.txt),
`pdf_text` (.pdf) hoặc `docx_text` (.docx). Văn bản lưu tối đa 200 000 ký tự;
vượt quá thì `truncated: true` và chỉ phần đã lưu được tính vào
`character_count`/`word_count`.

| Trường hợp | Mã |
| --- | --- |
| Tài liệu không tồn tại | 404 |
| Tài liệu chưa `ready` | 409 |
| File hỏng, không đọc được (`.pdf`/`.docx`) | 422 |
| PDF có mật khẩu | 422 |
| `.txt` không phải UTF-8 hợp lệ | 422 |
| Không tải được file gốc hoặc ghi MongoDB thất bại | 503 |

Chỉ kiểm tra đuôi file ở bước upload, không quét nội dung, nên một `.pdf`/`.docx`
hỏng vẫn upload thành công (201) — lỗi chỉ xuất hiện khi gọi `extract`.
Tài liệu chưa từng trích xuất có `extracted_text: null`.

## Ghi và lỗi giữa chừng

Thứ tự upload:

1. Validate input, kiểm tra project tồn tại.
2. Ghi dòng PostgreSQL `pending`.
3. Ghi file vào MinIO.
4. Ghi metadata vào MongoDB.
5. Cập nhật PostgreSQL `ready` → trả 201.

Lỗi ở bước 3–4: xoá (best-effort) dữ liệu MongoDB/MinIO đã ghi, đánh dấu dòng SQL
`failed` (giữ lại làm audit). Nếu dọn dẹp lỗi, log ghi document ID và bước lỗi.

Đây **không** phải transaction phân tán:

- Process chết giữa chừng có thể để lại dòng `pending`.
- Mất phản hồi ở bước 5: giữ nguyên file và metadata vì DB có thể đã commit `ready`.
  Tra log theo document ID và xử lý tay; đừng xoá chỉ vì client nhận timeout/503.
