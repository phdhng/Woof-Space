# Tặng YP Pro dùng thử cho user đăng ký mới

## 1. Bối cảnh

Marketing thử nghiệm ngày 19/10/2026: user đăng ký mới trong ngày 19/10 được bật PRO đến hết 22/10 (giờ VN), tất cả hết hạn cùng một mốc. Marketing có một trang giới thiệu tính năng PRO để đo tỉ lệ chuyển đổi so với user không được bật PRO từ đầu.

Phạm vi:

- **BE (làm trước):** tự cấp PRO cho user mới và tạo thông báo nhận gói (trả về qua `GET /v1/notifications`).
- **FE (để sau):** pop-up chúc mừng dẫn sang trang của Marketing.

**Chỉ áp dụng cho đăng nhập Google.** Trên web chỉ có một nút "Đăng nhập bằng Google", và hệ thống không tách đăng ký với đăng nhập. User mới là user được tạo trong lần đăng nhập Google đầu tiên.

## 2. Hiện trạng liên quan trong app-api

- `LoginGoogle` (`internal/services/user.go`) tìm user theo email. Nếu chưa có thì tạo mới và đặt `isNewUser = true`. Biến này đúng bằng "lần tạo user đầu tiên", nên chỉ cấp PRO ở nhánh này. Các lần đăng nhập sau không chạy lại.
- Luồng mua PRO hiện tại (`CreateSubscriptionFromOrder`) gọi loyalty service (`POST /internal/v1/subscriptions`). Loyalty insert `user_subscription`, rồi set `directus_users.user_type = product.category` nếu `item_type = subscription`. Hai bước này không nằm trong transaction.
- `user_subscription.order_id` có FK sang `orders(id)` và cho phép NULL. Model của app-api và loyalty đều để `OrderID int`, nên gọi loyalty khi không có đơn sẽ gửi `order_id = 0` và vi phạm FK.
- Hạn subscription khi mua được tính bằng `common.GetEndOfDayUTC(startedAt).AddDate(0, 0, item_quantity)`, tức 23:59:59 giờ VN của ngày bắt đầu, cộng thêm `item_quantity` ngày.
- Cron `update_expired_user_subscription` chạy lúc 17:05 UTC (00:05 giờ VN) mỗi ngày. Với user có `user_type` khác NULL mà không còn subscription `active` trong hạn, cron đặt `user_type` về NULL. PRO dùng thử sẽ tự gỡ lúc 00:05 ngày 23/10, không cần thêm logic hết hạn.

## 3. Giải pháp

### Cấu hình

Thêm product mới cho gói dùng thử, giống các product PRO 1 tháng và 3 tháng: `item_type = subscription`, `category = youpass_pro`, `item_quantity = 3`.

Thêm bản ghi `master_config` có `public_id = SIGNUP_PRO_TRIAL`:

```json
{
  "enabled": true,
  "product_id": 123,
  "signup_from": "2026-10-18T17:00:00Z",
  "signup_to": "2026-10-19T16:59:59Z"
}
```

- `signup_from` và `signup_to` phải đúng định dạng RFC3339 và ghi theo UTC, ứng với 00:00:00 đến 23:59:59 ngày 19/10 giờ VN. Khoảng này tính cả hai đầu mút. Ghi sai định dạng (vd `"2026-10-19"`) thì không cấp và mỗi user mới sinh một dòng log `subscription.signup_trial.failed`.
- Không cần trường `category`, vì `user_type` lấy theo `product.category`, giống luồng mua PRO.
- Config được cache Redis 15 phút (`GetConfigByPublicID`). Sửa config, kể cả tắt `enabled`, có thể mất đến 15 phút mới có hiệu lực, trừ khi xoá key `master_config_SIGNUP_PRO_TRIAL`.

### Luồng xử lý

Trong `LoginGoogle`, sau khi tạo user mới:

1. Đọc config `SIGNUP_PRO_TRIAL`. Nếu config không có, `enabled = false`, hoặc thời điểm hiện tại nằm ngoài `[signup_from, signup_to]` thì bỏ qua.
2. Lấy product theo `product_id`.
3. Tính hạn: `GetEndOfDayUTC(now).AddDate(0, 0, product.item_quantity)`. Mọi user đăng ký trong ngày 19/10 giờ VN đều hết hạn lúc 22/10 23:59:59 giờ VN (22/10 16:59:59 UTC), dù đăng ký đầu hay cuối ngày.
4. Trong một transaction:
   - insert `user_subscription`: `item_id = product_id`, `item_type = subscription`, `status = active`, `started_at = now`, `expires_at` theo bước 3, `order_id = NULL`, `meta = {"source": "signup_pro_trial"}`;
   - cập nhật `directus_users.user_type = product.category`;
   - insert `notification` riêng cho user (xem mục Thông báo).
5. Lỗi ở bất kỳ bước nào chỉ ghi log `Error`, không chặn đăng nhập. Lỗi ở bước 2 hoặc 4 (user đủ điều kiện nhưng cấp PRO thất bại) thì báo thêm Lark qua `LarkNotificationWebhookURL`, kèm `user_id` và lỗi. Lark gửi bất đồng bộ để không làm chậm đăng nhập. Lỗi đọc config chỉ ghi log.

Ghi thẳng DB trong app-api thay vì gọi loyalty, để các thao tác nằm trong một transaction và ghi được `order_id = NULL`. User hoặc nhận đủ PRO kèm thông báo, hoặc không nhận gì.

### Thông báo

`GET /v1/notifications` chỉ đọc bảng `notification` (noti chung `is_public = true`, hoặc noti riêng theo `user_id`; `status = 1`; `expired_at` NULL hoặc chưa tới). Nội dung để cứng trong `common/const.go` (`SignupProTrialNotification*`), vì sẽ xoá sau đợt thử nghiệm:

| Field | Giá trị |
| --- | --- |
| `title` | HOT: Trải nghiệm Luyện đề PRO miễn phí trong 3 ngày |
| `short_description` | Câu chúc mừng, text thường |
| `description` | HTML, gồm cả hạn "23:59 Thứ Năm 22/10/2026" |
| `action` | `MORE`; FE hiện nút "Tìm hiểu thêm" (FE map chữ nút theo danh sách cố định `EXERCISE`/`MORE`/`JOIN`) |
| `url` | Trang trải nghiệm PRO của Marketing, kèm tham số tracking |
| `is_public` / `user_id` / `status` | `false` / user được cấp / `1` |
| `expired_at`, `icon`, `thumbnail` | để trống; FE dùng ảnh mặc định |

Hạn trong nội dung để cứng, nên chỉ đúng khi khung đăng ký là đúng ngày 19/10 giờ VN.

### Mốc thời gian (giờ VN)

| Đăng ký lúc | `started_at` | `expires_at` | Cron gỡ PRO |
| --- | --- | --- | --- |
| 19/10 00:00:30 | 19/10 00:00:30 | 22/10 23:59:59 | 23/10 00:05 |
| 19/10 23:59:30 | 19/10 23:59:30 | 22/10 23:59:59 | 23/10 00:05 |
| 18/10 23:59:30 hoặc 20/10 00:00:30 | không cấp | | |

## 4. Kiểm tra trên staging

- Đặt `signup_from`/`signup_to` bao quanh thời điểm test, đăng nhập Google bằng tài khoản mới. Kiểm tra có bản ghi `user_subscription` với `expires_at` = 16:59:59 UTC của ngày (hôm nay giờ VN + `item_quantity`), `order_id` NULL, và `directus_users.user_type = youpass_pro`.
- Kiểm tra mốc đầu và cuối ngày: đặt `signup_to` lùi về quá khứ thì user mới không được cấp. Dùng một tài khoản đăng ký sau 00:00 và một tài khoản đăng ký trước 07:00 giờ VN để chắc ngày VN khác ngày UTC vẫn ra cùng hạn.
- Đăng nhập lại bằng tài khoản đã được cấp: không sinh thêm bản ghi `user_subscription`.
- `enabled = false`: không cấp (nhớ xoá cache config).
- API trả về user đó là PRO (`GET` profile, subscription đang active).
- `GET /v1/notifications` của user đó có thông báo nhận gói, nút "Tìm hiểu thêm" mở đúng link Marketing.
- Đếm số user đã được cấp: `user_subscription` có `meta->>'source' = 'signup_pro_trial'`. Không dựa vào log `subscription.signup_trial.granted`, vì log `Info` có thể không xuất ở production.

## 5. Việc cần chuẩn bị trước 19/10

- Tạo product dùng thử trên production và điền `product_id` vào config.
- Insert bản ghi `master_config` trên production trước 00:00 ngày 19/10 giờ VN.
- FE: pop-up (task riêng).
