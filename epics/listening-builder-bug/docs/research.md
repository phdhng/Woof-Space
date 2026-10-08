# Research: Guided Retry (listening-builder) — cơ chế tạo/import và nguồn gốc audio/transcript

Snapshot ngày 2026-08-27, repo `apps/app-agents-service` @ nhánh `feat/epic-007-listening-builder-app-agents-service` (base `origin/main`, HEAD `ee0d9c1`).
Mục tiêu: map "cái gì nằm ở đâu" cho 2 endpoint `/v1/guided-retry/generate`, `/v1/guided-retry/process/import` và cho các lỗi học sinh report (đều xoay quanh audio đoạn dictation: thiếu chữ đầu/cuối, audio không khớp transcript, transcript sai, không nghe được).

Tất cả route đều có `url_prefix = "/v1"` — `app/utils/const.py:128` (`URL_PREFIX = "/v1"`).

---

## app-agents-service — HTTP layer

### Controller guided-retry
- `app/controllers/guided_retry_controller.py:56` `create_guided_retry_controllers()` — Blueprint `"guided_retry"`, đăng ký ở `app/factory.py:139`.
- `app/controllers/guided_retry_controller.py:62` `POST /guided-retry/generate` — `@jwt_required`, validate body bằng `GenerateGuidedRetryRequest`, gọi `service.generate(req, user_id=...)`.
- `app/controllers/guided_retry_controller.py:118` `GET /guided-retry/quiz/<quiz_id>/status` — `service.get_quiz_status`.
- `app/controllers/guided_retry_controller.py:129` `POST /guided-retry/process/import` — body `{ "process_ids": [...] }`, loop gọi `service.import_to_db(process_id)`; trả `results[]` với `success/message`, HTTP 200/400 tuỳ số lượng thành công.
- `app/controllers/guided_retry_controller.py:230` `DELETE /guided-retry/practice-flow` — body `{ "ids": [...] }` → `service.delete_practice_flows`.
- `app/controllers/guided_retry_controller.py:243` `POST /guided-retry/practice-question/snippet-transcribe` — body `practice_question_id`, `provider` (`deepgram`|`assemblyai`); cắt audio cửa sổ `[audio_start-10s, audio_end+10s]`, STT, so transcript với `correct_answers[0]`. Công cụ kiểm tra, không sửa DB.

### Endpoint sửa lỗi audio đã có sẵn (part_controller)
- `app/controllers/part_controller.py:139` `POST /parts/<part_id>/vocab/sync-sentence-times` — body `is_update_vocab` (default false). Gọi `ListeningVocabSentenceTimeSyncService.sync_sentence_times` → chạy Deepgram, khớp lại timestamp câu vocab level-2, optionally ghi `vocab_linking.meta`.
- `app/controllers/part_controller.py:186` `POST /parts/<part_id>/guided-retry/fix-audio-times` — body `is_update` (default true), `question_id` (optional). Gọi `GuidedRetryAudioTimestampFixService.apply_for_part` → tính lại `practice_question.audio_start/audio_end` từ `vocab_linking.meta` level-2 hiện tại.

### Request schema — `app/schemas/guided_retry_schema.py`
- `:22` `GenerateGuidedRetryRequest` — `quiz_id: str` (bắt buộc), `part_id: int` (bắt buộc), một trong `question_set_ids` / `question_set_id` / `question_ids` (validator `:70` `require_question_target`), `step` (regenerate 1 atomic step), `model` (ép model AI), `listening_gap_filling_prompt: "auto"|"part1"|"part4"` (`:55`).
- `:79` `ImportGuidedRetryRequest` — `process_ids: list[int]`, `min_length=1`.
- `:150` `PracticeQuestionOutput` — chứa `audio_start: Optional[float]`, `audio_end: Optional[float]` ("Giây bắt đầu/kết thúc trong file audio"), `word_tokens`, `correct_answers`, `suggested_vocabs`, `type` (`SINGLE_CHOICE|MULTIPLE_CHOICE|INFORMATION|DICTATION|GAP_FILLING`).

---

## app-agents-service — `generate` pipeline

### `GuidedRetryService` — `app/services/guided_retry_service.py`
- `:329` `import_to_db(process_id)` — đọc `MigrateProcess.structure` (JSON), yêu cầu `status == "completed"`; với mỗi `flow_data` gọi `PracticeRepository.save_practice_flow(flow_data)`; set `process.status = "imported"`. **Không đụng gì tới `audio_start/audio_end`** — ghi nguyên si từ structure.
- `:395` `_build_question_context(part_detail, question_id, ...)` — dựng `QuestionContext`:
  - `:427` `vocabs = part_detail.get("vocabs")` — cây vocab của Part (nguồn transcript + timestamp).
  - `:438` `_extract_vocab_data(vocabs, search_list)` (`:1059`) — tìm `answer_sentence` (câu level-2 chứa `correct_answers`), `answer_paragraph` (level-1 cha), `all_sentences_text`.
  - `:441` `_flatten_level2_sentences(vocabs)` (`:1048`) — `all_vocab_sentences`: mọi câu level-2 của Part, mỗi phần tử `{ value, meta:{from,to}, youpass_pick_words, ... }`.
- `:626` `_process_single_question(...)` — build context → gọi AI agent → `QuestionGenerationResult`.
- `:563` `_persist_suggested_vocabs_in_structure(...)` — insert bảng `vocab` cho mỗi `suggested_vocabs` item, gán `id`/`word_display` ngược lại vào structure trước khi lưu `MigrateProcess`.

### `GuidedRetryAIAgent` — `app/services/guided_retry_ai_agent.py` (4517 dòng)
Nguồn `audio_start`/`audio_end` cho practice_question **hoàn toàn** đến từ `meta.from`/`meta.to` của câu vocab level-2 trong `part_detail.vocabs`. AI chỉ chọn *câu nào*; giá trị giây lấy từ meta.

- `:33` `QuestionContext` dataclass — `all_vocab_sentences`, `paragraph_sentences` (`:173`), `answer_sentence_meta` (`:162` → `{start,end}` — chú ý key `start/end` ở property này, còn lookup dùng `from/to`).
- `:178` `sentences_for_timestamp_lookup` — pool để fuzzy match: `all_vocab_sentences` nếu có, else `paragraph_sentences`.
- `:360` `_build_numbered_transcript(context)` — transcript đánh số `[L01]…` theo đúng index của `all_vocab_sentences`; đưa vào system prompt để AI tham chiếu `"line": "L15"` thay vì copy text.
- `:377` `_resolve_line_ref("L15", context)` — trả `(value, from, to)` từ `all_vocab_sentences[14].meta["from"/"to"]`; out-of-range → `("", None, None)` + log warning.
- `:402` `_resolve_item_timestamps(item, context, fallback_from, fallback_to)` — nếu item có `"line"` → dùng `_resolve_line_ref`; else fuzzy match `item["sentence"]` qua `_lookup_timestamps`; nếu vẫn None → `fallback_from/to`.
- `:2482` `_lookup_timestamps(text, sentences, match_threshold=0.75)` — tách `text` thành câu (`_transcription_to_fragments` `:2450`), mỗi fragment `difflib.SequenceMatcher` với `_normalize_for_timestamp_match` (`:2408` — bỏ filler words `uh/um/like/well/so/...`, bỏ dấu câu). Trả `(metas[0].from, metas[-1].to)` khi ≥50% fragment match; else `(None,None)` + log `"only X/Y fragments matched"`.
- `:2414` `_best_sentence_meta_for_fragment` — chọn câu có ratio cao nhất; chỉ nhận nếu `ratio >= match_threshold` **và** câu đó có `meta.from/to`.
- `:2552` `_calc_paragraph_time_range(context)` — fallback range = `min(from)`..`max(to)` của `paragraph_sentences` (else `all_vocab_sentences`, else `answer_sentence_meta`). Đây là range dùng khi AI không đưa được line/sentence hợp lệ → audio đoạn có thể rộng bằng cả paragraph.
- `:3199` `_format_map_dictation_step` và các builder DICTATION khác (`:3221`, `:3435`, `:4301`) — mỗi câu dictation: `_resolve_item_timestamps(sent, context, para_meta.from, para_meta.to)` rồi set `audio_start/audio_end`, `correct_answers=[sentence_val]`. Không cộng thêm padding/margin nào.
- Các bước không có audio (HIGHLIGHT, PREDICT_WORD_TYPE, MULTIPLE_CHOICE, SINGLE_CHOICE, INFORMATION-config...) set `audio_start/audio_end = None` (nhiều chỗ: `:2105`, `:2151`, `:3282`, `:3374`, `:4244`, `:4285`, `:4365`...). Ở API GET các câu này trả `0` (xem phần API thực tế).

### `GuidedRetryValidator` — `app/services/guided_retry_validator.py`
Post-generation, chỉ log/flag, không chặn:
- `:58` `_check_timestamps` — `audio_start >= audio_end` → error `timestamp_order`; DICTATION duration ngoài `[2.0, 60.0]s` → warning; câu sau `audio_start < prev_end - 1.0` → warning `timestamp_ascending`.
- `:142` `_check_transcript_accuracy` — so `correct_answers` / `content` của DICTATION & CONTEXT_COMPREHENSION với pool `all_vocab_sentences` (ngưỡng `_TRANSCRIPT_SIMILARITY_THRESHOLD = 0.60`) → warning khi câu sinh ra không khớp transcript gốc.
- `:50/:51` còn `_check_chronological_order`, `_check_anti_leakage`.

---

## app-agents-service — nguồn gốc timestamp câu (`vocab_linking.meta.from/to` level-2)

Đây là "sự thật gốc" mà cả `generate` và `fix-audio-times` phụ thuộc.

### `ListeningVocabSentenceTimeSyncService` — `app/services/listening_vocab_sentence_time_sync_service.py`
- `:231` `sync_sentence_times(part_id, is_update_vocab=False)`:
  - `:250` `locate.fetch_listening_transcript_with_words(file_id)` — lấy transcript Deepgram + words (word-level timing) cho `part.file_id`.
  - `:254` `flatten_transcript_paragraphs_to_sentences`, `:258` `assign_words_to_sentences`.
  - `:264` `collect_vocab_level2_for_mapping(vocabs, only_missing_meta=False)`, `:271` `map_transcript_sentences_to_vocab_level2` — khớp câu vocab level-2 ↔ câu Deepgram.
  - `:111` `_neighbor_word_recovery` — câu không khớp: lấy cửa sổ từ `prev_match.end+1` … `next_match.start` (inclusive câu sau), `locate.refine_vocab_timing_with_words(..., min_token_match_ratio=0.65)` để align theo từ.
  - `:337` `refine_vocab_timing_with_words(...)` — refine `from/to` xuống mức từ trong span câu đã khớp.
  - `:42` `_need_update_timestamp` — chỉ update khi lệch `> 1e-6` hoặc đổi speaker.
  - `:486` nếu `is_update_vocab` → `VocabRepository.batch_update_vocab_linking_meta(part_id, merged_for_db_update)`.
  - `:17` `MATCH_RATIO_META_THRESHOLD = 0.85` — dưới ngưỡng thì trả `meta`/`meta_previous` để review tay.
- `_merge_timing_into_meta` (`:22`) — chỉ ghi đè `from`, `to`, `speaker`; giữ nguyên field khác của meta.

### `GuidedRetryAudioTimestampFixService` — `app/services/guided_retry_audio_timestamp_fix_service.py`
Sửa `practice_question.audio_start/audio_end` **từ meta level-2 hiện có** (không gọi lại Deepgram):
- `:75` `_load_level2_vocab_meta(part_id)` — join `VocabLinking` (`level == 2`, `part_id`) × `Vocab`; đọc `meta["from"]`, `meta["to"]`; bỏ nếu thiếu hoặc `to < from`.
- `:133` `_load_practice_questions_with_audio` — `PracticeQuestion` join `PracticeStep`→`PracticeFlow`→`Question`→`QuestionSet` theo `part_id`, chỉ lấy câu `audio_start IS NOT NULL`.
- `:364` `apply_for_part(part_id, is_update=True, question_id=None)`:
  - `passage in {1,4}` → mode `multi_vocab_span_by_anchors`: candidate vocab từ `suggested_vocabs` (`_pick_candidate_vocab_ids` `:182`) hoặc suy từ overlap audio cũ (`_infer_candidate_vocab_ids_by_audio` `:277`); `start_anchor` = vocab gần `old_start` nhất theo `.from` (`:257`), `end_anchor` = gần `old_end` nhất theo `.to` (`:267`); `new_start/new_end` = min/max của segment vocab giữa 2 anchor.
  - `passage != 1,4` → mode `single_vocab_by_correct_answers`: `_pick_vocab_id_by_correct_answer` (`:316`) — match `correct_answers[0]` (sau `_norm`) với `vocab.value`, ngưỡng `_CORRECT_ANSWER_VOCAB_SIMILARITY_MIN = 0.8`; `new_start/new_end = vm.start/vm.end` của đúng 1 vocab đó.
  - similarity trong `[0.8, 0.9)` → ghi `GuidedRetryVocabMatchReview` (`_upsert_guided_retry_vocab_match_review` `:157`) để review tay.
  - Chỉ `UPDATE practice_question SET audio_start/audio_end` khi `is_update` và range mới khác range cũ (`_EPS = 1e-6`).
  - `new_start/new_end` set = biên câu vocab, **không cộng margin** → nếu meta câu vocab bị cắt sát chữ đầu/cuối, audio vẫn thiếu chữ.
- `:24-27` hằng số ngưỡng similarity; `app/models/guided_retry_vocab_match_review.py` (model), `script/guided_retry_vocab_match_review.sql` (query review).

---

## app-agents-service — persistence

- `app/repositories/practice_repository.py:16` `save_practice_flow(flow_data)` — tạo `PracticeFlow`/`PracticeStep`/`PracticeQuestion`.
- `app/repositories/practice_repository.py:41` `PracticeQuestion(...)` với `audio_start=q_data.get("audio_start")` (`:54`), `audio_end=q_data.get("audio_end")` (`:55`) — copy thẳng từ structure JSON.
- `app/repositories/practice_repository.py:129` nhánh khác tạo `PracticeQuestion` với `audio_start=None`, `audio_end=None` (`:138-139`).
- Models: `app/models/practice.py` — `PracticeFlow`, `PracticeStep`, `PracticeQuestion`.

---

## API thực tế — `GET /v1/practice-flows?quiz_id=&question_id=&answer_id=` (host `api.youpass.vn`)

Gọi thật với `quiz_id=11922&question_id=23321&answer_id=20363866` (flow_id 498, feature `listening_multiple_choice_many`, IELTS Listening Part 3 dyspraxia). Cấu trúc response:
- `data[]` mỗi phần tử = 1 flow: `flow_id`, `quiz_id`, `question_id`, `step_template` (`feature_code`, `title`, `description`, `steps[]` với `icon/sort/title`), `practice_steps[]`, `existing_practice_answer`.
- `practice_steps[]`: `id`, `sort`, `type` (`HIGHLIGHT`, `MULTIPLE_CHOICE_MANY_RETRY`, ...), `content`, `title`, `parent_step_id`, `parent_option_index`, `questions[]`.
- `questions[]`: `id`, `type` (`HIGHLIGHT|DICTATION|MULTIPLE_CHOICE|SINGLE_CHOICE|...`), `sort`, `content` (string), `options[]` (`{text, option}`), `correct_answers[]`, `explanation[]` (`{value, explanation}` — HTML), `suggested_vocabs` (null hoặc `[{id, value, meaning, explanation, word_display}]`), `audio_start`, `audio_end`, `map_image`.
- **`audio_start`/`audio_end` chỉ khác 0 ở câu `type=DICTATION`**; các loại khác trả `0` (không phải null). Ví dụ observed (giây, trong file audio của Part):
  - DICTATION sort 1: `[155.795, 158.435]` — "They need so much more support with letter formation."
  - sort 2: `[158.95, 162.79]`; sort 3: `[163.03, 164.79]` ("It takes a lot of patience.").
  - sort 6: `[165, 170.70999]`; sort 10: `[175.625, 188.89]` (câu dài); sort 13: `[188.8, 198.9]`; sort 14: `[199, 204.845]`.
  - Các câu DICTATION liên tiếp gần như nối đuôi (end câu trước ≈ start câu sau ± vài chục ms) → biên câu lấy từ word-level timing; sai lệch nhỏ ở biên = "thiếu chữ đầu/sau".
- `suggested_vocabs[].id` là id bảng `vocab` (đã persist ở generate), không phải vocab level-2 của Part.

Report học sinh dùng `flow_id` + `practice_question` (id của `practice_question`) + `order_question` (thứ tự câu hỏi trong quiz). Mapping: `bug_reported.md` "practice_question" = `practice_steps[].questions[].id`; "flow_id" = `flow_id`.

---

## Kết nối / quan sát (không phải đề xuất)

- Chuỗi phụ thuộc audio đoạn dictation: `part.file_id` → Deepgram transcript+words → khớp câu vocab level-2 → `vocab_linking.meta.from/to` → (generate: AI chọn câu qua `[Lnn]`/fuzzy match) → `structure.audio_start/audio_end` → `import_to_db` → `practice_question.audio_start/audio_end` → FE phát `[audio_start, audio_end]` trên file audio của Part.
- Không có bước nào cộng padding/margin vào `audio_start/audio_end`; sai số biên từ Deepgram word timing đi thẳng ra học sinh.
- Đã tồn tại 2 công cụ sửa hậu kỳ nhưng phải gọi thủ công theo từng Part: `POST /parts/<part_id>/vocab/sync-sentence-times` (làm mới meta câu vocab) và `POST /parts/<part_id>/guided-retry/fix-audio-times` (đẩy meta mới xuống practice_question). `fix-audio-times` không gọi lại Deepgram — chất lượng phụ thuộc meta level-2 đã có.
- "Transcript bị sai" / "audio không đúng transcript": `correct_answers` của DICTATION = `value` câu vocab level-2 được chọn (generate) — sai khi (a) fuzzy match `_lookup_timestamps`/`_resolve_line_ref` chọn nhầm câu, hoặc (b) `value` câu vocab level-2 gốc đã sai. `GuidedRetryValidator._check_transcript_accuracy` chỉ log warning, không chặn import.
- "Không nghe được audio": ở dữ liệu GET, câu không phải DICTATION có `audio_start=audio_end=0`; nếu FE cố phát đoạn `[0,0]` cho câu đáng lẽ có audio → im lặng. Chưa xác định được ở phía nào (FE hay data) trong phạm vi repo này.
- Report line 117-118 ("bấm câu nào cũng phát lại chỗ 'part 3'") gợi ý FE luôn phát từ một mốc cố định — logic phát audio nằm ở FE (`fe` repo), ngoài phạm vi `app-agents-service`.

## app-agents-service — tạo cây vocab + meta lần đầu (trước guided-retry)

- `app/repositories/vocab_repository.py:148` `save_vocab(part_id, vocab_data)` → `:36` `_save_vocab_attempt` — xoá vocab/`vocab_linking` cũ theo `part_id`, rồi tạo lại 3 level. Level-2 (`:271-281`): `VocabLinking(level=2, meta=sentence.get('meta') or None)` — `meta` (`{from,to,speaker}`) đến từ `vocab_data` truyền vào, **không tính ở repo**.
- `app/services/locate_info_service.py` — service dựng `vocab_data` từ transcript:
  - `:1181` `_map_transcript_to_vocab`, `:1193` `_map_transcript_to_vocab_by_ai` — map câu transcript ↔ câu vocab level-2, gán `meta.from/to`.
  - `collect_vocab_level2_for_mapping(..., only_missing_meta=True)` (`:1187`) — bản đầu chỉ điền câu chưa có meta.
  - `refine_vocab_timing_with_words(...)` — align biên câu xuống mức từ (dùng chung với `ListeningVocabSentenceTimeSyncService`).
  - `:1811` `_trim_vocab_by_listen_from`, `:1879` `_determine_paragraph_ranges` — cắt/nhóm paragraph theo `listen_from` của Part.
  - `:1803` gọi `VocabRepository(...)` để lưu.
- `GuidedRetryValidator` **có** chạy trong `generate`: `app/services/guided_retry_service.py:80` khởi tạo, `:718-724` `validate_flow(flow_dict, context)` mỗi question → **chỉ `logger.warning`**, không chặn, không ghi bảng. Flow vẫn `_parse_flow_output` và trả `status="completed"` dù có warning `timestamp_order` / transcript-accuracy.

## Gaps / open questions

- **Chi tiết luồng build Part lần đầu**: entrypoint/endpoint nào gọi `LocateInfoService` + `VocabRepository.save_vocab` khi tạo Part listening (khả năng qua `locate_info_controller.py` / `builder_*`), và Deepgram vs AI-mapping được chọn khi nào — mới trace tên hàm, chưa trace call chain từ controller.
- **`listening_service.py`** có prompt yêu cầu AI trả `from/to` "start/end time of the passage that contained the answer" (`:453` trở đi) — quan hệ giữa luồng này và guided-retry chưa rõ (có phải cùng ghi `audio_start` không, hay là feature listening cũ).
- **FE**: toàn bộ logic phát đoạn `[audio_start, audio_end]`, xử lý câu `audio=0`, và bug "luôn phát lại part 3" nằm ở repo `fe` — không có trong workspace này.
- **Deepgram accuracy**: mức sai số biên từ (leading/trailing) điển hình của Deepgram trên audio IELTS chưa đo; không có cấu hình padding nào trong repo để so sánh.
- **`answer_sentence_meta` dùng key `start/end`** (`ai_agent.py:165`) trong khi lookup/`_calc_paragraph_time_range` dùng `from/to` — chưa xác nhận đây là bug hay 2 nguồn meta khác format.
- Mapping chính xác `bug_reported.md` `question_id` (23321...) ↔ `Question.id` vs `PracticeFlow.question_id` — giả định bằng nhau dựa trên API GET, chưa kiểm DB.
- **Chọn câu đáp án theo `locate_info` chưa áp dụng cho `MULTIPLE_CHOICE_MANY`** (`guided_retry_service.py` `_locate_info_bounds`): loại này lưu `locate_info` lồng theo từng đáp án (`{"0": {"paragraph_ranges": [...]}, "1": {...}}`, xem `locate_info_service.py` `_process_multiple_choice_many_locate`), không có `paragraph_ranges` ở cấp trên cùng → `_locate_info_bounds` trả rỗng, vẫn lấy câu match cuối cùng của cả Part. Chưa gặp bug thực tế của loại này. Khi cần: gom `paragraph_ranges` từ mọi key con khi không có ở cấp trên cùng. Các loại khác (gap-filling, `MATCHING_ENDINGS`, `MATCHING_FEATURES`, ...) lưu ở cấp trên cùng nên đã được áp dụng.
- **MAP_DIAGRAM_LABEL: giải thích distractor có thể mô tả sai vị trí trên bản đồ** (`guided_retry_ai_agent.py` `generate_map_answer_selection`, cả `generate_map_dictation`): prompt chỉ gửi danh sách chữ cái vị trí (`A`–`J`) + transcript, **không gửi ảnh map** (ảnh nằm trong `question_set.description`, URL `cms.youpass.vn/assets/...`). Đáp án đúng vẫn đúng (lấy từ data), nhưng AI tự đoán vị trí của các option còn lại. Ví dụ quiz 13359 / question 35710 ("Café", đáp án A): giải thích ghi "B là cửa hàng (shop)" và "C dẫn ra bể bơi", trong khi trên map shop là phòng có nhãn chữ, B là weights room, hồ bơi là G. Hướng sửa khi cần: gửi kèm ảnh map vào prompt bước chọn vị trí (model hỗ trợ input ảnh) — cần tải ảnh từ CMS và đổi message sang dạng multimodal. Chưa gặp user báo bug cho lỗi này.
