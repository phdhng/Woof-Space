# Ghi chú / lưu ý

Ghi chú phát sinh trong quá trình điều tra + sửa bug Listening Builder — không phải việc cần làm (đã có task riêng trong you-ra cho phần đó), chỉ là thông tin/quan sát cần lưu lại để tham khảo sau.

## `locate_info.paragraph_ranges` sai vị trí — question_id 23968 (quiz 11912)

Phát hiện khi verify fix MAP_DIAGRAM_LABEL (dùng `question.locate_info.paragraph_ranges` làm nguồn chính thay vì letter thô — xem [research.md](research.md)).

- Question `id=23968`, `question_type=MAP_DIAGRAM_LABEL`, `text="Skateboard ramp"`, `correct_answer="I"`.
- `locate_info.paragraph_ranges[0]` = `{start:{paragraph:9,sentence:0}, end:{paragraph:9,sentence:3}}` — trỏ tới đoạn nói về `"purchased additional space... danger of stray balls... children's playground..."` — nội dung này thuộc map-label **"playground"** (1 câu hỏi khác cùng question_set), **không phải** "Skateboard ramp".
- Câu đúng thật (`"The skateboard ramp is very popular with both younger and older children."`) nằm ở `paragraph=9, sentence=5` (1-indexed) — ngoài range mà `paragraph_ranges[0]` khai báo.
- **Đã loại trừ**: không phải lỗi `vocab_linking.meta` (meta của câu skateboard đúng, khớp chính xác với vị trí thật đang chạy production: `from=308.055` = `true_from=308.055`). Không phải bug code (`_resolve_from_paragraph_ranges` đọc/xử lý đúng field).
- **Kết luận**: lỗi dữ liệu gốc ở `locate_info.paragraph_ranges` — gán nhầm index lúc content/QC author câu hỏi này. Cần đội content/QC sửa lại `paragraph_ranges` cho đúng — ngoài phạm vi sửa bằng code `app-agents-service`.