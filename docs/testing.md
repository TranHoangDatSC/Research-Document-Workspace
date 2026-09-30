# Kiểm thử

Có hai loại test:

| Loại | Thư mục | Cần Docker? | Chạy |
| --- | --- | --- | --- |
| Unit test (tự động) | `tests/unit/` | Không | `python -m pytest` |
| Test tích hợp (kịch bản) | `tests/integration/` | Có, stack đang chạy | từng script, xem các mục bên dưới |

## Unit test

```powershell
pip install -r requirements-dev.txt   # một lần: pytest + httpx
python -m pytest                      # hoặc: python -m pytest -v để xem từng ca
```

Không cần Docker hay API key: `tests/unit/support.py` thay PostgreSQL,
MongoDB và MinIO bằng bộ nhớ trong (bật lỗi từng kho bằng `backend.fail`),
LLM được thay bằng bản giả. Route, service, middleware đăng nhập và template
đều là code thật. Mỗi file ứng với một nghiệp vụ, chia class theo luồng
**thêm → xem → sửa → xóa**:

| File | Nội dung |
| --- | --- |
| `test_auth.py` | Băm mật khẩu, cookie phiên, đăng nhập/đăng xuất, quyền truy cập theo role |
| `test_users.py` | Quản lý tài khoản (admin): tạo, xem danh sách, đổi role, khóa/mở khóa |
| `test_projects.py` | Dự án: tạo, xem/tìm/phân trang, sửa, xóa (kéo theo tài liệu và lịch sử chat) |
| `test_documents.py` | Tài liệu: upload, xem/tải về, sửa metadata, xóa, và phục hồi khi một kho lỗi giữa chừng |
| `test_extraction.py` | Trích xuất văn bản .txt/.pdf/.docx và nút "Trích xuất văn bản" |
| `test_ai_retrieval.py` | Chia đoạn, xếp hạng BM25, prompt đánh số, đọc trích dẫn `[n]` |
| `test_ai_llm_client.py` | Gọi LLM: cấu hình, payload, đọc phản hồi, xoay model/key |
| `test_ai_chat.py` | Hỏi đáp AI và lịch sử hội thoại: hỏi (lưu), xem, xóa; domain |

`python -m unittest discover -s tests/unit` vẫn chạy được bộ này.

## Test tích hợp

Chạy ở thư mục gốc, stack đang `healthy`. Test ngày 2 chạy trên Windows bằng
Python ≥ 3.11 (chỉ thư viện chuẩn), gọi `127.0.0.1:8001` và `docker compose exec`.

Từ Ngày 6, mọi route (trừ `/health/*`) yêu cầu đăng nhập. Các test tích hợp
gọi HTTP thật (`day2_test.py`, `day3_test.py`, `day5_test.py`) tự đăng nhập
bằng `ADMIN_USERNAME`/`ADMIN_PASSWORD` đọc từ `.env`
(`tests/integration/_auth_helper.py`) trước khi chạy phần còn lại — cần
`.env` có 2 biến này khớp với tài khoản admin đã seed. Không cần sửa gì thêm
khi chạy các lệnh dưới đây như cũ.

Mỗi phase ghi kết quả vào `artifacts/day-02/day-02-<phase>-result.txt`, trả exit code
khác 0 khi FAIL. `artifacts/day-02/day-02-state.json` lưu ID project/tài liệu và SHA-256
(không có mật khẩu) — **đừng xoá**, phase `after`/`failure`/`recovery` cần nó.

## Kiểm tra nhanh dữ liệu cũ còn nguyên (không tạo dữ liệu mới)

```powershell
python .\tests\integration\day2_test.py after
```

Mong đợi dòng cuối `DAY 2 AFTER: PASS`.

## Bộ đầy đủ ngày 2

```powershell
# 1. Tạo project + 2 tài liệu mới, kiểm tra validate (ghi đè state.json)
python .\tests\integration\day2_test.py before

# 2. Tạo lại container, dữ liệu phải còn (KHÔNG thêm -v)
docker compose down
docker compose up -d --wait --wait-timeout 180
python .\tests\integration\day2_test.py after

# 3. Tắt MongoDB → upload phải 503 và tài liệu bị đánh dấu failed
docker compose stop mongo
try {
    python .\tests\integration\day2_test.py failure
} finally {
    docker compose start mongo
    docker compose up -d --wait --wait-timeout 180
}

# 4. MongoDB chạy lại → không còn rác, dữ liệu cũ vẫn tải được
python .\tests\integration\day2_test.py recovery
```

Cả 4 phase phải PASS. Không chạy lại `before` giữa chừng (sẽ thay state).

## Test persistence ngày 1

Chạy bên trong container web (dùng dependency và credentials của container):

```powershell
Get-Content -Raw .\tests\integration\day1_persistence.py |
    docker compose exec -T web python - verify
```

Máy mới chưa có dữ liệu checkpoint thì chạy `seed` một lần thay cho `verify`.

## Lưu bằng chứng

Chỉ sau khi PASS thật:

```powershell
$dir = "docs\evidence\$(Get-Date -Format yyyy-MM-dd)"
New-Item -ItemType Directory -Force $dir | Out-Null
Copy-Item artifacts\day-02\day-02-*-result.txt, artifacts\day-02\day-02-storage-evidence.json $dir
docker compose ps | Out-File -Encoding utf8 "$dir\compose-ps.txt"
```

Không commit `state.json` hay file download thử (đã nằm trong `artifacts/`, Git ignore).
