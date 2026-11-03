# Test env — local

Service chạy trên máy, credential ở `test-env/.env.local`.

## Base URL

Mỗi service 1 biến `LOCAL_<TÊN_SERVICE>_BASE_URL` trong `.env.local` (vd `LOCAL_APP_API_BASE_URL`). Service mới
thì thêm biến theo mẫu đó.

## Chạy local

- app-api: trong `Backend/app-api` chạy `go run main.go server --start`, nghe ở cổng `8080`. Config đọc từ
  `config/config.yaml` và `.env` của repo.
- `.env` của app-api hiện trỏ Postgres remote (DB staging dùng chung), không phải DB local. Vì vậy data và tài
  khoản giống staging, và mọi lệnh ghi qua API local cũng ghi vào DB staging: áp dụng quy tắc ghi của staging
  (`.claude/skills/testing-api/staging.md`).

## Login

- Login qua app-api: `POST $LOCAL_APP_API_BASE_URL/v1/login`, body `{"username": <email>, "password": <mật khẩu>}`
- Token ở `.data.access_token` (response dạng `{"code": 0, "message": "", "data": {...}}`)
- Gửi token bằng header `Authorization: Bearer <token>`
- Tài khoản: `LOCAL_TEST_ACCOUNT`/`LOCAL_TEST_PASSWORD`; để trống thì dùng tài khoản trong `.env.staging`
- Tài khoản bật 2FA cần thêm `"otp"` — dùng tài khoản test không bật 2FA
- Login bị rate limit theo username: sai nhiều lần liên tiếp sẽ bị chặn tạm

## Đánh dấu data test

Tên/tiêu đề tiền tố `[test]`, email `yp-test+<slug>@...`.
