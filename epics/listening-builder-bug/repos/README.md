# Linked code-bases

Symlinks tới các code-base liên quan tới epic này, không copy vào workspace.

| Link | Path thật | Ghi chú |
|---|---|---|
| `app-agents-service` | `/Users/hng.er/Documents/WORK/ielts1984/Backend/app-agents-service` | |
| `app-api` | `/Users/hng.er/Documents/WORK/ielts1984/Backend/app-api` | Trả `GET /v1/practice-flows`, `GET /v1/quizzes/{id}` |

## Thêm code-base mới

```bash
ln -s /absolute/path/to/codebase repos/<ten-code-base>
```

Nếu path nằm ngoài các thư mục đã từng mở trong Claude Code, có thể cần cấp quyền truy cập thư mục đó lần đầu (app sẽ tự hỏi hoặc dùng lệnh cấp quyền thư mục).
