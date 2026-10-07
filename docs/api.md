# API

Swagger đầy đủ: http://127.0.0.1:8001/docs

## Đăng nhập (Ngày 6)

Toàn bộ endpoint dưới đây **yêu cầu đăng nhập**, trừ `/health/live`,
`/health/ready`, `/login`. Đăng nhập bằng `POST /login` (form
`application/x-www-form-urlencoded`, trường `username`/`password`) — thành
công trả cookie session (`rdw_session`, ký HMAC, không lưu ở server); các
request sau gửi kèm cookie này. Thiếu/sai cookie: route `/ui/*` redirect
`/login`, route JSON API trả `401`. Gọi API bằng script cần đăng nhập trước
và giữ cookie qua các request tiếp theo (ví dụ `http.cookiejar` trong Python —
xem `tests/integration/_auth_helper.py`). Chi tiết tài khoản/role:
[ke-hoach-nang-cap.md](ke-hoach-nang-cap.md#5-đăng-nhập--đã-triển-khai-đảo-quyết-định-so-với-bản-đầu).

### Tài khoản tự phục vụ và dữ liệu riêng

| Trang | Ý nghĩa |
| --- | --- |
| `GET/POST /signup` | Tự đăng ký (role `user`, bắt buộc email). **Đóng mặc định** — admin mở ở trang Quản lý người dùng; `ALLOW_SIGNUP=false` khóa cứng → 403 |
| `GET /verify-email?token=…` | Xác minh email (48 giờ, một lần). Chưa xác minh thì chưa đăng nhập được |
| `GET/POST /verify-email/resend` | Gửi lại email xác minh (câu trả lời như nhau dù email có tồn tại) |
| `GET/POST /forgot-password` | Gửi link đặt lại mật khẩu (60 phút, một lần) |
| `GET/POST /reset-password?token=…` | Đặt mật khẩu mới; đăng xuất mọi phiên; cũng tính là đã xác minh email |
| `GET /account`, `POST /account/password`, `POST /account/logout-everywhere` | Đổi mật khẩu (đăng xuất các thiết bị khác), đăng xuất mọi thiết bị |
| `POST /admin/settings/signup`, `POST /admin/users/{id}/verify` | Admin mở/đóng đăng ký, xác minh email hộ |

- **Dữ liệu tách theo tài khoản**: mọi truy vấn dự án/tài liệu/chat chỉ thấy
  dự án của người đang đăng nhập (cả admin); của người khác trả `404`.
- **Thu hồi phiên**: cookie mang `session_version`, đối chiếu DB mỗi request.
  Đổi/đặt lại mật khẩu, khóa tài khoản, đổi role, "đăng xuất mọi thiết bị" →
  cookie cũ hết hiệu lực ngay. Role luôn đọc từ DB.
- **Email**: SMTP trong `.env`; chưa cấu hình thì nội dung (kèm link) ghi vào
  log. Link dựng từ `APP_BASE_URL`, không từ header `Host`.
- Rà soát bảo mật đầy đủ: [security.md](security.md).

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
| GET | `/documents/{document_id}/content` | Xem trước ảnh/âm thanh/video (inline, hỗ trợ `Range`); loại khác 415 |
| POST | `/documents/{document_id}/extract` | Trích xuất văn bản, lưu vào MongoDB, trả metadata đầy đủ |
| POST | `/projects/{project_id}/ask` | Hỏi đáp AI trên văn bản đã trích xuất trong project (`{"question"}`) |

`GET /projects/{project_id}/documents` nhận thêm `q` (tên tệp chứa chuỗi, không
phân biệt hoa thường; `%`/`_` được hiểu đúng nghĩa đen) và `kind` (`document`,
`presentation`, `data`, `image`, `audio`, `video`, `archive`; sai tên thì 422).

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
| Đuôi file không nằm trong danh sách dưới đây | 415 |
| File vượt giới hạn của nhóm | 413 |
| `Range` nằm ngoài file | 416 |
| Tài liệu chưa `ready` (đang `pending` hoặc `failed`) | 409 |
| Storage không phản hồi | 503 |

### Loại tệp được lưu

Danh sách và giới hạn nằm ở `app/file_types.py`; đổi giới hạn (MiB) qua biến
môi trường `MAX_UPLOAD_MB_<NHÓM>`, ví dụ `MAX_UPLOAD_MB_VIDEO=1000`.

| Nhóm | Đuôi | Giới hạn mặc định | Trích xuất văn bản / hỏi AI | Xem trước |
| --- | --- | --- | --- | --- |
| Tài liệu | `.txt .md .pdf .docx` | 50 MiB | Có | — |
| Trình chiếu | `.pptx` | 100 MiB | Có (chữ, bảng, ghi chú từng slide) | — |
| Dữ liệu | `.csv .json .xlsx` | 50 MiB | Có (.xlsx: từng sheet, mỗi hàng một dòng, giá trị đã tính của công thức) | — |
| Hình ảnh | `.png .jpg .jpeg .gif .webp` | 25 MiB | Không | Ảnh |
| Âm thanh | `.mp3 .wav .m4a .ogg` | 100 MiB | Không | Trình phát |
| Video | `.mp4 .webm .mov` | 500 MiB | Không | Trình phát, tua được |
| Tệp nén | `.zip` | 200 MiB | Không | — |

Upload được stream từ file tạm sang MinIO theo từng phần 8 MiB (SHA-256 tính
trong lúc truyền), tải về và xem trước cũng stream theo từng đoạn — file lớn
không nằm trọn trong RAM của container `web`. Trích xuất văn bản thì đọc cả
file vào bộ nhớ nên chỉ nhận file ≤ 50 MiB (lớn hơn vẫn lưu/tải bình thường,
chỉ không trích xuất được, 422).

Chỉ kiểm tra đuôi file, không kiểm tra nội dung hay quét mã độc. File luôn được
trả về với MIME type theo đuôi đã cho phép kèm `X-Content-Type-Options: nosniff`;
`/content` thêm `Content-Security-Policy: sandbox`. Hai file trùng tên vẫn lưu
riêng vì object key theo UUID.

## Trích xuất văn bản

`POST /documents/{document_id}/extract` tải lại file gốc từ MinIO, trích xuất
văn bản (`app/extractors.py`, hàm thuần không I/O) rồi ghi đè trường
`extracted_text` trong `document_details` (MongoDB). Gọi lại nhiều lần chỉ ghi
đè, không tạo dòng mới. Trả về tài liệu đầy đủ (như `GET /documents/{id}`).

`extracted_text` sau khi trích xuất: `{"text", "method", "character_count",
"word_count", "truncated", "extracted_at"}`. `method` là `plain_text` (.txt),
`pdf_text` (.pdf), `docx_text` (.docx), `xlsx_text` (.xlsx; mỗi sheet mở đầu
bằng `--- Sheet: tên ---`) hoặc `pptx_text` (.pptx; mỗi slide mở
đầu bằng `--- Slide N ---`, ghi chú người trình bày có tiền tố `Ghi chú:`).
`.md/.csv/.json` cũng là `plain_text`. `.zip` là `zip_text`: danh sách tệp bên
trong rồi văn bản của từng tệp đọc được (không mở zip lồng nhau; giải nén có
trần 20 MiB/tệp, 50 MiB tổng).

Ảnh, âm thanh, video ("Phân tích bằng AI"): stream từ MinIO lên Gemini Files
API, model trả về chữ trong ảnh + mô tả, hoặc transcript có mốc thời gian (+
diễn biến hình ảnh với video); `method` là `gemini_image|gemini_audio|gemini_video`
kèm `model`. Bản sao trên Google bị xóa ngay sau đó. Cần `LLM_PROVIDER=gemini`;
tắt bằng `AI_MEDIA_ANALYSIS=false`; tệp lớn hơn `AI_MEDIA_MAX_MB` (mặc định
200) → 422 mà không gửi gì.

Khi có service `worker` (Redis + RQ), phân tích ảnh/âm thanh/video chạy **nền**:
`POST .../extract` trả ngay 200 với tài liệu có `ai_job: {"status": "queued", ...}`
và `ai_job_pending: true`; gọi `GET /documents/{id}` tới khi `ai_job_pending`
thành `false`. Lỗi được ghi ở `ai_job.status = "failed"`, `ai_job.error`. Bấm lại
trong lúc đang chờ → 409. Không có hàng đợi thì chạy ngay trong request như trước.

Tài liệu, trình chiếu, dữ liệu, zip được **tự trích xuất ngay khi tải lên**
(lỗi thì tải lên vẫn thành công, bấm nút để thử lại); ảnh/âm thanh/video chỉ
phân tích khi người dùng bấm (tốn hạn mức API, gửi dữ liệu ra ngoài). Văn bản lưu tối đa 200 000 ký tự;
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

## Hỏi đáp AI (RAG thu nhỏ)

`POST /projects/{project_id}/ask` chia `extracted_text` của các tài liệu
`ready` (đã chọn) thành các đoạn theo ranh giới đoạn văn/câu. Tổng văn bản
nhỏ (≤ `full_text_max_chars` của domain) thì gửi **toàn văn**; lớn hơn thì
xếp hạng bằng **BM25** (bỏ dấu, bỏ stopword tiếng Việt, thêm bigram âm tiết —
không dùng embedding/vector DB, xem `app/rag.py`). Các đoạn được đánh số
`[1]..[n]` và gửi cho một LLM (`app/llm.py`, Gemini hoặc OpenAI qua
`LLM_PROVIDER`) cùng system instruction và temperature của domain đang bật
(`app/domains/<APP_DOMAIN>/`). Trả về `{"answer", "model", "sources": [{"ref",
"document_id", "original_name", "chunk_index"}]}` — `sources` chỉ gồm các
đoạn mà câu trả lời thực sự trích dẫn (`ref` = số `[n]` trong `answer`).

| Trường hợp | Mã |
| --- | --- |
| Project không tồn tại | 404 |
| Câu hỏi rỗng hoặc > 2000 ký tự | 422 |
| Chưa có tài liệu nào trong project được trích xuất văn bản | 409 |
| `LLM_PROVIDER`/`LLM_API_KEY` chưa cấu hình, hoặc lời gọi LLM thất bại | 503 |

### Lịch sử hội thoại

Mỗi (project, người dùng đăng nhập) có một cuộc hội thoại, lưu trong MongoDB
(`chat_messages`). Mỗi lần hỏi thành công lưu cặp câu hỏi và câu trả lời (kèm
`sources`, `model`); 10 tin gần nhất được gửi lại cho LLM làm ngữ cảnh (bỏ số
`[n]` cũ vì đoạn trích được đánh số lại mỗi lượt, cắt câu trả lời dài quá
4000 ký tự). Khi truy xuất bằng BM25, câu hỏi trước được ghép vào truy vấn để
câu hỏi nối tiếp ("giải thích thêm ý đó") vẫn tìm đúng đoạn. Kho lịch sử lỗi
thì vẫn trả lời, chỉ là không có lịch sử.

| Endpoint | Ý nghĩa |
| --- | --- |
| `GET /projects/{project_id}/chat` | `{"messages": [...]}` — 50 tin gần nhất của người dùng hiện tại, cũ trước |
| `DELETE /projects/{project_id}/chat` | 204 — xóa hội thoại của người dùng hiện tại (503 nếu MongoDB lỗi) |

Xóa project thì xóa luôn lịch sử chat của project đó.

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
