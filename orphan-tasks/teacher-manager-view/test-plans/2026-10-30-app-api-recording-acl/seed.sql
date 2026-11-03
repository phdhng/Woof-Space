-- Tạm tắt ACL TEACHER_CLASS_MANAGER của tài khoản test để chạy case GV thường.
BEGIN;
UPDATE action_control_list SET status = 0 WHERE id = 32018 RETURNING id, action_id, status;
COMMIT;
