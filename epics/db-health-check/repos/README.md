# Linked code-bases

Symlinks tới các code-base liên quan tới epic này, không copy vào workspace.

| Link | Path thật | Ghi chú |
|---|---|---|
| `app-lorvaix` | `/Users/hng.er/Documents/WORK/ielts1984/Backend/app-lorvaix` | Chứa toàn bộ tính năng DB Health Check (`app/services/db_health_check.py`, `db_health_lark_card.py`, `app/cron/db_health_check_job.py`) |

## Thêm code-base mới

```bash
ln -s /absolute/path/to/codebase repos/<ten-code-base>
```

Nếu path nằm ngoài các thư mục đã từng mở trong Claude Code, có thể cần cấp quyền truy cập thư mục đó lần đầu (app sẽ tự hỏi hoặc dùng lệnh cấp quyền thư mục).
