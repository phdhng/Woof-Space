# DB Health Check — Phân tích thủ công - Ngày 24/09

## Mục 1: Query tốn tài nguyên nhất

### 1.1 `DELETE FROM "public"."directus_files" WHERE id = $1`

**Số liệu:** calls=7, total=12,890ms, mean=1,841ms/lần, cache_hit=40.6%, rows=7

**Tình trạng:** Xoá 1 dòng theo primary key nhưng mất trung bình 1.8s/lần,
cache hit chỉ 40.6% (phần lớn phải đọc từ đĩa) — bất thường cao cho 1 DELETE
theo PK đơn giản. Nguyên nhân thường gặp cho pattern này:
- Có bảng khác tham chiếu FK tới bảng này mà cột FK thiếu index — Postgres
  phải quét bảng đó để kiểm tra ràng buộc trước khi cho xoá.
- Có xử lý dọn dẹp file vật lý (storage/S3) chạy đồng bộ trong cùng
  transaction với DELETE.

**Phương án:**
1. Rà toàn bộ FK trỏ tới bảng này, xác nhận cột FK phía tham chiếu đã có
   index — đây là nguyên nhân phổ biến nhất khiến DELETE chậm.
2. Nếu có xử lý storage/cleanup đồng bộ trong transaction, cân nhắc tách ra
   xử lý bất đồng bộ (queue/background job) để không giữ lock lâu.
3. Chỉ 7 lần gọi nên chưa đủ dữ liệu khẳng định pattern lặp lại — theo dõi
   thêm ở lần chạy tới.

### 1.2 `SELECT ssp.id, ssp.user_id, ... COALESCE(ssp.product_id, sp.product_id) ...`

**Số liệu:** calls=1,913, total=6,515ms, mean=3.41ms/lần, cache_hit=99.99%, rows=127

**Tình trạng:** Join lấy thông tin study plan của user (product_id, end_date,
trạng thái mua trước onboarding survey...) — hiệu năng thực tế đã tốt (mean
3.41ms, cache gần 100%). Lọt vào top "tốn tài nguyên nhất" thuần vì tần suất
gọi cao (1,913 lần), không phải vì bản thân chậm.

**Phương án:**
1. Không cần tối ưu kỹ thuật — chỉ số hiện tại đã hiệu quả.
2. Nếu muốn giảm tổng tải hệ thống, xem lại tần suất gọi thực tế (có đang bị
   gọi lặp không cần thiết trong cùng 1 luồng nghiệp vụ không).

### 1.3 `SELECT * FROM "public"."processed_transcriptions" WHERE sub_answer_id IN (...18...)`

**Số liệu:** calls=1, total=6,486ms, mean=6,486ms (đúng 1 lần), cache_hit=0.07%, rows=18

**Tình trạng:** Batch-fetch 18 bản ghi transcript theo ID — bản thân pattern
`IN (...)` là cách gộp truy vấn hợp lý, không phải anti-pattern. Nhưng
cache_hit chỉ 0.07% (gần như toàn bộ phải đọc từ đĩa) khiến 1 lần gọi mất
6.5s. Với `calls=1`, khó kết luận đây là vấn đề hệ thống hay chỉ 1 lần
cache-miss ngẫu nhiên (dữ liệu ít truy cập, bị đẩy khỏi buffer cache).

**Phương án:**
1. Xác nhận index vật lý trên `sub_answer_id` thực sự tồn tại trên DB (không
   chỉ trong định nghĩa model).
2. Theo dõi ở lần chạy kế tiếp — nếu tái diễn với `calls` tăng và cache_hit
   vẫn thấp mỗi lần, mới đáng đầu tư điều tra `EXPLAIN ANALYZE`.

### 1.4 `SELECT distinct quiz FROM "public"."answer" WHERE user_created = $1`

**Số liệu:** calls=359, total=4,978ms, mean=13.87ms/lần, cache_hit=90.68%,
rows=42,869 (**cộng dồn qua toàn bộ 359 lần gọi**, không phải 1 lần)

**Tình trạng:** Đính chính hiểu lầm dễ gặp: `rows` trong `pg_stat_statements`
là tổng cộng dồn qua tất cả `calls`. 42,869 / 359 ≈ **119 rows/lần** — không
phải 1 user có 42,869 quiz riêng biệt, không phải dấu hiệu bug dữ liệu.
`quiz` là cột Integer thường, không phải JSON/array bị "nổ" ra nhiều dòng.
Hiệu năng: 13.87ms mean, cache 90.68% — không báo động, total cao chủ yếu vì
số lần gọi tương đối lớn.

**Phương án:**
1. Không có gì báo động về hiệu năng hiện tại.
2. Một biến thể khác của query này (#1.9) lọc thêm theo cột `class`, đã có
   index riêng (`answer_class`, xem Mục 4) — nếu cần đọc/filter theo `class`
   từ code Python trong tương lai, cần bổ sung cột này vào model ORM trước.

### 1.5 `SELECT "id","status" FROM "public"."student_study_plan" WHERE user_id = $1 AND plan_type = $2 ORDER BY id DESC LIMIT $3`

**Số liệu:** calls=1,913, total=4,733ms, mean=2.47ms/lần, cache_hit=100%,
rows=312 (cộng dồn → phần lớn lần gọi trả về 0-1 dòng)

**Tình trạng:** Kiểm tra "user này có study plan loại X không" — hiệu năng
đã rất tốt (mean 2.47ms, cache 100%). `total_ms` cao chỉ vì gọi rất nhiều
(1,913 lần, trùng số lần gọi với #1.2 — nhiều khả năng cùng 1 luồng nghiệp
vụ gọi liên tiếp cả 2 query). Index hiện có trên `user_id`/`plan_type` là
2 index đơn lẻ, chưa có composite `(user_id, plan_type, id)`.

**Phương án:**
1. Không cần xử lý gấp — hiệu năng hiện tại đã nhanh.
2. Nếu muốn tối ưu thêm: index composite `(user_id, plan_type, id DESC)` sẽ
   giúp thành index-only scan — ưu tiên thấp vì đã rất nhanh.

### 1.6 `SELECT DISTINCT pronunciation_corrections.mispronounced_word, corrected_word_ipa, corrected_word_audio ...`

**Số liệu:** calls=1, total=3,856ms, mean=3,856ms, cache_hit=0.05%, rows=19,482

**Tình trạng:** Call site: `_compute_map_pronunciation`
(`app/services/marking/speaking_pipeline.py:1463-1476`), bước
MAPPING_PRONUNCIATION trong pipeline chấm Speaking — tra cứu audio đã tổng
hợp (TTS) cho các cặp (từ phát âm sai, phiên âm IPA đã sửa) của 1 answer.
Composite index `(mispronounced_word, corrected_word_ipa)` đã có sẵn
(`migrations/006_pronunciation_corrections_word_ipa_index.sql`) nên index
không phải vấn đề.

Vấn đề nằm ở cách dùng DISTINCT: cột `corrected_word_audio` được sinh mới
bằng `uuid.uuid4()` mỗi lần TTS chạy (`app/services/pronunciation_audio.py:
80-95`) — không bao giờ tái sử dụng/deterministic theo (word, ipa). Vì
`.distinct()` áp dụng trên cả 3 cột (kể cả cột luôn-unique này), DISTINCT
gần như không gom được gì — trả về gần 1 dòng cho mỗi lần TTS lịch sử từng
khớp các cặp (word, ipa) trong batch, thay vì 1 tập nhỏ theo answer đang
chấm. cache_hit 0.05% + 19,482 rows cho 1 lần gọi khớp với giả thuyết này.

**Phương án:**
1. Nếu mục đích là cache audio theo (word, ipa) để khỏi TTS lại — loại
   `corrected_word_audio` khỏi `.distinct()` (dùng `DISTINCT ON
   (mispronounced_word, corrected_word_ipa)` thay vì distinct cả 3 cột).
2. Nếu mỗi lần synthesis thực sự cần audio riêng, nên xem lại có cần
   DISTINCT hay không.
3. Cần đội phụ trách Speaking xác nhận ý đồ thiết kế trước khi sửa vì liên
   quan business logic chấm điểm.

### 1.7 `SELECT * FROM "student_daily_progress" WHERE user_id = $1 AND created_date = $2 ORDER BY "student_daily_progress"."id" LIMIT $3`

**Số liệu:** calls=2,806, total=3,002ms, mean=1.07ms, cache_hit=96.71%, rows=2,730

**Tình trạng:** Truy vấn tiến độ học trong ngày của user — hiệu năng tốt
(mean 1.07ms, cache 96.71%), `total_ms` cao chỉ vì gọi nhiều (hợp lý nếu mỗi
user có 1 dòng progress/ngày, upsert liên tục mỗi lần submit bài).

**Phương án:**
1. Không cần xử lý gấp về mặt DB — chỉ số hiện tại ổn.
2. Nếu muốn giảm tải đọc/ghi tổng, cân nhắc cache "đã có progress hôm nay
   chưa" ở tầng ứng dụng (Redis) thay vì SELECT-rồi-UPDATE/INSERT mỗi lần
   submit — cần cân nhắc trade-off tính nhất quán trước khi làm.

### 1.8 `SELECT * FROM "public"."practice_step" WHERE practice_flow_id = $1 ORDER BY sort ASC`

**Số liệu:** calls=198, total=2,644ms, mean=13.35ms, cache_hit=99.50%, rows=990

**Tình trạng:** Lấy danh sách bước luyện tập của 1 flow — `practice_flow_id`
đã có index. rows=990/198 calls ≈ 5 dòng/lần, hợp lý cho 1 flow. mean
13.35ms hơi cao cho lookup nhỏ như vậy nhưng chưa báo động (cache 99.5%).

**Phương án:**
1. Không cần xử lý gấp.
2. `sort` chưa có index riêng nhưng với ~5 dòng/flow lợi ích gần như không
   đáng kể — không ưu tiên.

### 1.9 `SELECT distinct quiz FROM "public"."answer" WHERE user_created = $1 AND quiz IN ($2,$3,$4,$5) AND class = $6 AND status != $7`

**Số liệu:** calls=6,816, total=2,628ms, mean=0.39ms, cache_hit=97.34%, rows=2,596

**Tình trạng:** Biến thể lọc thêm của #1.4 (thêm `quiz IN(...)`, `class`,
`status != `). Hiệu năng rất tốt: mean 0.39ms, cache 97.34% — total cao
thuần vì `calls` rất lớn (6,816 lần). Cột `class` có index riêng
(`answer_class`, xem Mục 4).

**Phương án:**
1. Không có gì cần xử lý — hiệu năng hiện tại rất tốt.

### 1.10 `SELECT diagnostic_test_skill_progress.id FROM diagnostic_test_skill_progress JOIN survey_answer ON survey_answer.id = diagnostic_test_skill_progress.survey_answer...`

**Số liệu:** calls=12, total=2,602ms, mean=216.83ms, cache_hit=98.77%,
**rows=0 (toàn bộ 12 lần đều trả về 0 dòng)**

**Tình trạng:** Call site: `_is_mock_day_writing_answer`
(`app/services/mock_day.py:700-710`) — kiểm tra "answer này có phải bài
Mock Day diagnostic không", gọi từ pipeline chấm Writing
(`app/services/marking/pipeline.py:339,600`) và `app/utils/lark.py:85-106`.

`answer_id`, `survey_answer_id`, `mock_day_event_id` đều khai báo
`index=True` trong model, nhưng chưa xác minh được các index này đã thực sự
tồn tại trên DB thật hay chưa (bảng không có migration đi kèm trong repo
đang xem). 216.83ms mean cho 1 existence-check trả về 0 dòng, dù cache_hit
98.77% (không phải đọc đĩa) — khó giải thích nếu có index thật, vì lookup
theo index dù miss cũng nên rất nhanh (<1ms). Nhiều khả năng đang scan do
thiếu index vật lý thật sự.

**Phương án:**
1. **Actionable nhất trong toàn Mục 1**: xác nhận trực tiếp trên DB (`\d
   diagnostic_test_skill_progress`, `\d survey_answer`) xem
   `survey_answer_id`, `answer_id`, `mock_day_event_id` có index thật
   không. Nếu thiếu, thêm migration tạo index.
2. Hàm này được gọi khá thường xuyên trong pipeline chấm Writing (kể cả khi
   trả về 0) — 216ms/lần tuy không lớn nhưng là chi phí ẩn lặp lại mỗi lần
   chấm, đáng ưu tiên sửa.

### Kết luận Mục 1

Phần lớn (8/10) query có hiệu năng thực tế đã tốt (mean thấp, cache hit
cao) và chỉ lọt vào top "tốn tài nguyên nhất" vì tần suất gọi cao — không
cần xử lý gấp. Hai điểm đáng ưu tiên nhất:
- **#1.10** — nghi thiếu index vật lý thật trên bảng Mock Day, cần xác minh
  trực tiếp trên DB rồi thêm migration nếu đúng.
- **#1.6** — cách dùng `.distinct()` trong tra cứu audio pronunciation đang
  làm mất tác dụng cache, cần đội Speaking xác nhận hướng sửa.
- **#1.1** — DELETE chậm bất thường trên bảng file, nên kiểm tra FK/index
  liên quan hoặc tách xử lý storage ra khỏi transaction.

## Mục 2: Bảng cần VACUUM nhất

**Phát hiện quan trọng — bug trong chính code báo cáo.** Công thức
`dead_ratio_pct` ở [db_health_check.py:86-89](../../../../Backend/app-lorvaix/app/services/db_health_check.py:86):

```python
CASE WHEN n_live_tup > 0
     THEN ROUND((n_dead_tup::numeric / n_live_tup) * 100, 2)
     ELSE 0
END AS dead_ratio_pct
```

Khi `n_live_tup = 0` (bảng gần như toàn dead tuple), công thức trả về
**0%** — nghĩa là những bảng **tệ nhất** (100% dead, live=0) lại hiện ra
đẹp nhất trên report. Khớp đúng với dữ liệu đã gửi: 4 bảng có `live=0`
(dead=1132/139/90/29) đều hiện `0%` — đây là **false negative**, thực chất
đáng lo hơn cả các dòng % cao (vd 1 bảng hiện 3650%).

Ngoài ra công thức dùng `dead/live` (không phải `dead/(dead+live)` — cách
tính chuẩn để so với ngưỡng autovacuum), nên % có thể vượt 100% và khó so
sánh chuẩn hoá giữa các bảng.

**Đề xuất sửa code:** đổi công thức về chuẩn
`n_dead_tup / (n_dead_tup + n_live_tup) * 100` — luôn nằm trong [0,100], dễ
so sánh, không cần nhánh ELSE gây hiểu lầm (live=0, dead>0 sẽ tự nhiên ra
đúng 100%).

**Vấn đề vận hành đáng chú ý khác:** cả 10/10 bảng trong danh sách đều có
`last_autovacuum = "-"` (NULL) — autovacuum **chưa từng chạy** trên toàn bộ
các bảng này. Có thể do: (a) autovacuum daemon không hoạt động/bị throttle
trên instance, hoặc (b) counter thống kê mới bị reset gần đây (restart DB,
`pg_stat_reset()`...). Đáng kiểm tra trực tiếp cấu hình `autovacuum` trên
Postgres instance — nếu thật sự không chạy, đây là rủi ro vận hành nghiêm
trọng hơn bất kỳ bảng cụ thể nào trong danh sách.

**Theo từng bảng** (sắp lại theo % dead thực = `dead/(dead+live)`, không
theo % bug hiển thị trên report):

| Bảng | dead | live | % dead thực |
|---|---|---|---|
| directus_sessions | 1132 | 0 | 100% |
| draft_answer | 139 | 0 | 100% |
| quiz | 90 | 0 | 100% |
| student_study_item | 29 | 0 | 100% |
| learning_session | 73 | 2 | 97.3% |
| review | 112 | 19 | 85.5% |
| student_daily_progress | 263 | 78 | 77.1% |
| user_device | 29 | 12 | 70.7% |
| answer | 82 | 264 | 23.7% |
| ai_token_log | 61 | 415 | 12.8% |

**Phương án:**
1. **Sửa công thức `dead_ratio_pct`** trước tiên, để lần chạy tới ra số
   liệu đúng và không bỏ sót các bảng 100% dead.
2. Với các bảng có % dead thực cao (`directus_sessions`, `draft_answer`,
   `quiz`, `student_study_item`, `learning_session`) — cân nhắc `VACUUM`
   thủ công hoặc điều chỉnh `autovacuum_vacuum_scale_factor` cho riêng
   từng bảng; nếu schema không nằm trong repo/migration đang quản lý, trao
   đổi với team sở hữu bảng/dịch vụ tương ứng kèm số liệu này để họ ưu
   tiên.
3. Với `review`, `answer`, `ai_token_log` — đã có migration quản lý schema
   sẵn trong repo, có thể chủ động thêm migration để tune autovacuum hoặc
   chạy `VACUUM` thủ công.
4. Quan trọng nhất: xác nhận trạng thái autovacuum thật của Postgres
   instance — ảnh hưởng toàn hệ thống, ưu tiên cao.

## Mục 3: Index Bloat & Sequential Scan

_Lưu ý: mục này gồm 2 truy vấn riêng (`seq_scan_heavy` + `large_index`).
Dữ liệu đã nhận chỉ có phần `large_index` (10 index lớn nhất) — chưa có
`seq_scan_heavy`, sẽ bổ sung phân tích khi có._

10 index lớn nhất kèm `idx_scan` (số lần được dùng):

| # | Index (tên bị cắt) | Bảng | Size | idx_scan | Nhận xét |
|---|---|---|---|---|---|
| 1 | success_quiz_l... | success_quiz | 1681 MB | 903 | Có dùng — ổn |
| 2 | idx_success_q... | success_quiz | 1678 MB | 0 | **Chưa từng dùng** — 1.68 GB lãng phí |
| 3 | success_quiz_l... | success_quiz | 1677 MB | 0 | **Chưa từng dùng** — 1.68 GB lãng phí |
| 4 | draft_answer_... | draft_answer | 1626 MB | 2715 | Có dùng — ổn |
| 5 | success_quiz_l... | success_quiz | 1489 MB | 4 | Gần như không dùng — hiệu quả cực thấp |
| 6 | answer_user_id... | answer | 1298 MB | 28 | Dùng rất ít so với dung lượng |
| 7 | idx_directus_fil... | directus_files | 1254 MB | 0 | Chưa từng dùng |
| 8 | idx_directus_fil... | directus_files | 1199 MB | 0 | Chưa từng dùng |
| 9 | answer_user_c... | answer | 1079 MB | 77,313 | Dùng rất nhiều — hiệu quả tốt |
| 10 | idx_revisions_c... | directus_revisions | 815 MB | 0 | Chưa từng dùng |

**Phát hiện đáng chú ý:** bảng `success_quiz` có tới **4 index lớn**
(#1,2,3,5) cộng lại ~6.5 GB, nhưng chỉ 1 (#1, 903 lần) thực sự được dùng
thường xuyên — 3 index còn lại gần như vô dụng, tốn ~4.8 GB + chi phí ghi
(mỗi INSERT/UPDATE/DELETE trên bảng này phải cập nhật cả 4 index).

Trên bảng `answer`: 1 index hiệu quả cao (#9, 77k scans) và 1 index dùng
rất ít (#6, chỉ 28 scans/1.3GB) — nghi ngờ #6 có thể trùng lặp/dư thừa so
với #9, nhưng cần tên đầy đủ + định nghĩa cột mới kết luận chắc được (report
đang cắt ngắn tên).

**Phương án:**
1. Với 3 index gần như không dùng trên `success_quiz` (#2,3,5 — ~4.8 GB):
   xác nhận với team sở hữu bảng này xem có thể drop bớt không — index
   không dùng vừa tốn dung lượng vừa làm chậm ghi mà không lợi gì.
2. Với `answer` (#6 vs #9): lấy đầy đủ tên 2 index để xác nhận có trùng mục
   đích không — nếu #6 thực sự dư thừa, có thể cân nhắc DROP qua migration
   sau khi xác nhận không nơi nào khác phụ thuộc.
3. Với `directus_files`/`directus_revisions` (#7,8,10 — ~3.3 GB không
   dùng): chuyển thông tin này cho team quản lý Directus.

## Mục 4: Hiệu quả Index

Phần "unused" (idx_scan < 50) trùng phần lớn với các index ở Mục 3 (cùng
nguồn `pg_stat_user_indexes`, chỉ khác điều kiện lọc) — không phân tích
lại.

**Phần "low_efficiency"** (5 index có `fetch_efficiency_pct` thấp nhất,
đều đã được dùng ≥1 lần):

| Index | Bảng | idx_scan | Size | fetch_efficiency_pct |
|---|---|---|---|---|
| idx_vocab_linki... | vocab_linking | 625 | 117 MB | 0.00 |
| answer_class | answer | 194 | 230 MB | 0.00 |
| idx_question_set_p... | question_set | 107 | 216 KB | 0.00 |
| tracking_user_... | tracking | 96 | 6,496 KB | 0.00 |
| idx_banner_po... | banner | 79 | 16 KB | 0.00 |

**Nghi vấn bug thứ 2 trong code báo cáo.** Công thức ở
[db_health_check.py:192-195](../../../../Backend/app-lorvaix/app/services/db_health_check.py:192):

```python
CASE WHEN idx_tup_read > 0
     THEN ROUND((idx_tup_fetch::numeric / idx_tup_read) * 100, 2)
     ELSE 0
END AS fetch_efficiency_pct
```

Cả 5 dòng đều đúng `0.00` — có 2 khả năng, cần xem thêm `idx_tup_read`/
`idx_tup_fetch` thật (cột "detail" đang bị report cắt ngắn) mới phân biệt
được:
- **(a) Thật sự kém hiệu quả:** index được scan nhiều lần (79-625) nhưng
  chưa từng "đọc" được entry nào khớp (`idx_tup_read = 0`) — đúng nghĩa
  lãng phí, cần xem lại điều kiện query có đang tận dụng đúng index không.
- **(b) False negative giống bug Mục 2:** nếu `idx_tup_read > 0` nhưng
  `idx_tup_fetch = 0`, đây thực chất là **Index-Only Scan hoàn hảo**
  (Postgres lấy đủ dữ liệu từ chính index, không cần đọc thêm từ heap) —
  tức trường hợp **tốt nhất**, nhưng công thức `fetch/read` lại ra 0%, bị
  xếp vào "kém hiệu quả nhất" (`ORDER BY fetch_efficiency_pct ASC`) —
  ngược hoàn toàn thực tế, cùng kiểu lỗi như `dead_ratio_pct` ở Mục 2.

**Phương án:**
1. **Trước khi sửa gì**: lấy đầy đủ `idx_tup_read`/`idx_tup_fetch` thật của
   5 dòng này (không bị cắt) để xác định đang ở case (a) hay (b) — hướng
   xử lý khác nhau hoàn toàn.
2. Nếu là case (b) (nhiều khả năng với bảng nhỏ như `question_set` 216KB,
   `banner` 16KB — rất dễ là Index-Only Scan tự nhiên): sửa code báo cáo
   để phân biệt "0% vì không khớp gì" và "0% vì index-only, không cần fetch
   heap" — ví dụ hiển thị thêm `idx_tup_read` bên cạnh %, hoặc loại các
   dòng `idx_tup_read=0` ra khỏi "low efficiency".
3. `answer_class` đáng chú ý riêng — nếu rơi vào case (a) thật, đây là 1
   điểm actionable trực tiếp có thể xử lý qua migration.
