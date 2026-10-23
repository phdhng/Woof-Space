# Task 1403 — Tự động ENROLLED học sinh vào lớp W0

Repo: `app-api`. Task trên YouRa: #1403.

## Yêu cầu

Khi học sinh ghi danh thành công vào lớp chính (`class.class_category = 'main'`) qua
`POST /regis/submit-registration`, hệ thống thêm học sinh vào lớp W0 cùng level, sau đó gửi email
`week0-activation`.

- Lớp W0: `class.class_category = 'preparation'`. Mỗi level (ROOT/TRUNK/BUD/BLOOM) có đúng 1 lớp
  W0; level của lớp lấy qua `course.level`.
- Chỉ cần thêm học sinh vào lớp W0 (1 dòng trong `class_directus_users_2`, `status = 1`,
  `payment_status = 1`) — không tạo điểm danh, learning progress, workbook như lớp chính.
- Lớp `extra` hoặc không phải `main` → không enroll W0.

## Điểm gắn logic

`SubmitRegistration` (`internal/services/crm.go`), sau khi `AssignClassToRegistrationRequest` thành
công. Nhánh không có `ClassID` (waiting list) không enroll W0.

`AssignClassToRegistrationRequest` đã kiểm tra `checkClassLevel`: level của lớp (`course.level`)
phải nằm trong `registration_request.levels`, nếu `levels` rỗng/null thì ghi danh đã bị từ chối
(`ErrInvalidClassLevel`). Vì vậy tới bước W0, level của lớp ghi danh luôn là một level hợp lệ của
học sinh.

## Luồng W0

1. Lớp ghi danh không phải `main` → bỏ qua.
2. Xác định level ghi danh: `registration_request.levels` phải có đúng 1 phần tử (level đó trùng
   `course.level` của lớp ghi danh nhờ `checkClassLevel`). Nhiều hơn 1 level → lỗi, không enroll W0.
3. Tìm lớp `preparation`, `status = published`, có `course.level` = level đó. Không tìm thấy (hoặc tìm thấy hơn 1) → lỗi.
4. Học sinh đã có dòng trong `class_directus_users_2` của lớp W0 này với `status = 1` và
   `payment_status = 1` → bỏ qua (idempotent, chặn cả trigger lặp). W0 khác level không ảnh hưởng;
   học sinh đổi sang lớp khác level thì enroll thêm W0 mới, W0 cũ giữ nguyên.
5. Thêm học sinh vào lớp W0 qua `userClassRepo` (giống `EnrollStudentsToClasses`).
6. Gửi email kích hoạt W0 qua AWS SES, template `week0-activation` (config
   `ielts_84_email.week0_activation_template`), bằng `SendEmailWithTemplateAndSaveTransaction`.
   - Người nhận: email học sinh. CC: `ielts_84_email.cc_addresses` (`enrollment@ielts1984.group`).
   - Template không có biến, data truyền vào rỗng. Tiêu đề và nội dung nằm trong template trên SES;
     template phải có sẵn trên SES đúng account/region của từng môi trường.

## Xử lý lỗi

- Lỗi ở phần W0 (bước 2–5) không làm hỏng luồng ghi danh chính: API vẫn trả thành công, lỗi được
  log và gửi Lark qua webhook riêng (env `LARK_CLASS_MANAGEMENT_WEBHOOK_URL`, config
  `lark_class_management_webhook_url`). Tin nhắn có thể được chuyển tiếp cho EC nên viết tiếng Việt:
  dòng "Lỗi" mô tả lỗi, sau đó danh sách "Thông tin để check lỗi" gồm học sinh (email (id)), link CRM
  ghi danh (`common.URLFormatLinkCRM` + id registration request; domain CRM prod nên link
  chỉ đúng trên prod), lớp ghi danh (tên (id)), level lớp
  ghi danh, levels của ghi danh, lớp W0 (nếu đã tìm thấy).
- Lỗi gửi email (template chưa có trên SES, SES lỗi...): `SendEmailWithTemplateAndSaveTransaction`
  lưu `email_transaction` trạng thái `Failed` kèm lỗi và gửi Lark (`lark_notification_webhook_url`);
  phía gọi chỉ log, không rollback enroll W0, không retry.
- Toàn bộ phần W0 chạy sau khi ghi danh chính xong, không nằm trong transaction của ghi danh.

## Trước khi deploy

- Tạo template SES `week0-activation` trên stg và prod.
- Set env `LARK_CLASS_MANAGEMENT_WEBHOOK_URL` trên từng môi trường.
