# Kế hoạch nâng cấp

**Đăng nhập đã code và chạy được** (xem mục 5 — quyết định đã đảo lại từ bản
đầu: user thật + role, không phải một mật khẩu chung). Các mục còn lại
(UX nhập metadata, tìm kiếm, cấm trùng tên, giới hạn 100 MiB) vẫn chỉ là
**kế hoạch, chưa code** — nên làm sau khi Ngày 6 (triển khai VPS) và Ngày 7
(chốt đồ án) xong, vì mỗi mục đều chạm vào code đã test kỹ từ Ngày 2–5.
Ưu tiên thứ tự còn lại: giới hạn 100 MiB (S) → tìm kiếm tên file (S) → UX
metadata (M) → cấm trùng tên (M).

Quyết định đã chốt:

1. **Đăng nhập** — *đã đảo lại*: user thật trong PostgreSQL, quản trị viên
   (role `admin`) tạo/quản lý tài khoản, hai role `user`/`admin`, DB thiết kế
   để thêm role sau này không cần đổi kiểu dữ liệu. Xem mục 5.
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

## 5. Đăng nhập — ĐÃ TRIỂN KHAI (đảo quyết định so với bản đầu)

Quyết định ban đầu (một mật khẩu chung) đã bị đảo lại: hệ thống hiện có bảng
`users` thật trong PostgreSQL, quản trị viên tạo và quản lý tài khoản, hai
role `user`/`admin`. Đã code, đã test (unit + live trên Docker thật), không
còn là kế hoạch.

**Schema** (`app/bootstrap.py`, cùng transaction với các bảng khác):
```sql
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE CHECK (length(trim(username)) > 0),
    password_hash TEXT NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
```
`role` là CHECK constraint đơn giản — thêm role thứ ba sau này chỉ cần
`DROP CONSTRAINT` + `ADD CONSTRAINT` mới (đúng pattern Ngày 3 đã dùng để thêm
`deleting` vào `documents.status`), không đổi kiểu dữ liệu hay dữ liệu cũ.

**Mật khẩu & session** — không thêm dependency, chỉ thư viện chuẩn
(`app/auth.py`):
- Băm mật khẩu bằng `hashlib.pbkdf2_hmac` (200,000 vòng, salt ngẫu nhiên 16
  byte mỗi lần — 2 lần băm cùng mật khẩu ra 2 chuỗi khác nhau).
- Cookie session tự ký bằng HMAC-SHA256 (`user_id:username:role:hết_hạn:chữ_ký`),
  không lưu session ở server (stateless) — khớp việc `web` chỉ chạy 1 container.
  Cần biến môi trường `SESSION_SECRET` (chuỗi ngẫu nhiên dài) để ký.

**Bootstrap tài khoản đầu tiên**: `ADMIN_USERNAME`/`ADMIN_PASSWORD` trong
`.env` — `app/bootstrap.py:seed_admin_user()` tạo admin này **một lần** nếu
bảng `users` chưa có ai trùng username, bỏ qua (không lỗi) nếu thiếu 2 biến
này, để `.env` cũ không bị vỡ khi chưa cập nhật.

**Route**: `GET/POST /login`, `POST /logout` (`app/ui/routes.py`);
`GET/POST /admin/users`, `POST /admin/users/{id}/role`,
`POST /admin/users/{id}/active` (`app/ui/admin.py`, chỉ role `admin`, tự chặn
đổi role/khóa chính mình để tránh tự khóa mình ra ngoài).

**Middleware** `require_login` (`app/main.py`, cùng kiểu `same_origin_forms`
đã có): chặn mọi route trừ `/login`, `/health/*`, `/static/*`, trang Swagger;
thiếu/sai cookie → redirect `/login` (route UI) hoặc 401 JSON (route API).

**Phạm vi đã chọn — cả hai role dùng chung không gian tài liệu**: khác role
chỉ khác quyền vào `/admin/users`; project/tài liệu KHÔNG gắn với người tạo,
mọi user thấy được mọi project (giống hệ thống trước khi có đăng nhập). Nếu
sau này cần "mỗi user chỉ thấy tài liệu của mình", đó là một hạng mục riêng
(thêm `created_by` vào `documents`/`projects`, lọc theo user ở mọi query) —
chưa làm.

**Test đã sửa để không vỡ vì middleware mới**:
- `tests/unit/test_day3.py`: `Cases.setUp()` gắn thẳng một session cookie hợp
  lệ (role admin) vào `TestClient`, không qua form — vì lớp này test luồng
  tài liệu/dự án, không phải auth.
- `tests/unit/test_day6.py` (mới): hash mật khẩu, ký/xác minh token (round
  trip, giả mạo chữ ký, cố nâng role không có chữ ký hợp lệ, token hết hạn),
  service (`authenticate`/`create_user`/`set_role`/`set_active` và các guard
  409/422/400/404), và luồng HTTP thật (redirect khi chưa đăng nhập, 401 JSON
  cho API, đăng nhập sai/tài khoản bị khóa, `user` thường vào `/admin/users`
  bị 403, `admin` vào được).
- `tests/integration/_auth_helper.py` (mới): hàm `login()` dùng chung, đọc
  `ADMIN_USERNAME`/`ADMIN_PASSWORD` từ `.env` — `day2_test.py`, `day3_test.py`,
  `day5_test.py` đều gọi trước khi test phần còn lại; đã chạy lại và PASS
  trên Docker thật.
- `deploy/external-smoke-test.py`: thêm `--username` + prompt mật khẩu (không
  đọc `.env` vì máy chạy script này ở ngoài VPS, có thể không có repo).

**Chưa làm / giới hạn còn biết**: chưa có trang tự đổi mật khẩu (đổi qua admin
tạo lại tài khoản), chưa có "quên mật khẩu", cookie chưa gắn cờ `Secure` (an
toàn khi qua HTTPS/Caddy nhưng vẫn hoạt động được ở local HTTP — đơn giản hoá
có chủ đích cho đồ án, không phải mức production thật), chưa rate-limit số
lần đăng nhập sai.

## 6. Hỏi đáp AI trên tài liệu (RAG thu nhỏ) — ĐÃ TRIỂN KHAI

Không nằm trong yêu cầu Ngày 1–7 gốc, thêm theo yêu cầu riêng. Cho phép hỏi
một câu về nội dung các tài liệu trong một project, dựa trên `extracted_text`
đã có từ Ngày 5.

**Không dùng vector DB/embedding** — "RAG thu nhỏ" thật nghĩa: chấm điểm đoạn
văn bản bằng số từ khóa trùng với câu hỏi (tập hợp từ, so giao nhau), không
gọi API nào cho bước tìm kiếm, chỉ bước trả lời cuối cùng mới gọi LLM
(`app/rag.py` thuần Python, `app/llm.py` gọi HTTP qua `urllib`, không thêm
dependency).

**Luồng**: `POST /projects/{id}/ask` (`{"question"}`) → lấy toàn bộ tài liệu
`ready` trong project + `extracted_text` (MongoDB) → cắt thành đoạn ~1000 ký
tự (chồng lấn 100 ký tự) → chấm điểm theo câu hỏi, lấy 6 đoạn cao nhất → ghép
thành prompt kèm tên tài liệu + số đoạn (để trích nguồn) → gọi Gemini/OpenAI
→ trả `{"answer", "sources"}`.

**Cấu hình** (`.env`, xem hướng dẫn lấy key miễn phí trong `.env.example`):
`LLM_PROVIDER` (`gemini` khuyến nghị — free tier thật, không cần thẻ; hoặc
`openai` — không có free tier dài hạn), `LLM_API_KEY`, `LLM_MODEL`. Thiếu cấu
hình → lỗi 503 rõ ràng, không crash app.

**Giao diện**: khung "Hỏi đáp AI" trên trang project (`project_detail.html`),
form POST không redirect (trả lời hiển thị ngay dưới form, không lưu lịch sử
hỏi đáp).

**Test**: `tests/unit/test_rag.py` — cắt đoạn (biên, chồng lấn), chấm điểm
(không từ khóa trùng → rỗng, đúng thứ tự theo điểm, giới hạn top-k), dựng
prompt, phân tích phản hồi Gemini/OpenAI (giả HTTP response, không gọi API
thật), và service (thiếu câu hỏi → 422, chưa trích xuất tài liệu nào → 409,
LLM lỗi → 503, thành công → answer + sources đúng). Đã live-test trên Docker
thật tới đúng biên giới gọi LLM (chưa cấu hình `LLM_API_KEY` → 503 với thông
báo rõ) — chưa test được câu trả lời thật vì cần key Gemini/OpenAI thật, tự
thêm key vào `.env` rồi thử lại để xem chất lượng trả lời.

## Thứ tự đề xuất & khi nào nên bắt đầu

Không làm trước khi Ngày 6–7 xong — mỗi mục dưới đây đều sửa code đã có
evidence/test đã nộp. Theo thứ tự rủi ro thấp → cao:

1. Giới hạn 100 MiB + đo lại RAM (S)
2. Tìm kiếm tên file (S)
3. UX nhập metadata (M, không đổi API)
4. Cấm trùng tên (M, đổi hành vi đã test — sửa `day2_test.py` + `api.md` cùng lúc)

Mỗi mục nên là một commit riêng, chạy lại toàn bộ `python -m unittest
discover -s tests/unit` + integration test liên quan trước khi qua mục tiếp
theo — giống cách Ngày 2–6 đã làm.
