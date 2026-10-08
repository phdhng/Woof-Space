# DB Health Check — Phân tích thủ công - Ngày 06/10

Nguồn: dòng log mới nhất của cron `db_health_check_job`
([db_health_latest.json](db_health_latest.json), 10 dòng đầu mỗi tiêu chí).
Các chỉ số (`calls`, `seq_scan`, `idx_scan`...) là bộ đếm cộng dồn, chưa rõ
cửa sổ thống kê do `stats_reset` đều NULL, nên chỉ dùng để so tương đối giữa
các mục, không quy ra "mỗi ngày".

## Index hiện có trên DB

Kết quả `pg_indexes` của 7 bảng liên quan (32 index) cho thấy:

- **Thiếu index, đã xác nhận:** `processed_transcriptions` không có index trên
  `sub_answer_id` (chỉ có `answer`, `file`, `id`); `student_study_plan` không
  có index trên `user_id` hay `plan_type` (chỉ có `(status, created_at)` và
  `id`). Model khai báo `index=True` cho các cột này nhưng DB không có, vì
  app không tự tạo schema.
- **Đã có sẵn, không cần thêm:** `answer` có `answer_user_created
  (user_created, quiz)`; `pronunciation_corrections` có
  `idx_pron_word_ipa_cov (mispronounced_word, corrected_word_ipa) INCLUDE
  (...)`; `user_vocab_category` có index `(user_id)`.
- **Có index nhưng không dùng được cho query nóng:** `user_vocab_bank` có
  `idx_user_vocab_active` nhưng là partial `WHERE status = 1`, trong khi query
  lọc `status != $2`; `notification` không có index `(user_id, id)` nào cho
  điều kiện `push_id IS NOT NULL`.
- **Index trùng lặp:** `idx_answer_review_ccnew` giống hệt `idx_answer_review`
  (cùng `(review)`); `idx_pronunciation_corrections_word_ipa` bị
  `idx_pron_word_ipa_cov` bao trùm hoàn toàn (cùng 2 cột đầu, cái sau còn
  `INCLUDE` thêm cột).

Mọi `CREATE/DROP INDEX` dùng `CONCURRENTLY` và theo cách của repo: viết
thành file migration SQL, chạy tay trên PostgreSQL.

## Mục 1: Query tốn tài nguyên nhất

10 query nặng nhất tổng ~64 giờ thực thi cộng dồn, gom thành 6 nhóm theo mức
ưu tiên.

### 1.1 `processed_transcriptions` theo `sub_answer_id` — ưu tiên cao nhất

```sql
SELECT * FROM "public"."processed_transcriptions"
WHERE sub_answer_id IN ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
```

**Tình trạng:** 14,454 lần gọi, mỗi lần ~6 giây, cache hit 1.45%, tổng ~24
giờ (gấp 3.3 lần query đứng thứ hai). Lấy 19 bản ghi theo ID mà mất 6 giây,
gần như toàn bộ phải đọc từ đĩa, nên đây là vấn đề hệ thống chứ không phải
cache-miss ngẫu nhiên. **Nguyên nhân đã xác nhận: bảng không có index trên
`sub_answer_id`**, nên mỗi lần gọi quét cả bảng ~2.18M dòng (bảng còn có
335,784 dead tuple, 13.33%, chưa từng autovacuum, xem Mục 2). Query không có
trong `app-lorvaix`, nhiều khả năng đến từ service khác.

**Các bước xử lý:**
1. Tạo index (bảng 2.18M dòng, nên chạy ngoài giờ cao điểm):
   ```sql
   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_processed_transcriptions_sub_answer_id
       ON processed_transcriptions (sub_answer_id);
   ```
2. Chạy `EXPLAIN (ANALYZE, BUFFERS)` với 19 `sub_answer_id` thật để xác nhận
   đã dùng Index Scan, rồi `VACUUM (ANALYZE) processed_transcriptions;`.
3. Ở call site, bỏ `SELECT *` và chỉ lấy cột cần.

### 1.2 `student_study_plan` (2 query, mỗi query ~6M lần gọi)

```sql
-- Q2
SELECT ssp.id, ssp.user_id, ssp.study_plan_id, ssp.status, ssp.end_date,
       COALESCE(ssp.product_id, sp.product_id) AS product_id,
       ssp.purchased_before_onboarding_survey,
       COALESCE(p1.name, p2.name) AS product_name
FROM public.student_study_plan AS ssp
LEFT JOIN public.product AS p1 ON p1.id = ssp.product_id
LEFT JOIN public.study_plan AS sp ON sp.id = ssp.study_plan_id
LEFT JOIN public.product AS p2 ON p2.id = sp.product_id
WHERE ssp.user_id = $1 AND ssp.plan_type = $2
  AND (ssp.end_date IS NULL OR ssp.end_date >= CURRENT_DATE)
  AND ssp.status IN ($3,$4,$5,$6,$7)
ORDER BY ssp.id DESC

-- Q4
SELECT "id","status" FROM "public"."student_study_plan"
WHERE user_id = $1 AND plan_type = $2
ORDER BY id DESC,"student_study_plan"."id" LIMIT $3
```

**Tình trạng:** Mỗi query ~6.04M lần gọi (hai query gọi cùng nhau trong 1
luồng), tổng ~13.4 giờ. Từng lần chỉ 3.6–4.4ms và cache 100%, nhưng trung
bình trả về rất ít dòng (Q2 0.03 dòng/lần, Q4 0.11 dòng/lần), nghĩa là phần
lớn lần gọi trả rỗng. Model
[student_study.py:40-55](../../../repos/app-lorvaix/app/models/student_study.py:40)
khai báo `index=True` cho `user_id` và `plan_type`, nhưng **trên DB không có
index nào trên 2 cột này** (chỉ có `(status, created_at)` và `id`), nên cả
2 query đều không có index để dùng, mỗi lần phải quét bảng. Cả hai query đều
không có trong `app-lorvaix`.

**Các bước xử lý:**
1. Index composite phục vụ cả 2 query, bỏ bước sort:
   ```sql
   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_student_study_plan_user_type_id
       ON student_study_plan (user_id, plan_type, id DESC);
   ```
2. Giảm tần suất ở call site: nếu 1 request gọi nhiều lần cho cùng `user_id`
   thì memoize trong request; nếu phần lớn user không có plan thì cache kết
   quả "không có" ngắn hạn.
3. Sau khi tạo index, `EXPLAIN (ANALYZE, BUFFERS)` Q2 để xác nhận dùng index
   mới và 3 JOIN không quét cả bảng `study_plan`/`product`.

### 1.3 `pronunciation_corrections` DISTINCT (3 query cùng dạng)

```sql
-- Q6 (Q8 và Q10 chỉ khác danh sách từ trong IN; Q10 dùng $1..$17)
SELECT DISTINCT pronunciation_corrections.mispronounced_word AS pronunciation_corrections_mispronounced_word,
       pronunciation_corrections.corrected_word_ipa AS pronunciation_corrections_corrected_word_ipa,
       pronunciation_corrections.corrected_word_audio AS pronunciation_corrections_corrected_word_audio
FROM pronunciation_corrections
WHERE pronunciation_corrections.mispronounced_word IN ('big', 'quite', 'happen', 'if', 'think', 'yes', 'vehicles', 'don''t', 'green', 'makes', 'because', 'space', 'cities', 'is', 'pollute')
```

**Tình trạng:** Tổng 1,343 lần gọi, ~8.4 giờ, mỗi lần ~22–24 giây và trả
**~323k–379k dòng**. Hai lỗi cộng lại:
- Chỉ lọc theo từ, **không có điều kiện `corrected_word_ipa`**, nên các từ
  phổ biến (`the`, `think`, `it's`...) kéo về toàn bộ lịch sử TTS của từ đó.
- `DISTINCT` trên cả 3 cột vô tác dụng vì `corrected_word_audio` là uuid sinh
  mới mỗi lần TTS
  ([pronunciation_audio.py:80-95](../../../repos/app-lorvaix/app/services/pronunciation_audio.py:80)),
  không bao giờ trùng.

Hệ quả: index `idx_pron_word_ipa_cov` (1,036 MB) bị đọc ~25k entry/lần scan.

Code hiện tại trong repo
([speaking_pipeline.py:1463-1476](../../../repos/app-lorvaix/app/services/marking/speaking_pipeline.py:1463))
lọc theo cặp `tuple_(word, ipa).in_(pairs)` với tham số `$n`, còn Q6/Q8 nhúng
giá trị thẳng vào câu (`'don''t'`). Vậy 3 câu này đến từ call site khác, chưa
xác định được nguồn.

**Các bước xử lý:**
1. Xác định call site (log truy vấn kèm `application_name`, hoặc
   `pg_stat_activity` lúc chạy).
2. Dù ở đâu, sửa thành lọc theo cặp và `DISTINCT ON`; số dòng trả về giảm từ
   ~300k+ xuống bằng số cặp:
   ```sql
   SELECT DISTINCT ON (mispronounced_word, corrected_word_ipa)
          mispronounced_word, corrected_word_ipa, corrected_word_audio
   FROM pronunciation_corrections
   WHERE (mispronounced_word, corrected_word_ipa) IN (($1, $2), ($3, $4), ...)
   ORDER BY mispronounced_word, corrected_word_ipa, id DESC;
   ```
   Nếu call site là SQLAlchemy như đoạn trên, chỉ cần thay `.distinct()` bằng
   `.distinct(PronunciationCorrection.mispronounced_word,
   PronunciationCorrection.corrected_word_ipa)` kèm `.order_by(word, ipa,
   PronunciationCorrection.id.desc())`.
3. Dùng tham số `$n` thay vì nhúng giá trị (Q6, Q8), tránh mỗi bộ từ tạo 1
   queryid riêng trong `pg_stat_statements`.
4. Team Speaking xác nhận trước khi sửa, vì việc chọn bản ghi nào cho mỗi cặp
   (mới nhất thay vì bất kỳ) liên quan business logic chấm điểm.

### 1.4 `answer` DISTINCT quiz

```sql
SELECT distinct quiz FROM "public"."answer" WHERE user_created = $1
```

**Tình trạng:** 1.19M lần gọi, mean 21.72ms, cache 84.94%, tổng ~7.2 giờ,
mỗi lần lấy ~105 dòng rồi DISTINCT. Index `answer_user_created` đã là
`(user_created, quiz)` (1,109 MB, 299M lượt scan), tức đã đủ cột để chạy
Index-Only Scan, không cần thêm index. Nhưng Index-Only Scan chỉ bỏ được việc
đọc heap khi visibility map được cập nhật, mà bảng `answer` có 222,521 dead
tuple (23.63%) và chưa từng autovacuum/autoanalyze, nên Postgres vẫn phải đọc
heap cho từng dòng (cache hit 84.94% là hệ quả).

**Phương án:** Không cần đổi index; vacuum bảng `answer` (Mục 2) rồi đo lại
mean của query này.

### 1.5 `user_vocab_bank` (2 query)

```sql
-- Q5
SELECT category as group_field, COUNT(*) as count FROM "public"."user_vocab_bank"
WHERE user_id = $1 AND status != $2 GROUP BY "category"

-- Q9
SELECT uvb.category,
       COUNT(*) as total_words,
       SUM(CASE WHEN uvb.learning_status = $3 THEN $4 ELSE $5 END) as mastered,
       SUM(CASE WHEN uvb.learning_status = $6 THEN $7 ELSE $8 END) as partially_remembered,
       SUM(CASE WHEN uvb.learning_status = $9 OR uvb.learning_status IS NULL THEN $10 ELSE $11 END) as not_mastered,
       MAX(uvc.sort) as sort
FROM user_vocab_bank uvb
LEFT JOIN user_vocab_category uvc ON uvc.ref_id = uvb.category AND uvc.user_id = uvb.user_id
WHERE uvb.user_id = $1 AND uvb.status != $2
GROUP BY "uvb"."category"
```

**Tình trạng:** Q5 264,717 lần gọi, mean 73.78ms, cache chỉ 49.82%; Q9
106,051 lần, mean 82.16ms. Tổng ~7.9 giờ, mỗi lần chỉ trả 4–8 dòng. Bảng có
381,898 dòng live nhưng **250,837 dead tuple (39.64%)** và chưa từng
vacuum/analyze. Index `idx_user_vocab_bank_user_created` nặng 562 MB (quá
lớn cho bảng 382k dòng, gợi ý bloat) nhưng chỉ có 72 lượt scan, nghĩa là
các query nóng này không dùng nó. Index partial `idx_user_vocab_active
(user_id, category, learning_status) WHERE status = 1` có đủ cột nhưng
Postgres không dùng được vì query lọc `status != $2` chứ không phải `status =
1`. Cả hai query không có trong `app-lorvaix`.

**Các bước xử lý:**
1. Vacuum và reindex trước (ít rủi ro), rồi đo lại:
   ```sql
   VACUUM (ANALYZE, VERBOSE) user_vocab_bank;
   REINDEX INDEX CONCURRENTLY idx_user_vocab_bank_user_created;
   ```
2. Nếu vẫn quét rộng, thêm index covering (cả 2 query lọc `user_id`, gom
   theo `category`, chỉ đọc `status` và `learning_status`):
   ```sql
   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_vocab_bank_user_category_cov
       ON user_vocab_bank (user_id, category) INCLUDE (status, learning_status);
   ```
   Index covering chỉ cho Index-Only Scan khi bảng được vacuum thường xuyên.
   Q9 còn JOIN `user_vocab_category`, bảng này đã có index `(user_id)` nên
   không cần thêm.

### 1.6 `notification` polling

```sql
SELECT * FROM "notification"
WHERE user_id = $1 AND push_id IS NOT NULL AND id > $2 AND created_at > $3
ORDER BY id ASC LIMIT $4
```

**Tình trạng:** 1.24M lần gọi, mean 9.01ms, cache 100%, tổng ~3.1 giờ,
**toàn bộ các lần đều trả 0 dòng**: pattern polling hỏi "có thông báo push
mới không". Các index hiện có của bảng đều không khớp: `(user_id, status,
created_at DESC)` không có `id` để duyệt theo `ORDER BY id`, `idx_notification_push_id`
chỉ theo `push_id`.

**Phương án:** Partial index nhỏ để trả rỗng gần như tức thì; triệt để hơn là
giảm tần suất polling hoặc chuyển sang cơ chế đẩy (queue/pub-sub) tuỳ thiết
kế client/worker:
```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_notification_user_push_pending
    ON notification (user_id, id) WHERE push_id IS NOT NULL;
```

## Mục 2: Bảng cần VACUUM nhất

Autovacuum **có chạy** trên instance (nhiều bảng có `last_autoanalyze` trong
vài ngày qua), nhưng 10 bảng nhiều dead tuple nhất chia thành 2 nhóm.

**Nhóm A — dead tuple vượt ngưỡng autovacuum mặc định (50 + 20% × live)
nhưng chưa từng được vacuum/analyze:**

| Bảng | dead / live | % dead | Ngưỡng ~ |
|---|---|---|---|
| user_vocab_bank | 250,837 / 381,898 | 39.64% | 76k |
| answer | 222,521 / 719,216 | 23.63% | 144k |
| directus_files | 152,767 / 172,042 | 47.03% | 34k |
| user_device_log | 103,824 / 96,571 | 51.81% | 19k |

Đã vượt ngưỡng nhiều lần mà cả autovacuum lẫn autoanalyze đều chưa chạm tới,
nghĩa là planner đang dùng statistics cũ cho các bảng nóng như `answer`
(299M lượt index scan). Cần tìm nguyên nhân chung:
```sql
-- giao dịch giữ xmin lâu, khiến vacuum không dọn được
SELECT pid, state, xact_start, now() - xact_start AS age, left(query, 80)
FROM pg_stat_activity WHERE xact_start IS NOT NULL
ORDER BY xact_start LIMIT 10;

-- autovacuum bị tắt riêng cho bảng
SELECT relname, reloptions FROM pg_class
WHERE relname IN ('answer','user_vocab_bank','directus_files','user_device_log')
  AND reloptions IS NOT NULL;

-- cấu hình worker
SELECT name, setting FROM pg_settings
WHERE name IN ('autovacuum','autovacuum_max_workers','autovacuum_naptime',
               'autovacuum_vacuum_cost_limit','autovacuum_vacuum_scale_factor');
```

Dọn ngay (ngoài giờ cao điểm), bảng nhỏ trước. `processed_transcriptions`
thuộc Nhóm B nhưng tác động trực tiếp tới query 1.1 nên đưa vào luôn:
```sql
VACUUM (ANALYZE, VERBOSE) user_device_log;
VACUUM (ANALYZE, VERBOSE) user_vocab_bank;
VACUUM (ANALYZE, VERBOSE) directus_files;
VACUUM (ANALYZE, VERBOSE) answer;
VACUUM (ANALYZE, VERBOSE) processed_transcriptions;
```

**Nhóm B — bảng lớn, dead nhiều về số tuyệt đối nhưng dưới ngưỡng 20%** (chưa
được vacuum vì chưa tới ngưỡng, không phải lỗi cấu hình):

| Bảng | dead / live | % dead | Ngưỡng ~ |
|---|---|---|---|
| student_study_item | 1,108,469 / 6,880,619 | 13.87% | 1.38M |
| student_daily_progress | 954,458 / 11,447,702 | 7.70% | 2.29M |
| processed_transcriptions | 335,784 / 2,184,126 | 13.33% | 437k |
| user_device | 319,229 / 1,867,493 | 14.60% | 374k |
| review | 246,110 / 2,407,433 | 9.27% | 482k |
| directus_users | 147,392 / 768,014 | 16.10% | 154k |

Bảng lớn hàng triệu dòng phải tích luỹ hàng trăm nghìn dead tuple mới tới
ngưỡng 20%, đủ để kéo cache hit và hiệu năng index xuống (VD
`processed_transcriptions` ở 1.1). **Phương án:** hạ scale factor riêng cho
các bảng do `app-lorvaix` quản lý, đưa vào migration (`user_device` cần xác
nhận bảng thuộc service nào; bảng Directus trao đổi với team phụ trách).
0.03–0.05 là điểm khởi đầu, chỉnh lại sau 1–2 tuần theo dõi báo cáo cron:
```sql
ALTER TABLE student_study_item SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE student_daily_progress SET (autovacuum_vacuum_scale_factor = 0.03, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE processed_transcriptions SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE user_device SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
```

## Mục 3: Index Bloat & Sequential Scan

### 3.1 Index lớn nhất

10 index lớn nhất tổng ~14.0 GiB, trong đó 4 index của `success_quiz_log`
chiếm ~6.6 GiB. Các điểm cần chú ý:
- **`idx_success_quiz_log_passage_id`** (1,736 MB) và 2 index trigram của
  `directus_files` (1,257 MB + 1,202 MB): `idx_scan` = 0, chưa từng dùng.
  Mỗi lần ghi vào `directus_files` phải cập nhật cả 2 GIN index này, trong
  khi bảng đang có 47.03% dead tuple. Phương án drop ở Mục 4.1.
- **`success_quiz_log_pkey`** (1,736 MB): chỉ 539 lượt scan nhưng đọc
  ~15.9 tỷ entry (~29.5M entry/scan), tức có job (thống kê, export,
  dashboard...) đang quét gần toàn bộ index PK. Cần truy nguồn bằng
  `pg_stat_activity` hoặc log truy vấn chậm trong lúc job chạy.
- **`idx_pron_word_ipa_cov`** (1,036 MB): mỗi scan đọc ~25k entry, là hệ quả
  của query 1.3.
- **`success_quiz_log_dashboard`** (1,539 MB): 21,504 scan, mỗi scan đọc 1
  entry, dùng ít so với dung lượng.
- Các index còn lại (`success_quiz_log_answer_idx`, `draft_answer_pkey`,
  `answer_user_created`, `answer_user_id_idx`) đang được dùng nhiều, ổn.

### 3.2 Bảng bị seq scan nhiều

4 bảng (`registration_request_tag_search`, `directus_permissions`,
`otp_attempt`, `entrance_test_comment`) có số tuple đọc mỗi lần seq scan ≈
số dòng live: quét cả bảng nhỏ rẻ hơn dùng index, hợp lý, không cần xử lý.

6 bảng còn lại đọc nhiều hơn số dòng live rất nhiều mỗi lần quét:

| Bảng | Dòng live | Tuple đọc / lần seq scan | Gấp | Số lần seq scan |
|---|---|---|---|---|
| student_activity | 699,511 | 15.6M | 22.4 | 271 (idx_scan = 0) |
| order_items | 2,351 | 68,902 | 29.3 | 45,264 |
| practice_flow | 575 | 9,710 | 16.9 | 391,996 |
| real_test_result | 2,452 | 37,180 | 15.2 | 21,879 |
| activity_log | 6,331 | 94,399 | 14.9 | 86,138 |
| pronunciation_practice_result | 9,977 | 81,159 | 8.1 | 12,485 |

Với 5 bảng nhỏ, đọc gấp 8–30 lần số dòng live là dấu hiệu bảng phình vật lý
do dead tuple chưa dọn, hoặc `n_live_tup` đã cũ vì chưa analyze (nếu bloat thì
vacuum sẽ giảm mạnh số tuple phải đọc). `student_activity` khác: bảng ít được
truy cập (271 lần, chưa từng dùng index) nhưng mỗi lần quét rất nặng; nên xác
nhận với team sở hữu bảng còn dùng không.

**Phương án:** So kích thước vật lý với số dòng live để xác nhận bloat, vacuum
nếu cần (`VACUUM FULL` hoặc `pg_repack` ngoài giờ nếu bloat nặng và cần trả
dung lượng):
```sql
SELECT relname,
       pg_size_pretty(pg_relation_size(relid)) AS table_size,
       n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE relname IN ('activity_log','practice_flow','order_items',
                  'pronunciation_practice_result','real_test_result',
                  'student_activity');
```

## Mục 4: Hiệu quả Index

### 4.1 Index chưa từng dùng (~5.2 GiB)

Cả 10 index trong danh sách đều `idx_scan` = 0. Hơn 94% dung lượng nằm ở 5
index, nên chỉ cần xử lý 5 index này:

| Index | Bảng | Size | Ghi chú |
|---|---|---|---|
| idx_success_quiz_log_passage_id | success_quiz_log | 1,736 MB | |
| idx_directus_files_filename_download_trgm | directus_files | 1,257 MB | Directus |
| idx_directus_files_title_trgm | directus_files | 1,202 MB | Directus |
| idx_answer_date_updated | answer | 587 MB | |
| idx_answer_review_ccnew | answer | 256 MB | Trùng định nghĩa `(review)` với `idx_answer_review`; hậu tố `_ccnew` là dấu vết `REINDEX/CREATE INDEX CONCURRENTLY` còn sót lại |

5 index còn lại đều ≤ 185 MB; `idx_raw_vocab_raw_data_gin` (GIN trên JSON) có
thể phục vụ truy vấn admin hiếm gặp nên hỏi team sở hữu trước khi đụng tới.

**Phương án:** Kiểm tra trước, rồi drop theo thứ tự rủi ro tăng dần (lưu lại
`indexdef` trước khi drop để tạo lại được khi cần):
```sql
SELECT c.relname AS index_name, i.indisvalid, i.indisunique, i.indisprimary
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relname IN ('idx_answer_review_ccnew','idx_answer_date_updated',
  'idx_success_quiz_log_passage_id',
  'idx_directus_files_filename_download_trgm',
  'idx_directus_files_title_trgm');

-- index trùng với idx_answer_review (0 lượt scan): an toàn
DROP INDEX CONCURRENTLY IF EXISTS idx_answer_review_ccnew;
-- sau khi xác nhận không có job định kỳ (tuần/tháng) nào dùng:
DROP INDEX CONCURRENTLY IF EXISTS idx_answer_date_updated;
DROP INDEX CONCURRENTLY IF EXISTS idx_success_quiz_log_passage_id;
-- Directus, trao đổi team phụ trách trước:
DROP INDEX CONCURRENTLY IF EXISTS idx_directus_files_filename_download_trgm;
DROP INDEX CONCURRENTLY IF EXISTS idx_directus_files_title_trgm;
```

### 4.2 Index hiệu quả thấp

Phần lớn 10 dòng là PK/index nhỏ ≤ 6 MB, ít lượt scan, không đáng quan tâm.
Chỉ có 3 index đáng chú ý:

| Index | Bảng | Size | Mỗi scan đọc → lấy | Nhận xét |
|---|---|---|---|---|
| idx_answer_quiz_type_user_created_type | answer | 315 MB | ~956 → ~1.5 entry | 3.77M scan; điều kiện lọc không tận dụng cột đầu của index |
| answer_type | answer | 300 MB | ~1,257 → ~2.5 entry | Chọn lọc thấp (cột `type` ít giá trị) |
| dcoin_transaction_pkey | dcoin_transaction | 42 MB | ~548k → ~102 entry | 496 scan quét gần toàn bộ PK để lấy rất ít dòng |

**Xử lý theo từng index:**
- `idx_answer_quiz_type_user_created_type` có thứ tự cột `(quiz_type,
  user_created, type)` và `answer_type` là `(type)`. Cột đầu là `quiz_type`
  nên query chỉ lọc `user_created`/`type` mà không lọc `quiz_type` sẽ phải đọc
  rất nhiều entry (~956/scan). Cần xác định query dùng index này (3.77M scan)
  rồi mới chốt đổi thứ tự cột (VD `(user_created, quiz_type, type)` nếu
  query luôn lọc `user_created`):
   ```sql
   SELECT queryid, calls, left(query, 300) AS query
   FROM pg_stat_statements
   WHERE query ILIKE '%answer%' AND query ILIKE '%quiz_type%'
   ORDER BY calls DESC LIMIT 5;
   ```
- `answer_type`: chọn lọc thấp (cột `type` ít giá trị) và `type` đã nằm trong
  index trên; cân nhắc drop sau khi kiểm tra không query nào cần.
- `dcoin_transaction_pkey`: chưa có hành động cụ thể, cần truy nguồn các scan
  nặng như đã nêu cho `success_quiz_log_pkey` (Mục 3.1).

### 4.3 Index trùng lặp

Hai trường hợp phát hiện từ danh sách index hiện có:
- `idx_answer_review_ccnew` trùng `idx_answer_review`: đã nằm trong lệnh drop
  ở 4.1.
- `idx_pronunciation_corrections_word_ipa (mispronounced_word,
  corrected_word_ipa)` bị `idx_pron_word_ipa_cov` (cùng 2 cột đầu, thêm
  `INCLUDE`) bao trùm; nên drop index nhỏ hơn để giảm chi phí ghi, sau khi
  xác nhận cả hai không bị ràng buộc nào phụ thuộc:
   ```sql
   DROP INDEX CONCURRENTLY IF EXISTS idx_pronunciation_corrections_word_ipa;
   ```

## Tổng hợp ưu tiên

| # | Việc | Hành động đầu tiên | Chủ sở hữu |
|---|---|---|---|
| 1 | `processed_transcriptions IN (...)` 6s/lần, thiếu index `sub_answer_id` (1.1) | Tạo index, vacuum | Backend |
| 2 | `student_study_plan` ~12M lần gọi, hầu hết rỗng, thiếu index `user_id` (1.2) | Index composite, memoize/cache | Backend |
| 3 | Autovacuum bỏ sót 4 bảng vượt ngưỡng (Mục 2) | Kiểm tra long transaction, `reloptions`, cấu hình worker | DBA/Infra |
| 4 | `pronunciation_corrections` trả 300k+ dòng/lần (1.3) | Xác định call site, lọc theo cặp, `DISTINCT ON` | Team Speaking |
| 5 | `user_vocab_bank` bloat 39.64% (1.5) | `VACUUM (ANALYZE)`, `REINDEX CONCURRENTLY` | Backend |
| 6 | Index chưa dùng ~5.2 GiB và index trùng lặp (4.1, 4.3) | Drop dần, bắt đầu từ `idx_answer_review_ccnew` | Backend + Directus |
| 7 | `notification` polling 100% rỗng (1.6) | Partial index / giảm tần suất | Backend |
| 8 | Bảng seq scan đọc gấp 8–30 lần dòng live (3.2) | So `pg_relation_size`, vacuum | Backend |
| 9 | Scan lớn trên `success_quiz_log_pkey`, `dcoin_transaction_pkey` | Truy nguồn job/query định kỳ | Backend |

**Cần bổ sung để chốt kết luận:** kết quả `EXPLAIN` sau khi tạo index ở 1.1
và 1.2; call site của 3 query ở 1.3; query dùng
`idx_answer_quiz_type_user_created_type` (4.2).
