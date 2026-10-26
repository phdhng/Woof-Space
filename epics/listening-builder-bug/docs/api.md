# API dùng cho Listening Builder

Các API để sinh lại builder (guided retry) và kiểm tra kết quả trên production.

## Biến

| Biến | Giá trị | Ghi chú |
|---|---|---|
| `{{agents_url}}` | `https://app-agents.youpass.vn` | Service sinh builder (`app-agents-service`) |
| `{{api_url}}` | `https://api.youpass.vn` | API đọc quiz / practice flow (`app-api`) |
| `{{token}}` | Access token đăng nhập admin | Lấy từ header `authorization` khi thao tác ở `https://admin.ielts1984.vn`. Token sống khoảng 30 giờ, không lưu vào file |

Tài khoản dùng token phải có gói YouPass PRO thì `GET /v1/practice-flows` mới trả dữ liệu (không có thì lỗi `you_have_no_subscription`).

## 1. Sinh lại builder theo câu hỏi

`POST {{agents_url}}/v1/builder-batches` với `kind = by_question`.

```bash
curl --location '{{agents_url}}/v1/builder-batches' \
--header 'Authorization: Bearer {{token}}' \
--header 'Content-Type: application/json' \
--data-raw '{"skill":"listening","kind":"by_question","quiz_id":{{quiz_id}},"question_ids":[{{question_id}}]}'
```

- Response `202`: `{"data": {"batch_id": <id>}}`. Việc sinh chạy nền, theo dõi bằng API 3.
- Luôn **xoá builder cũ** của các câu được chọn rồi sinh mới, không có cờ để giữ bản cũ.
- `question_ids` phải thuộc `quiz_id`, câu không thuộc quiz trả `400` kèm danh sách id sai. Có thể truyền nhiều câu của cùng một quiz.
- Gửi trùng (cùng người, cùng quiz, cùng nhóm câu) trong thời gian ngắn thì trả lại `batch_id` của lần trước, không chạy lại.
- Một câu hỏi có thể được gắn vào nhiều quiz (vd question `24192` thuộc quiz 11375, 6583, 6615, 7410) và các quiz này dùng chung một practice flow. Chỉ cần gọi cho một quiz, các quiz còn lại nhận bản mới theo.

## 2. Sinh builder cho cả quiz

`POST {{agents_url}}/v1/builder-batches` với `kind = batch`.

```bash
curl --location '{{agents_url}}/v1/builder-batches' \
--header 'Authorization: Bearer {{token}}' \
--header 'Content-Type: application/json' \
--data-raw '{"skill":"listening","kind":"batch","quiz_ids":[{{quiz_id}}]}'
```

- `quiz_ids` là danh sách, nhận nhiều quiz một lần. Quiz không hợp lệ trả `400` với `details` liệt kê lý do từng quiz.

## 3. Theo dõi lần chạy

```bash
curl --location '{{agents_url}}/v1/builder-batches/{{batch_id}}' \
--header 'Authorization: Bearer {{token}}'
```

- Trả trạng thái, số lượng theo trạng thái (`counts`, `error_counts`) và từng mục (`items[]`, kèm `error_raw` khi lỗi). Chỉ đọc, gọi lặp để chờ xong.

Chạy lại các mục lỗi của một lần chạy:

```bash
curl --location '{{agents_url}}/v1/builder-batches/{{batch_id}}/retry' \
--header 'Authorization: Bearer {{token}}' \
--header 'Content-Type: application/json' \
--data-raw '{"all_failed":true}'
```

- Hoặc chọn mục cụ thể: `{"item_ids":[...]}`.

## 4. Kiểm tra kết quả builder

Practice flow hiện tại của một câu:

```bash
curl --location '{{api_url}}/v1/practice-flows?quiz_id={{quiz_id}}&question_id={{question_id}}' \
--header 'Authorization: Bearer {{token}}'
```

- `data[].flow_id`: flow mới sau khi sinh lại sẽ có id lớn hơn flow cũ.
- `practice_steps[].questions[]`: `audio_start` / `audio_end` (giây trong file audio của Part), `correct_answers` (câu dictation), `explanation`.

Chi tiết quiz kèm transcript (vocab level 2 có `meta.from` / `meta.to` / `meta.speaker`) và `locate_info` của từng câu:

```bash
curl --location '{{api_url}}/v1/quizzes/{{quiz_id}}?included_vocabs=true' \
--header 'Authorization: Bearer {{token}}'
```

- Đối chiếu `audio_start` / `audio_end` của câu dictation với `meta.from` / `meta.to` của transcript để biết dictation đang lấy câu nào.
- `question.locate_info.paragraph_ranges` (paragraph / sentence đánh số từ 1) là vùng đáp án QC gán.
