-- Khôi phục ACL TEACHER_CLASS_MANAGER của tài khoản test về giá trị trước khi test.
BEGIN;
UPDATE action_control_list SET status = 1 WHERE id = 32018 RETURNING id, action_id, status;
COMMIT;
