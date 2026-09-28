# Ngày 5 — Trích xuất văn bản

## Phạm vi

Thêm khả năng trích xuất văn bản từ file đã lưu (`.txt`, `.pdf`, `.docx`) và
lưu kết quả vào MongoDB (`document_details.extracted_text`). Thêm nút trích
xuất và khung xem văn bản trên giao diện. Không đổi schema PostgreSQL, không
thêm service mới; `extracted_text` đã có sẵn trong `document_details` từ
Ngày 2 (giá trị mặc định `null`), Ngày 5 chỉ điền giá trị đó.

Dependency runtime mới: `pypdf>=5.0,<6`, `python-docx>=1.1,<2` (thuần Python,
không cần thư viện hệ thống).

## Thiết kế

| File | Vai trò |
| --- | --- |
| `app/extractors.py` | Hàm thuần `extract_text(content, extension)`; không DB/storage/network/log |
| `app/services/documents.py` | `extract_document()`: tải file từ MinIO, gọi extractor, ghi MongoDB |
| `app/repositories/documents.py` | `update_extracted_text()`: `update_one` + `$set`, ghi đè không tạo dòng mới |
| `app/api/documents.py` | `POST /documents/{id}/extract` |
| `app/ui/routes.py`, `document_detail.html` | Nút trích xuất (form POST), tab "Văn bản trích xuất" |

`extract_document()` tái dùng `document_row`, `require_ready`, `read_object`,
`storage_error`, `get_document` đã có ở Ngày 2–3; không tạo đường dẫn tải file
riêng cho trích xuất (`read_object` được tách ra dùng chung với download).

Lỗi trích xuất (`ExtractionError`, mã lỗi nội bộ: `unsupported_format`,
`input_too_large`, `invalid_utf8`, `invalid_text`, `corrupt_file`,
`encrypted_file`) đều trả HTTP 422 kèm mã lỗi trong message; không phân biệt
mã HTTP theo từng loại vì tất cả đều nghĩa là "nội dung không xử lý được",
không phải lỗi hệ thống. Lỗi đọc MinIO hoặc ghi MongoDB trả 503 (giữ nguyên
quy ước `storage_error` từ Ngày 2). Chỉ upload kiểm tra đuôi file, không quét
nội dung — một `.pdf`/`.docx` hỏng vẫn upload được (201), lỗi chỉ lộ ra khi
gọi `extract`.

## Route mới

| Route | Chức năng |
|---|---|
| `POST /documents/{id}/extract` | API trích xuất, trả tài liệu đầy đủ (JSON) |
| `POST /ui/documents/{id}/extract` | Form UI, redirect 303 về trang tài liệu |

## Kiểm thử tự động

Unit test thuần (`tests/unit/test_day5.py`, không cần Docker):

```powershell
python -m pip install -r requirements.txt httpx
python -m unittest discover -s tests/unit -v
```

- `TextExtractionTests` (12 test, giữ từ trước): UTF-8, BOM, dòng trắng, NUL, giới hạn ký tự.
- `PdfExtractionTests`, `DocxExtractionTests`: văn bản thật (PDF dựng tay bằng content
  stream tối thiểu, không cần renderer ngoài), trang trắng/không đoạn văn (rỗng, không lỗi),
  file hỏng (`corrupt_file`), PDF có mật khẩu (`encrypted_file`), cắt bớt, giới hạn 10 MiB.
- `ExtractServiceTests`: `extract_document()` với MinIO/MongoDB giả (fake), không mock phần
  logic thật — kiểm tra lưu đúng MongoDB, trích lại ghi đè (không tạo dòng mới), 409 khi
  tài liệu chưa `ready`, 503 khi tải MinIO hoặc ghi MongoDB lỗi, 422 khi file hỏng/có mật khẩu
  và không ghi MongoDB trong các trường hợp lỗi.

Test tích hợp trên Docker thật (`tests/integration/day5_test.py`), stack đang `healthy`:

```powershell
python .\tests\integration\day5_test.py
```

Tạo project mới, upload `samples/day5-sample.pdf` và `samples/day5-sample.docx`
(có sẵn, sinh từ `pypdf`/`python-docx`) cùng một file `.txt`, gọi `extract` cho
từng file, rồi xác minh theo hai nguồn độc lập: gọi `GET` lại qua HTTP và đọc
thẳng MongoDB bằng `docker compose exec web`. Kiểm tra trích xuất lại không
tạo dòng MongoDB mới (`count_documents == 1`), PDF hỏng và PDF có mật khẩu
(`samples/day5-encrypted-sample.pdf`) trả 422 và không ghi `extracted_text`,
trích xuất tài liệu không tồn tại trả 404.

Kết quả ghi vào `artifacts/day-05/day-05-check-result.txt`. Chạy sau khi
`docker compose up -d --build --wait` (dependency mới cần build lại image
`web`).

## Kiểm tra thủ công

- [ ] Mở một tài liệu `.txt`/`.pdf`/`.docx` bất kỳ trên giao diện.
- [ ] Tab "Văn bản trích xuất" hiển thị "Chưa trích xuất văn bản cho tài liệu này."
- [ ] Bấm "Trích xuất văn bản" → quay lại trang tài liệu, tab hiển thị văn bản,
      phương thức (`plain_text`/`pdf_text`/`docx_text`), số ký tự, số từ, thời điểm.
- [ ] Bấm lại ("Trích xuất lại") → nội dung cập nhật, không tạo bản ghi trùng.
- [ ] Tab JSON (MongoDB) hiển thị `extracted_text` trong `document_details`.

## Giới hạn đã biết

- Không OCR ảnh scan; PDF/DOCX không có lớp văn bản trả về chuỗi rỗng (không phải lỗi).
- Văn bản trích xuất lưu tối đa 200 000 ký tự (`truncated: true` nếu vượt), giống giới hạn
  đã có sẵn trong `app/extractors.py` từ khi chỉ hỗ trợ `.txt`.
- Trích xuất tải lại toàn bộ file gốc từ MinIO qua bộ nhớ (giới hạn 10 MiB, giống download);
  chưa streaming.
- Không có hàng đợi/nền: `extract` là đồng bộ, chặn tới khi xong hoặc lỗi.
