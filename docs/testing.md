# Kiểm thử

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
