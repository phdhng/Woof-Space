# Test env — staging

Dùng chung cho mọi service: chung 1 DB, chung access_token. Base URL và tài khoản test ở
`test-env/.env.staging`; credential DB ở `test-env/.env.dbstaging` (dùng chung với local).

## Base URL

Mỗi service 1 biến `STAGING_<TÊN_SERVICE>_BASE_URL` trong `.env.staging` (vd `STAGING_APP_API_BASE_URL`,
`STAGING_APP_AGENTS_SERVICE_BASE_URL`). Service mới thì thêm biến theo mẫu đó.

## Login

- Login qua app-api: `POST $STAGING_APP_API_BASE_URL/v1/login`, body
  `{"username": <email>, "password": <mật khẩu>}`
- Token ở `.data.access_token` (response dạng `{"code": 0, "message": "", "data": {...}}`), dùng được cho mọi
  service
- Gửi token bằng header `Authorization: Bearer <token>`
- Tài khoản bật 2FA cần thêm `"otp"` — dùng tài khoản test không bật 2FA
- Login bị rate limit theo username: sai nhiều lần liên tiếp sẽ bị chặn tạm
- Không có endpoint đăng ký — dùng tài khoản có sẵn người dùng cung cấp

## DB

Postgres dùng chung cho cả local và staging, biến `STAGING_DB_*` trong `test-env/.env.dbstaging`. Cần seed/kiểm
DB thì nạp thêm file này cùng `.env.staging`:

```bash
set -a; . test-env/.env.staging; . test-env/.env.dbstaging; set +a; <lệnh>
```

## Đánh dấu data test

Tên/tiêu đề tiền tố `[test]`, email `yp-test+<slug>@...`.
