# Test plan — app-api — phân quyền route đọc recording (local, DB staging)

**Ngày**: 2026-10-30 · **Base URL**: http://localhost:8080 · **Bản deploy**: app-api local, nhánh
`feat/teacher-manager-view` (gồm thay đổi chưa commit của YP-1484)
**Tài khoản test**: tài khoản trong `test-env/.env.local` (role Teacher, user id `0fe63cdc-bc92-4090-9903-237b1598aa63`)
**Nguồn kỳ vọng**: checklist YP-1484
**Trạng thái**: xong

app-api local trỏ DB staging, nên lệnh ghi dưới đây ghi lên DB staging.

## 1. Case

| # | Case | Loại | Request | Kỳ vọng |
|---|---|---|---|---|
| N1 | GV thường xem recording GV khác qua route theo GV | cần seed | `GET /v1/recordings/spotlightr/teachers/<GV khác>` | 403 `action_not_allowed` |
| N2 | GV thường xem recording GV khác qua route list | cần seed | `GET /v1/recordings/spotlightr?teacher_id=<GV khác>` | 403 |
| N3 | GV thường xem requirement của GV khác | cần seed | `GET /v1/recordings/spotlightr/requirement?teacher=<GV khác>` | 403 |
| N4 | GV thường xem recording của chính mình | cần seed | `GET /v1/recordings/spotlightr?teacher_id=<chính mình>` | 200 |
| N5 | GV thường gọi list không truyền teacher_id | cần seed | `GET /v1/recordings/spotlightr` | 200, không có recording của GV khác |
| N6 | GV thường lấy danh sách GV | cần seed | `GET /v1/admin/teachers?action_id=TEACHER_CLASS_MANAGER` | 200, chỉ 1 GV là chính mình |

GV khác: `08e21212-900b-42bc-a95b-54d4777b24e8` (có 27 recording).

## 2. Data

**Data có sẵn dùng để test (chỉ đọc)**
| Case | Query tìm | Bản ghi chọn |
|---|---|---|
| N1–N3 | GV trong `GET /v1/admin/teachers?action_id=TEACHER_CLASS_MANAGER` có recording | `08e21212-900b-42bc-a95b-54d4777b24e8` |

**Data cần sửa** (đã duyệt)
| Case | Cách | Bảng | Sửa gì, vì sao |
|---|---|---|---|
| N1–N6 | seed DB | `action_control_list` | Dòng `id = 32018` (`TEACHER_CLASS_MANAGER`, cấp riêng cho tài khoản test): `status` 1 → 0. Không có tài khoản GV thường nào khác để test |

File seed: `seed.sql` · File rollback: `rollback.sql`

## 3. Các bước

1. Login tài khoản test (không đổi data)
2. Chạy `seed.sql`, restart app-api local để nạp lại ACL
3. Gọi N1 … N6
4. Chạy `rollback.sql`, restart app-api local, `SELECT` xác nhận `status = 1`

Server staging nạp lại ACL mỗi 10 phút; bước 2–4 chạy liền một mạch để tài khoản không mất quyền manager trên staging.

## 4. Change log

| Thời điểm | Hành động | Bảng/collection | Id bản ghi | Trước (khi sửa) | Sau |
|---|---|---|---|---|---|
| 2026-10-30 16:49:17 | Chạy `seed.sql` | `action_control_list` | 32018 | `status = 1` | `status = 0` |
| 2026-10-30 16:49:22 | Chạy `rollback.sql` | `action_control_list` | 32018 | `status = 0` | `status = 1` |

## 5. Kết quả

| # | Request | Kỳ vọng | Thực tế | Kết quả |
|---|---|---|---|---|
| N1 | `GET /v1/recordings/spotlightr/teachers/<GV khác>` | 403 | 403 "Không có quyền thực hiện thao tác" | đạt |
| N2 | `GET /v1/recordings/spotlightr?teacher_id=<GV khác>` | 403 | 403 | đạt |
| N3 | `GET /v1/recordings/spotlightr/requirement?teacher=<GV khác>` | 403 | 403 | đạt |
| N4 | `GET /v1/recordings/spotlightr?teacher_id=<chính mình>` | 200 | 200, 0 bản ghi | đạt |
| N5 | `GET /v1/recordings/spotlightr` | 200, không có recording của GV khác | 200, 0 bản ghi | đạt |
| N6 | `GET /v1/admin/teachers?action_id=TEACHER_CLASS_MANAGER` | 200, chỉ chính mình | 200, 1 GV là chính mình | đạt |

Sau rollback: restart app-api local, tài khoản test lấy lại được 60 GV và gọi `/teachers/<GV khác>` trả 200.

### Chạy thêm bằng tài khoản test có sẵn (chỉ đọc, không ghi DB)

Tài khoản `fullnameno.<n>@nomail.com` trên staging, chọn theo role bằng `SELECT` trên `directus_users`.

**End User** (`fullnameno.11`)
| # | Request | Kỳ vọng | Thực tế | Kết quả |
|---|---|---|---|---|
| E1 | `GET /v1/recordings/spotlightr` | 403 | 403 | đạt |
| E2 | `GET /v1/recordings/spotlightr/teachers/<GV khác>` | 403 | 403 | đạt |
| E3 | `GET /v1/recordings/spotlightr/teachers/<chính mình>` | 403 | 403 | đạt |
| E4 | `GET /v1/recordings/spotlightr?teacher_id=<GV khác>` | 403 | 403 | đạt |
| E5 | `GET /v1/recordings/spotlightr/requirement?teacher=<GV khác>` | 403 | 403 | đạt |

**GV thường, không có `TEACHER_CLASS_MANAGER`** (`fullnameno.2`)
| # | Request | Kỳ vọng | Thực tế | Kết quả |
|---|---|---|---|---|
| R1 | `GET /v1/recordings/spotlightr/teachers/<GV khác>` | 403 | 403 | đạt |
| R2 | `GET /v1/recordings/spotlightr?teacher_id=<GV khác>` | 403 | 403 | đạt |
| R3 | `GET /v1/recordings/spotlightr/requirement?teacher=<GV khác>` | 403 | 403 | đạt |
| R4 | `GET /v1/recordings/spotlightr/teachers/<chính mình>` | 200 | 200, 0 bản ghi | đạt |
| R5 | `GET /v1/recordings/spotlightr` | 200, không có recording của GV khác | 200, 0 bản ghi | đạt |
| R6 | `GET /v1/admin/teachers?action_id=TEACHER_CLASS_MANAGER` | Chỉ chính mình | 200, 1 GV | đạt |

**Administrator** (`fullnameno.5`)
| # | Request | Kỳ vọng | Thực tế | Kết quả |
|---|---|---|---|---|
| A1 | `GET /v1/recordings/spotlightr/requirement` (cách CRM gọi khi mở trang) | 200 | 200 | đạt |
| A2 | `GET /v1/recordings/spotlightr/teachers/<GV khác>` | 403 (không có `MANAGE_RECORDINGS`/`TEACHER_CLASS_MANAGER`) | 403 | đạt |

**Chưa kiểm được**: GV xem đúng recording của chính mình khi có dữ liệu — các tài khoản GV test
(`fullnameno.1`–`4`) đều chưa có recording nào.

## 6. Dọn data

Đã chạy `rollback.sql` lúc 16:49:22; `SELECT` xác nhận dòng `32018` có `status = 1`. ACL bị tắt khoảng 5 giây.
