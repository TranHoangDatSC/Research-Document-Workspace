# Rà soát bảo mật

Phạm vi: ứng dụng web (FastAPI) + PostgreSQL, MongoDB, MinIO sau Caddy trên
một VPS. Mục tiêu sử dụng: **nội bộ** (một nhóm/lab), không phải dịch vụ công
cộng. Mỗi mục ghi rõ cơ chế và nơi kiểm chứng bằng test (`tests/unit/`).

## Đã có

| Rủi ro | Biện pháp | Test |
| --- | --- | --- |
| Người lạ tự đăng ký vào hệ thống nội bộ | Đăng ký **đóng mặc định**; admin mở/đóng ở trang Quản lý người dùng; `ALLOW_SIGNUP=false` trong `.env` khóa cứng (admin cũng không mở được) | `SignupSettingTests` |
| Tài khoản rác / email người khác | Tự đăng ký phải bấm link xác minh email (48 giờ, dùng một lần) mới đăng nhập được | `SignupAndVerificationTests` |
| Xem dữ liệu của người khác | Mọi dự án có `owner_id`; mọi truy vấn dự án/tài liệu/chat lọc theo người đang đăng nhập (kể cả admin); dữ liệu người khác trả 404 như không tồn tại; service gọi khi không có người dùng → 401 (chặn mặc định) | `DataIsolationTests` |
| Phiên bị đánh cắp, máy quên đăng xuất | Cookie mang `session_version`, đối chiếu DB ở **mỗi** request: đổi/đặt lại mật khẩu, bị khóa, bị đổi role, "Đăng xuất khỏi mọi thiết bị" → mọi cookie cũ mất hiệu lực ngay | `SessionRevocationTests` |
| Hạ quyền admin nhưng cookie vẫn là admin | Role đọc từ DB mỗi request, không tin role trong cookie | `test_role_change_applies_immediately` |
| Dò mật khẩu | Giới hạn theo IP: đăng nhập 20/5 phút, đăng ký 5/giờ, quên mật khẩu & gửi lại xác minh 5/15 phút → 429 + `Retry-After`; PBKDF2-SHA256 200 000 vòng, salt riêng | `test_login_is_rate_limited` |
| Dò username / email | Username sai vẫn tốn một lần băm (thời gian như nhau); quên mật khẩu / gửi lại xác minh trả cùng một câu dù email có tồn tại; mail gửi ở luồng nền (không lộ qua thời gian phản hồi) | `test_unknown_username_costs…`, `test_unknown_or_locked…` |
| Link đặt lại mật khẩu giả mạo (Host header) | Link luôn dựng từ `APP_BASE_URL`; có SMTP mà thiếu `APP_BASE_URL` thì không gửi | `test_link_never_follows_a_forged_host_header` |
| Lộ token trong DB | Chỉ lưu SHA-256 của token đặt lại / xác minh; token tách mục đích (`reset` không dùng được như `verify` và ngược lại) | `test_full_flow`, `test_a_verify_token_cannot_reset…` |
| CSRF | Cookie `SameSite=Lax` + kiểm tra `Origin` cho mọi form (cả đăng nhập — chặn login CSRF); so theo host nên vẫn đúng sau Caddy (https ở trình duyệt, http trong container) | `test_account_forms_reject_cross_site_posts`, `test_forms_accepted_from_https…` |
| Đánh cắp cookie | `HttpOnly`, `SameSite=Lax`, `Secure` khi chạy https | `test_forms_accepted_from_https…` |
| Clickjacking, sniffing, rò Referer | `X-Frame-Options: DENY`, `CSP: frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: same-origin`, HSTS khi https | `test_security_headers_on_every_response` |
| Tệp tải lên chạy như mã | Chỉ nhận đuôi trong danh sách (không .svg/.html); trả về đúng MIME theo đuôi + `nosniff`; xem trước kèm `CSP: sandbox` | `test_media_preview_is_inline_and_sandboxed` |
| Zip bomb | Giải nén có trần (20 MiB/tệp, 50 MiB tổng), không mở zip lồng nhau | `test_decompression_budget_stops_a_zip_bomb` |
| Hết RAM do tệp lớn | Upload/tải về/xem trước/gửi Gemini đều stream theo đoạn | `test_video_streams_in_chunks…` |
| Bản đồ API công khai | `/docs`, `/redoc`, `/openapi.json` cần đăng nhập | `test_api_docs_need_login` |
| Quên đổi secret mẫu trên VPS | Bootstrap từ chối khởi động nếu còn `REPLACE_WITH…` hoặc `SESSION_SECRET` < 32 ký tự khi `APP_BASE_URL` là https | `SecretsCheckTests` |
| Cổng DB/MinIO lộ ra Internet | Compose chỉ bind `127.0.0.1`; chỉ Caddy (80/443) mở ra ngoài | `docker-compose.yaml` |

## Rủi ro còn lại (chấp nhận được cho dùng nội bộ, cần xử lý nếu mở công cộng)

- **Gửi dữ liệu cho Google**: hỏi đáp AI gửi đoạn văn bản, "Phân tích bằng AI"
  gửi nguyên ảnh/âm thanh/video lên Gemini. Free tier cho phép Google dùng dữ
  liệu để cải thiện sản phẩm. Tài liệu nhạy cảm: `AI_MEDIA_ANALYSIS=false`,
  hoặc dùng API trả phí / model chạy tại chỗ.
- **Prompt injection**: nội dung tài liệu có thể chứa câu lệnh nhắm vào AI.
  Ảnh hưởng giới hạn vì AI không có công cụ hành động (chỉ trả lời văn bản),
  nhưng câu trả lời có thể bị dẫn sai.
- **Giới hạn thử theo IP trong bộ nhớ**: mất khi khởi động lại, không chia sẻ
  giữa nhiều tiến trình; dò mật khẩu phân tán nhiều IP vẫn chậm lại nhờ PBKDF2
  nhưng không bị chặn theo tài khoản.
- **Không quét virus** tệp tải lên; không xác thực hai lớp (2FA); không có
  nhật ký kiểm toán (audit log) cho thao tác admin.
- **Mã hóa khi lưu**: dữ liệu PostgreSQL/MongoDB/MinIO nằm trên đĩa VPS không
  mã hóa ở tầng ứng dụng; phụ thuộc vào bảo mật VPS và bản sao lưu.

## Danh sách kiểm tra trước khi mở domain

1. `.env` trên VPS: secret mới, ngẫu nhiên (bootstrap sẽ chặn nếu quên).
2. `APP_BASE_URL=https://<domain>` và `ALLOW_SIGNUP=false` nếu chỉ dùng nội bộ.
3. Điền SMTP (xác minh email, quên mật khẩu) — hoặc chấp nhận admin tạo tài khoản.
4. Đổi mật khẩu admin seed ngay sau lần đăng nhập đầu (trang Tài khoản).
5. Firewall VPS chỉ mở 22 (SSH bằng key), 80, 443 — xem `deploy/`.
6. Chạy `docker compose ... up -d --build` rồi `python -m pytest`.
