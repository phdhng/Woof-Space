# app-lorvaix — App Context

Repo: `Backend/app-lorvaix`. Backend Python phục vụ các pipeline AI chấm điểm
IELTS (Writing PRO, Speaking VIP2) và một số tính năng vệ tinh (Mock Day, DB
Health Check, Operation Console...), chạy song song với service chính/legacy
viết bằng Go (`app-api`) — nhiều bảng Postgres dùng chung giữa 2 service.

Repo hiện **không có** CLAUDE.md, README thật (chỉ có ASCII art logo,
14 dòng), hay CONTRIBUTING — mọi convention dưới đây được rút ra trực tiếp từ
code/config, không phải từ tài liệu có sẵn.

---

## 1. Tech stack

| Thành phần | Công nghệ | Ghi chú |
|---|---|---|
| Framework | FastAPI 0.115.6 | Entry point `run.py` → `app/factory.py:create_app()`, chạy bằng `uvicorn` |
| Python | 3.10.11 (Dockerfile) | `.venv` local lại là 3.12 — **lệch version giữa local và prod**, cần lưu ý khi cài đặt/test |
| Validation | Pydantic v2 (2.8.2) | Schemas ở `app/schemas/` |
| ORM/DB driver | SQLAlchemy 2.0.35 (Core+ORM) + `psycopg2-binary` (sync) | Postgres, không dùng async driver |
| Config | Dynaconf 3.2.6 + `python-dotenv` | Xem Mục 7 |
| Auth | PyJWT | HS256, xem Mục 8 |
| Cache | Redis 5.2.1 | Dùng làm cache/leaderboard, **không phải** task queue (không có Celery/RQ) |
| HTTP client | httpx 0.27.0 | Dùng trong SDK OpenAI-compatible + gọi trực tiếp 1 số nơi |
| AI SDK | `openai` 1.46.0 | Dùng như client OpenAI-compatible chung, trỏ tới AntiYouPass/OpenRouter/UniversalAI — không phải gọi thẳng OpenAI |
| TTS | `google-cloud-texttospeech` | Sinh audio phát âm mẫu cho Speaking |
| NLP phụ trợ | `eng-to-ipa`, `langdetect` | Phục vụ chấm Speaking/Writing |

**Không có** linter/formatter/type-checker nào được khai báo (không
ruff/black/flake8/isort/mypy, không `pyproject.toml`/`.flake8`/
`.pre-commit-config.yaml`). Đây là khoảng trống đáng lưu ý khi thiết lập quy
trình review — hiện tại style nhất quán (nếu có) là do thói quen người viết,
không có công cụ enforce.

---

## 2. Cấu trúc thư mục

```
app-lorvaix/
├── app/                  # Toàn bộ application package
│   ├── controllers/      # FastAPI router factories (7 file)
│   ├── models/           # SQLAlchemy ORM models
│   ├── repositories/     # Client gọi ra ngoài (AI gateway, STT, TTS, CMS, Lark, app-api...)
│   ├── schemas/          # Pydantic request/response
│   ├── services/         # Business logic (lớp lớn nhất)
│   │   └── marking/      # Pipeline chấm AI (Writing PRO + Speaking VIP2)
│   ├── cron/             # Entry point cho k8s CronJob
│   ├── utils/            # DB session, Redis, logger, const.py, error, helper...
│   └── factory.py        # create_app(): wiring router, exception handler, lifespan
├── config/               # config.py (Dynaconf settings), error_messages.py (catalog lỗi)
├── middleware/           # authentication.py — JWT decode + FastAPI Depends guard
├── migrations/           # File .sql thô, chạy tay (xem Mục 4)
├── provision/k8s/production/  # Manifest k8s (deployment, hpa, service, ingress, cronjob)
├── tests/                # 20 file test phẳng, không mirror cấu trúc app/
├── keys/                 # config.json (non-secret) + settings.toml (secret template, commit rỗng giá trị)
├── docs/                 # 3 doc thiết kế viết tay (Speaking PR mapping, Writing PRO model switch)
└── .circleci/config.yml  # Toàn bộ CI/CD pipeline
```

Không có thư mục `api/`/`routes/` riêng (vai trò đó do `controllers/` đảm
nhiệm), cũng không có tầng "repository cho DB" — DB access nằm trực tiếp
trong `services/` qua SQLAlchemy `Session`; `repositories/` chỉ dành cho
client HTTP/service bên ngoài.

---

## 3. Kết nối DB & session pattern

`app/utils/postgres.py`:
- Pool: `pool_size=3, max_overflow=5, pool_timeout=30, pool_recycle=1800,
  pool_pre_ping=True` — cố ý nhỏ vì Postgres đang giới hạn tổng 320
  connection dùng chung với `app-api`, trong khi HPA có thể scale tới 20
  pods × 2 worker.
- `SessionLocal` **không** dùng `scoped_session` — có comment giải thích rõ:
  FastAPI có thể chạy `__enter__`/`__exit__` của 1 sync dependency trên các
  thread khác nhau, nên registry thread-local sẽ `remove()` nhầm thread và
  làm rò rỉ/hỏng connection trong pool. Mỗi unit-of-work lấy 1 `Session`
  riêng qua `get_db()` (FastAPI `Depends`) hoặc `db_session()` (context
  manager cho cron/background code).
- `init_db()` **không** tạo/alter schema — chỉ log nhắc: *"Run migrations
  manually before using new tables/columns."*
- SQL echo là opt-in qua `POSTGRES__GORM_DEBUG` (tên biến mirror theo quy
  ước của service Go legacy).

---

## 4. Quy ước migration

- File `.sql` thô trong `migrations/`, đánh số 3 chữ số (`001`...`006`) —
  lưu ý số `002` bị dùng 2 lần (`002_db_health_log.sql` và
  `002_speaking_scoring_schema.sql`), đánh số không hoàn toàn tuần tự toàn
  cục, chỉ tăng dần theo nhánh tại thời điểm merge.
- **Chạy thủ công** — mọi file đều ghi rõ song ngữ ở đầu: *"Chạy thủ công
  trên PostgreSQL. SQLAlchemy KHÔNG tự tạo/alter schema."* Không có
  Alembic, không có bảng `alembic_version`, không có script tự động chạy
  migration — cần chạy tay theo thứ tự.
- **Idempotent theo convention**: `ADD COLUMN IF NOT EXISTS`, `CREATE TABLE
  IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, bọc trong `BEGIN;...
  COMMIT;`. Một số migration còn re-assert lại cột của migration trước "in
  case 001 hasn't run yet" — viết để an toàn khi chạy lại/chạy lệch thứ tự,
  không strict enforce chain tuyến tính.
- **Không có rollback strategy hay approver chính thức** được ghi ở đâu cả
  (không `down.sql`, không quy ước "reviewed-by").
- **Phân biệt bảng tự tạo vs bảng dùng chung/kế thừa từ Go**:
  - Model của bảng KHÔNG do repo này sở hữu có docstring bắt đầu bằng
    `"""Existing … table …"""` và `__table_args__ = {"extend_existing":
    True}` (vd `app/models/answer.py`, `app/models/directus_users.py`).
  - Model bảng tự tạo (vd `app/models/db_health_log.py`) không có
    docstring/`extend_existing` kiểu đó.
  - Toàn bộ tên bảng được tập trung thành hằng số `PG_TABLE_*` trong
    `app/utils/const.py` — muốn biết app đang đụng tới bảng nào, đọc thẳng
    danh sách này.

---

## 5. Domain modules

| Module | Vai trò | Entry point chính |
|---|---|---|
| **Writing PRO marking pipeline** | Chấm bài Writing bằng AI, nhiều bước tuần tự (segment → check → audit → reconcile → score/council → arbitrate → caps → head examiner → highlights → upgrade) | `app/services/marking/pipeline.py`, các file `s0_segment.py`...`s6_upgrade.py` |
| **Speaking VIP2 marking pipeline** | Chấm Speaking (FC/PR/LR/GRA), có 2 chế độ map phát âm: AI prompt hoặc thuật toán align phoneme cục bộ (Needleman–Wunsch) | `app/services/marking/speaking_pipeline.py`, `ipa_alignment.py` |
| **Answer upgrade** (tính năng trả phí) | Viết lại/nâng cấp bài Writing/Speaking sau khi chấm | `answer_upgrade.py`, `speaking_answer_upgrade.py` |
| **Quiz submission** | Nhận bài nộp, dispatch theo loại kỹ năng | `quiz_submit.py`, `speaking_quiz_submit.py`, gọi từ `controllers/quizzes.py` |
| **Mock Day** | Sự kiện thi thử đa kỹ năng có giới hạn thời gian | `mock_day.py`, `mock_day_analysis.py`, `mock_day_mastery.py`, `mock_day_retry.py` + cron reclaim riêng |
| **DB Health Check** | Quét `pg_stat_*`, sinh gợi ý AI, gửi Lark card | `db_health_check.py`, `db_health_ai.py`, `db_health_job.py`, `db_health_lark_card.py` |
| **Operation Console** | Cho phép ADMIN/OPS đổi model AI theo prompt group, xem danh sách review | `operation_prompts.py`, `operation_reviews.py`, `controllers/operation.py` |
| **Configuration/Prompt-set** | Kho key-value (bảng `configuration`, `prompt_set`, cache Redis) chứa provider/model/prompt cho mọi lời gọi AI — đổi được không cần deploy | `configuration.py`, `prompt_set.py` |
| **Student study plan** | Kế hoạch học tập theo tuần | `student_study_item.py`, `models/student_study.py` |
| **Subscription/Loyalty quota** | Giới hạn lượt dùng tính năng trả phí qua service Loyalty ngoài | `subscription_quota.py`, `repositories/loyalty.py` |
| **STT Gateway** | Abstraction đa provider cho speech-to-text (hiện chỉ wire Deepgram) | `stt_gateway.py`, `repositories/stt_deepgram.py` |
| **Pronunciation audio** | Sinh audio mẫu phát âm đúng bằng GCP TTS, cache theo (word, ipa) | `pronunciation_audio.py` |
| **Test-prompt sandbox** | Cho Ops dry-run 1 bước hoặc cả pipeline với prompt nháp | `test_prompt.py` |
| **Auth/ACL** | Resolve role từ `directus_roles`, gate endpoint theo role | `acl.py`, `auth_roles.py` |
| **Activity progress** | Ghi nhận hoạt động phục vụ tính năng streak ở service khác | `activity_progress.py` |

---

## 6. Config & secrets

- `config/config.py` — thứ tự ưu tiên: `keys/settings.toml` (Dynaconf) →
  env var có prefix `MARKING_` → env var trần → giá trị mặc định hardcode.
- `.env` (gitignore, dùng local dev) chứa các biến `MARKING_*`: DB, Redis,
  API key AI gateway, token CMS, JWT secret, webhook Lark...
- `keys/settings.toml` là cơ chế cho **staging/production** — mount vào
  pod từ k8s ConfigMap `app-lorvaix-key`. Bản commit trong git có ~45 key
  nhưng **để trống giá trị** — đóng vai trò template/schema tên setting,
  không phải rò rỉ secret.
- `keys/config.json` (được commit, không secret): chỉ chứa URL/tên model
  (`OPENROUTER_URL`, `CLAUDE_MODEL_NAME`...).
- Mỗi setting trong `Config.__init__` đều có comment giải thích rõ *why*
  (timeout, fallback chain, feature flag) — đáng đọc trước khi đổi giá trị
  mặc định.

---

## 7. API layer convention

- Router: factory function trong `app/controllers/` (`create_X_router`/
  `init_X_router`), mount ở `app/factory.py` dưới prefix `/v1`. Dependency
  (vd `AIProviderService`) được khởi tạo 1 lần trong `create_app()` rồi
  truyền vào factory — DI thủ công, không dùng container framework.
- Validation: Pydantic v2 ở `app/schemas/`.
- Response envelope chuẩn: `response_ok(data, message="success", status=200)`
  trả `{"message", "data", "status"}` — tự serialize Pydantic model hoặc
  SQLAlchemy model. `response_error()` build từ `CustomError`.
- Error handling: `CustomError(code, ...)` tra cứu `config/error_messages.py`
  — catalog lỗi tập trung, **cố tình giữ đồng bộ với `common/error_messages.go`
  bên service Go** (ghi rõ trong comment đầu file), mỗi lỗi có message song
  ngữ VI/EN. Exception handler toàn cục ở `app/factory.py` convert
  `CustomError` thành JSON `{code, error_code, message, error_detail,
  data: null}`.
- Auth: JWT HS256 qua `middleware/authentication.py` (`get_current_user`,
  `require_user_id` — dependency dùng chung, được tách ra sau khi bị
  reviewer nhắc vì "copy-paste ở từng controller"). Authorization theo
  role là tầng riêng: `acl.py` resolve role từ `directus_roles`,
  `auth_roles.py` gate riêng cho Operation Console (ADMIN/OPS).
- CORS mở toàn bộ (`allow_origins=["*"]`) ở tầng app — giới hạn origin
  thật nằm ở ingress k8s, không phải trong code.

---

## 8. Testing

- Framework: pytest + FastAPI `TestClient`.
- Cấu trúc: `tests/` phẳng (20 file), **không mirror** cây thư mục `app/`
  — đặt tên theo feature/controller. Không có `conftest.py` — fixture định
  nghĩa riêng từng file.
- Pattern mock: override `app.dependency_overrides[get_current_user]` /
  `[get_db]` để giả JWT + DB — **không dùng DB thật trong test**, phần lớn
  file dùng `unittest.mock`/`MagicMock`/`monkeypatch`. 1 số file test logic
  thuần (`test_scoring.py`, `test_ipa_alignment.py`) không đụng
  FastAPI/DB.
- Không có coverage tooling (`.coveragerc`, `pytest.ini`...).
- **Lưu ý quan trọng**: `pytest` **không** có trong `requirements.txt` và
  **không được cài** trong `.venv` của repo — chạy `pip install -r
  requirements.txt` xong vẫn không có pytest để chạy 20 file test này.
  Không có `requirements-dev.txt`. Ai muốn chạy test phải tự `pip install
  pytest` thủ công.

---

## 9. CI/CD

CircleCI (`​.circleci/config.yml`), các job chính:

1. `general-version` (nhánh `main`) — tính semver tag, sinh `CHANGELOG.md`.
2. `build-dockerimage-production` / `build-dockerimage-staging` — build +
   push Docker image.
3. `deploy-staging` — SSH vào host staging, `docker compose up -d` (dùng
   chung path `/app/ielts-lms/app-api` với service Go) — **staging deploy
   qua docker-compose, không qua k8s**.
4. `approve-deploy` — **cổng approve thủ công trên CircleCI**, đây là
   bước duy nhất mang tính "gate chất lượng" trước khi lên production.
5. `deploy-production` — `sed` thay placeholder vào YAML trong
   `provision/k8s/production/`, `kubectl apply`.
6. `tag-version` — tạo GitHub Release + changelog.

**Không có bước lint/type-check/test nào trong CI.** Cổng duy nhất trước
khi code lên production là approve thủ công, không có gate tự động nào
khác — điểm cần lưu ý rõ khi thiết lập quy trình review.

---

## 10. Cron jobs

`app/cron/` có 3 script, chạy bằng `python -m app.cron.<module>`, cùng
được định nghĩa trong **1 file duy nhất**
`provision/k8s/production/05-cronjob.yaml`:

| Cron | Lịch | Việc làm |
|---|---|---|
| `reclaim_stuck_marking_job` | mỗi 10 phút (offset 8) | Tìm review Writing bị kẹt `PROCESSING` > 20 phút, trigger lại pipeline |
| `reclaim_stuck_mock_day_marking_job` | mỗi 10 phút (offset 5) | Tương tự, scope riêng cho Mock Day |
| `db_health_check_job` | hàng ngày 20:10 UTC | Quét DB health, gửi Lark card |

Cả 3 dùng chung pattern `concurrencyPolicy: Forbid`, `backoffLimit: 0`,
`activeDeadlineSeconds: 1800` — có comment giải thích rõ lý do chọn
`Forbid` + không retry thay vì DB advisory lock: tránh chạy chồng lấn dẫn
đến bị tính phí AI 2 lần cho cùng 1 review.

---

## 11. Coding style quan sát được (suy ra từ code, không có tài liệu chính thức)

- **Đặt tên file**: `snake_case`, mỗi file 1 mối quan tâm; các bước pipeline
  đặt tên theo mã giai đoạn (`s0_segment.py`, `s1_check.py`,
  `s25_reconcile.py`...) khớp với giá trị `review.current_step` và số mục
  trong tài liệu spec ngoài repo.
- **Class**: `PascalCase` cho model/service/repository
  (`Answer`, `AIProviderService`, `OpenRouterRepository`).
- **Hàm**: `snake_case`, thường verb-first và khá dài/mô tả rõ
  (`schedule_marking_pipeline`, `require_non_end_user_role`). Nhiều hàm
  service nhận `db: Session` làm **tham số đầu tiên**; class repository thì
  giữ `self.db` từ `__init__`.
- **Docstring/comment**: dùng nhiều để ghi lại *lý do* (rationale), không
  chỉ mô tả hàm làm gì — nhiều đoạn đọc như ADR/giải thích lịch sử fix bug,
  có nơi ghi rõ nguồn gốc là "reviewer request" (bằng chứng review thủ công
  từng diễn ra qua PR, dù không formalize thành quy trình).
- **Quy ước tham chiếu `§x.y`**: comment/docstring trong nhiều file trỏ tới
  số mục của 1 tài liệu spec **nằm ngoài repo** (không có bản copy trong
  `docs/`) — cần biết khi review để không bị lạc khi thấy các số này.
- **Bilingual**: comment/docstring pha trộn VI/EN, và mọi message lỗi cho
  người dùng đều song ngữ theo thiết kế.
- **Logging**: `logging` chuẩn, cấu hình 1 lần ở `app/utils/logger.py`
  (DEBUG–WARNING ra stdout, ERROR+ ra stderr).
- **`app/utils/const.py`**: file hằng số rất lớn (~920 dòng) — có 1 số
  hằng số bị định nghĩa **trùng nhiều lần** trong cùng file (nhiều khả năng
  do conflict merge/copy-paste) — đáng để dọn, không chỉ là style nhỏ.

---

## 12. Quy trình review code hiện tại — và các khoảng trống

**Hiện có:**
- Không có `CODEOWNERS`, không có PR template, không có config branch
  protection trong repo (nếu có thì nằm ở setting GitHub, ngoài repo).
- Quy trình PR có tồn tại trong thực tế (git log có nhiều merge PR, có cả
  case revert/revert-the-revert), branch đặt tên theo `feat/...`,
  `fix/...`, `perf/...`, `revert-<n>-...`.
- 2 bằng chứng review thủ công để lại dấu vết trực tiếp trong code
  (`middleware/authentication.py`, `app/utils/helper.py` — cả 2 đều ghi rõ
  đây là thay đổi theo yêu cầu reviewer).
- Cổng chất lượng tự động duy nhất: approve thủ công trên CircleCI trước
  khi deploy production — **không có lint/type-check/test tự động nào**.

**Gợi ý khi thiết lập/thực hiện review cho repo này** (dựa trên các khoảng
trống ở trên, cần bạn/team xác nhận trước khi áp dụng thành quy tắc chính
thức):
1. Vì không có test tự động trong CI và bản thân `pytest` chưa cài được
   theo `requirements.txt` — review nên tự chạy thử/đọc kỹ thay vì tin vào
   "CI xanh" như tín hiệu chất lượng.
2. Vì không có linter — review cần tự soi các quy ước style đã quan sát ở
   Mục 11 (tên hàm, `db: Session` làm tham số đầu, vị trí comment "why").
3. Migration không có rollback/approver chính thức — review migration nên
   kiểm tra kỹ tính idempotent (`IF NOT EXISTS`) và xác nhận thứ tự chạy
   đúng, đặc biệt với các bảng "existing" (dùng chung với app-api).
4. Các đoạn code trích dẫn `§x.y` cần đối chiếu với tài liệu spec gốc
   (ngoài repo) khi review logic pipeline chấm điểm — bản thân code không
   tự giải thích đủ ngữ cảnh.
5. `error_messages.py` cần giữ đồng bộ thủ công với service Go
   (`common/error_messages.go`) — review thay đổi ở đây nên kiểm tra chéo
   phía Go nếu có thể.
