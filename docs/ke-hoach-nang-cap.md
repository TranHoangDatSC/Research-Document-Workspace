# Kế hoạch nâng cấp (sau khi xong Ngày 5–7)

Đây là **kế hoạch**, chưa code. Phạm vi nằm ngoài lịch 7 ngày gốc (đăng nhập,
UX nhập metadata, tìm kiếm, cấm trùng tên, giới hạn 100 MiB) — nên làm **sau
khi** Ngày 6 (triển khai VPS) và Ngày 7 (chốt đồ án) đã xong, vì mỗi mục dưới
đây đều chạm vào code đã test kỹ từ Ngày 2–5. Ưu tiên thứ tự: giới hạn
100 MiB (S) → tìm kiếm tên file (S) → UX metadata (M) → cấm trùng tên (M) →
đăng nhập (M–L).

Ba quyết định đã chốt (bạn chọn qua câu hỏi trước đó):

1. **Đăng nhập**: một mật khẩu quản trị chung, không có bảng `users`, không
   phân quyền, không biết ai upload file nào.
2. **Metadata**: giữ nguyên `tags`/`authors`/`custom_metadata` trong MongoDB
   (đúng mục đích "tài liệu nghiên cứu" từ Ngày 2); chỉ đổi cách **nhập** —
   JSON thô → form key–value.
3. **Tìm kiếm**: chỉ theo tên file (`original_name` trong PostgreSQL).

## Việc đã có sẵn, không cần làm lại

- **"Multipart/form-data thay vì pre-signed URL"**: hệ thống hiện tại **đã**
  làm đúng như vậy từ Ngày 2 — client upload thẳng `multipart/form-data` tới
  server, server ghi trực tiếp vào MinIO (`app/services/documents.py:upload_document`).
  Không có pre-signed URL ở đâu trong codebase. Không cần đổi gì.
- **"Document gọn: document_id, file_name, file_path, file_size, uploaded_at"**:
  5 trường này đã tồn tại, chỉ chia giữa 2 kho theo thiết kế có chủ đích
  (xem [architecture.md](architecture.md)):
  - `document_id` → `document_details.document_id` (MongoDB)
  - `file_name` → `documents.original_name` (PostgreSQL)
  - `file_path` (MinIO) → `documents.object_name` (PostgreSQL)
  - `file_size` → `documents.size_bytes` (PostgreSQL)
  - `uploaded_at` → `documents.created_at` (PostgreSQL)

  `GET /documents/{id}` đã gộp cả hai kho thành **một object JSON phẳng** —
  client nhận đúng 5 trường này (cộng thêm metadata nghiên cứu). Gộp hẳn mọi
  thứ vào một MongoDB document sẽ đi ngược thiết kế "PostgreSQL giữ trường cố
  định, MongoDB giữ trường linh hoạt" đã xây và test từ Ngày 2–3 — không nên
  làm trừ khi có lý do cụ thể ngoài yêu cầu đã liệt kê.
- **Phân trang**: đã có, 20 tài liệu/trang, điều hướng bằng `?offset=`
  ([project_detail.html](../app/templates/project_detail.html)). Có thể tinh
  chỉnh thêm (số trang thay vì chỉ trước/sau) nhưng không bắt buộc.

## 1. Giới hạn file 100 MiB (nhỏ, ~1–2 giờ)

**Đổi**: `MAX_BYTES` trong `app/services/documents.py` và
`MAX_INPUT_BYTES` trong `app/extractors.py`, từ `10 * 1024 * 1024` thành
`100 * 1024 * 1024`.

**Rủi ro cần xử lý cùng lúc**: `web` container hiện giới hạn `mem_limit: 512m`
([docker-compose.yaml](../docker-compose.yaml)). Server đọc cả file vào bộ
nhớ (`file.file.read(...)` trả về `bytes`) rồi mới ghi MinIO/hash/trích xuất
— một request 100 MiB có thể giữ nhiều bản sao trong RAM cùng lúc, vài request
đồng thời có thể vượt 512 MB. Nên tăng `mem_limit` của `web` lên tối thiểu
1–2 GB cùng lúc với việc nâng giới hạn, và đo lại bằng
`deploy/resource-usage.sh` sau khi đổi.

**Trích xuất văn bản riêng**: nên cân nhắc giữ `MAX_INPUT_BYTES` của
`extract_text()` thấp hơn giới hạn upload (ví dụ 30–50 MiB) — PDF/DOCX 100 MiB
parse bằng `pypdf`/`python-docx` có thể chậm vài giây mỗi request (đồng bộ,
FastAPI chạy trong threadpool nên không chặn event loop, nhưng vẫn chiếm 1
worker thread lâu). Đây là quyết định UX cần bạn xác nhận: chấp nhận chờ lâu,
hay giữ trích xuất ở giới hạn nhỏ hơn upload.

**Test cần sửa**: các test hiện có kiểm tra đúng số `10 * 1024 * 1024` (ví dụ
`test_input_size_limit`, `test_day5.py`) — cập nhật hằng số kỳ vọng, không
đổi logic test.

## 2. Tìm kiếm theo tên file (nhỏ, ~2–3 giờ)

**API**: thêm query param `search` cho `GET /projects/{id}/documents`:
```sql
WHERE project_id = %s AND (%s = '' OR original_name ILIKE %s) 
ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s
```
(`%s ILIKE '%' || search || '%'`, escape `%`/`_` trong input trước khi query
— tránh người dùng tự chèn wildcard).

**UI**: ô tìm kiếm trong `project_detail.html`, submit qua `GET` (query
string, hoạt động không cần JS — giống cách phân trang hiện tại dùng
`?offset=`). Đổi `search` phải reset `offset=0`.

**Không đụng MongoDB**: theo quyết định đã chọn, chỉ tìm theo `original_name`
(PostgreSQL) — đơn giản, không cần giao (intersect) kết quả từ 2 kho.

## 3. UX nhập metadata: form key–value thay vì JSON thô (vừa, ~1 ngày)

**Không đổi service/API/schema** — `service.upload_document(...)` vẫn nhận
`custom_metadata` là chuỗi JSON như hiện tại (giữ tương thích cho ai gọi API
trực tiếp qua Swagger/script). Chỉ đổi **route UI**
(`app/ui/routes.py:upload`):

1. Form hiển thị vài cặp input `key`/`value` cố định (ví dụ 5 cặp, đủ dùng
   thực tế; không cần JS để hoạt động — đúng nguyên tắc "mọi trang chạy được
   không cần JS" đã ghi trong `app/static/app.js`).
2. (Tăng cường bằng JS, không bắt buộc) nút "+ Thêm trường" nhân bản một
   hàng input — ẩn nếu JS tắt, khi đó vẫn còn các hàng cố định ở bước 1.
3. Route UI gộp các cặp key/value không rỗng thành `dict`, `json.dumps(...)`
   rồi gọi `documents.upload_document(...)` y như cũ — không sửa gì ở
   `service`/`repository`/`schema`.
4. Trang chi tiết tài liệu (`document_detail.html`) đã hiển thị
   `custom_metadata` dạng `<pre>{{ ...|pretty_json }}</pre>` — có thể đổi
   thành bảng key–value cho dễ đọc hơn JSON thô (tùy chọn thêm).

**Rủi ro thấp**: không đổi API/DB, chỉ đổi HTML form + một hàm nhỏ trong
`app/ui/routes.py`. Test UI hiện có (`test_day3.py`) không bị ảnh hưởng vì
vẫn gọi thẳng `service.upload_document` qua patch, không qua form HTML.

## 4. Cấm trùng tên file (vừa, ~1 ngày — có phá vỡ hành vi cũ)

**⚠️ Đây là thay đổi hành vi đã tài liệu hóa và đã test từ Ngày 2.**
`docs/api.md` hiện ghi rõ: *"Hai file trùng tên vẫn lưu riêng vì object key
theo UUID"*, và `tests/integration/day2_test.py:before()` **cố tình** upload
2 file cùng tên `day2-sample.txt` vào cùng project rồi assert cả hai đều
thành công. Thêm ràng buộc cấm trùng tên sẽ làm test đó FAIL — cần sửa
`day2_test.py` (đổi tên file thứ 2) và `docs/api.md` cùng lúc, đồng thời note
lại trong báo cáo là hành vi đã đổi có chủ đích ở giai đoạn nâng cấp.

**Phạm vi**: đề xuất cấm trùng tên **trong cùng một project** (không phải
toàn hệ thống) — giữ đúng tinh thần "mỗi project là một không gian tài liệu
nghiên cứu riêng". Cần bạn xác nhận lại phạm vi này trước khi code.

**Thiết kế**: unique index **một phần** (partial), chỉ áp dụng cho tài liệu
`ready` — để không chặn việc tải lại cùng tên sau khi một lần upload trước đó
`failed`:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS documents_project_name_ready_idx
ON documents(project_id, original_name) WHERE status = 'ready';
```
Thêm trong `app/bootstrap.py` (cùng transaction, additive — giống cách Ngày 3
từng thêm giá trị `deleting` vào CHECK constraint). Ở
`app/services/documents.py:upload_document`, bắt `psycopg.errors.UniqueViolation`
quanh bước `mark_ready` (chèn cuối) và trả `HTTPException(409, "Tên file đã
tồn tại trong project này")` — kèm dọn dẹp MinIO/MongoDB đã ghi giống nhánh
lỗi hiện có (tái dùng `compensate(...)`).

**Race condition**: 2 request upload cùng tên gần như đồng thời — index ở DB
đảm bảo đúng, không cần lock ứng dụng.

## 5. Đăng nhập bằng mật khẩu quản trị chung (vừa–lớn, ~1–2 ngày)

**Không có bảng `users`, không đăng ký, không phân quyền** — đúng lựa chọn đã
chốt. Thiết kế tối giản, không cần thêm dependency:

- Hai biến môi trường mới: `ADMIN_PASSWORD` (mật khẩu, nên hash bằng
  `hashlib.pbkdf2_hmac` lưu sẵn thay vì plaintext — cần một script tạo hash
  một lần) và `SESSION_SECRET` (chuỗi ngẫu nhiên ≥32 byte để ký cookie).
- Cookie session **tự ký bằng HMAC** (thư viện chuẩn `hmac` + `hashlib`,
  không cần `itsdangerous`/`authlib`): giá trị cookie = `hết_hạn:chữ_ký`,
  server chỉ cần so `hmac.compare_digest` khi đọc — không lưu session ở đâu
  cả (stateless), khớp với việc `web` chỉ chạy 1 container.
- Trang `GET/POST /login` (form mật khẩu, redirect 303 sau khi đúng),
  `POST /logout` (xóa cookie).
- Middleware mới trong `app/main.py` (cùng kiểu với `same_origin_forms` đã
  có): chặn mọi route trừ `/login`, `/health/*`, `/static/*`; thiếu/sai cookie
  → redirect `/login` (route `/ui/*`) hoặc 401 JSON (route API).

**Chi phí thật sự nằm ở test, không phải code**: toàn bộ
`tests/unit/test_day3.py` và `test_day5.py` gọi thẳng `TestClient(app)` rồi
bắn request — sau khi thêm middleware, **tất cả sẽ nhận 401/redirect** trừ
khi test tự đăng nhập trước. Cần thêm một bước `login()` dùng chung trong
`setUp()` của cả hai file test, hoặc (đơn giản hơn cho test) cho phép bỏ qua
middleware khi biến môi trường test đặc biệt được set — cân nhắc kỹ để không
làm yếu bảo mật thật. Đây là lý do ước lượng "vừa–lớn" dù thiết kế auth không
phức tạp.

## Thứ tự đề xuất & khi nào nên bắt đầu

Không làm trước khi Ngày 6–7 xong — mỗi mục trên đều sửa code đã có evidence/
test đã nộp. Nếu làm, theo thứ tự rủi ro thấp → cao:

1. Giới hạn 100 MiB + đo lại RAM (S)
2. Tìm kiếm tên file (S)
3. UX nhập metadata (M, không đổi API)
4. Cấm trùng tên (M, đổi hành vi đã test — sửa `day2_test.py` + `api.md` cùng lúc)
5. Đăng nhập (M–L, sửa toàn bộ test hiện có)

Mỗi mục nên là một commit riêng, chạy lại toàn bộ `python -m unittest
discover -s tests/unit` + integration test liên quan trước khi qua mục tiếp
theo — giống cách Ngày 2–5 đã làm.
