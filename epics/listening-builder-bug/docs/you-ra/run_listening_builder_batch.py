"""Chạy hàng loạt Listening Builder (Guided Retry): generate/retry -> poll -> (xoá practice-flow cũ nếu retry) -> import.

Danh sách quiz_id (và question_id nếu muốn retry) khai báo trực tiếp trong QUIZZES ở dưới,
không đọc từ file input.

Cách dùng:
    export GUIDED_RETRY_TOKEN="ey..."     # token gọi app-agents.youpass.vn (/guided-retry/*)
    export YOUPASS_API_TOKEN="ey..."      # token gọi api.youpass.vn (/v1/quizzes, /v1/practice-flows)
    python run_listening_builder_batch.py

2 flow, tự động detect theo từng item trong QUIZZES:
1. Generate mới (không có "question_ids" hoặc để rỗng []):
   - Lấy quiz detail (GET /v1/quizzes/{quiz_id}) để lấy toàn bộ part_id + question_set_id.
   - Với mỗi question_set, gọi POST /v1/guided-retry/generate riêng (1 question_set / request).
   - Poll GET /v1/migrate-data/{id} tới completed, rồi POST /v1/guided-retry/process/import.

2. Retry (có "question_ids"):
   - Lấy quiz detail để map mỗi question_id -> (part_id, question_set_id).
   - Gom question_id theo cùng (part_id, question_set_id) — mỗi group = 1 question_set / request.
   - Với mỗi group:
       a. GET /v1/practice-flows?quiz_id=&question_id= (api.youpass.vn) để lấy practice_flow_id cũ
          của từng question_id trong group.
       b. POST /v1/guided-retry/generate với question_set_id + question_ids=group.
       c. Poll tới completed, DELETE /v1/guided-retry/practice-flow với các practice_flow_id cũ.
       d. POST /v1/guided-retry/process/import với migrate_process_id vừa tạo.

Kết quả (thành công/lỗi) được ghi log ra console và lưu JSON tổng kết ra file
`run_listening_builder_batch_result.json` cạnh file input để tra cứu lại.
"""
import base64
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

AGENTS_BASE_URL = "https://app-agents.youpass.vn"
API_BASE_URL = "https://api.youpass.vn"
BATCH_SIZE = 5
POLL_INTERVAL_SEC = 30
POLL_MAX_TRIES = 20  # 10 phút

RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_listening_builder_batch_result.json")

_FALLBACK_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhZG1pbl9hY2Nlc3MiOmZhbHNlLCJhcHBfYWNjZXNzIjpmYWxzZSwiZXhwIjoxNzkwMzI1MTYxLCJpYXQiOjE3OTAwNjU5NjEsImlkIjoiMDMxNzkwMWMtNDA1ZS00NWU4LTlmY2YtNzljZWEwMGQ2OGMwIiwiaXNzIjoiZGlyZWN0dXMiLCJyb2xlIjoiNDliMTliM2EtM2NjZi00OWM5LWExMGItYzc2N2ZiMGRmMzgxIn0.CYBHDBlBc34AbWUFb09SmfMyssy-fKz0-bydU1WvpSE"
)

# ---------------------------------------------------------------------------
# Danh sách quiz cần chạy — sửa trực tiếp ở đây.
#   - Không có "question_ids" (hoặc để []) -> generate mới toàn bộ quiz.
#   - Có "question_ids" -> retry đúng các question đó.
# ---------------------------------------------------------------------------
QUIZZES = [
    {"quiz_id": 10848},
{"quiz_id": 11345},
{"quiz_id": 11335},
{"quiz_id": 11340},
{"quiz_id": 11330},
{"quiz_id": 11325},
{"quiz_id": 11320},
    # {"quiz_id": 11829},
    # {"quiz_id": 8183, "question_ids": [31741, 31745]},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("listening_builder_batch")
_log_lock = threading.Lock()


def _headers(token: str) -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _jwt_exp(token: str) -> Optional[int]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data["exp"]) if data.get("exp") is not None else None
    except Exception:
        return None


def _raise_for_status(resp: requests.Response) -> None:
    if resp.ok:
        return
    try:
        detail = resp.json()
    except Exception:
        detail = (resp.text or "")[:1000]
    raise requests.HTTPError(
        f"{resp.status_code} {resp.reason} for url: {resp.url} | body: {detail}",
        response=resp,
    )


def _append_log(log: list, entry: dict) -> None:
    with _log_lock:
        log.append(entry)


def extract_process_id(gen: dict) -> Optional[int]:
    """API generate trả {processes: [{migrate_process_id, ...}]} (async, 1 process / question_set)."""
    processes = gen.get("processes") or []
    if processes:
        return processes[0].get("migrate_process_id")
    return gen.get("migrate_process_id")


def get_quiz_detail(quiz_id, api_token: str) -> dict:
    resp = requests.get(
        f"{API_BASE_URL}/v1/quizzes/{quiz_id}",
        headers=_headers(api_token),
        timeout=60,
    )
    _raise_for_status(resp)
    return resp.json()["data"]


def find_question_location(quiz_detail: dict, question_id: int):
    """Trả về (part_id, question_set_id) chứa question_id, hoặc (None, None) nếu không tìm thấy."""
    for part in quiz_detail.get("parts") or []:
        for qset in part.get("question_sets") or []:
            for question in qset.get("questions") or []:
                if question.get("id") == question_id:
                    return part["id"], qset["id"]
    return None, None


def get_old_practice_flow_ids(quiz_id, question_id, api_token: str) -> list:
    resp = requests.get(
        f"{API_BASE_URL}/v1/practice-flows",
        headers=_headers(api_token),
        params={"quiz_id": quiz_id, "question_id": question_id},
        timeout=30,
    )
    _raise_for_status(resp)
    body = resp.json()
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, dict):
        data = data.get("data") or data.get("results") or []
    return [row["id"] for row in data if isinstance(row, dict) and "id" in row]


def call_generate(quiz_id, part_id, question_set_id, token: str, question_ids=None) -> dict:
    payload = {
        "quiz_id": str(quiz_id),
        "part_id": int(part_id),
        "question_set_id": int(question_set_id),
    }
    if question_ids:
        payload["question_ids"] = [int(q) for q in question_ids]
    resp = requests.post(
        f"{AGENTS_BASE_URL}/v1/guided-retry/generate",
        headers=_headers(token),
        json=payload,
        timeout=300,
    )
    _raise_for_status(resp)
    return resp.json()


def call_migrate_status(process_id, token: str) -> dict:
    resp = requests.get(
        f"{AGENTS_BASE_URL}/v1/migrate-data/{process_id}",
        headers=_headers(token),
        timeout=30,
    )
    _raise_for_status(resp)
    return resp.json()["data"]


def poll_until_completed(process_id, token: str, quiz_id, question_set_id) -> str:
    """Trả về status cuối: completed | imported | failed | timeout."""
    for attempt in range(1, POLL_MAX_TRIES + 1):
        data = call_migrate_status(process_id, token)
        status = data.get("status")
        logger.info(
            "quiz_id=%s question_set_id=%s process_id=%s poll #%s status=%s",
            quiz_id, question_set_id, process_id, attempt, status,
        )
        if status in ("completed", "imported", "failed", "partial_failed"):
            return status
        time.sleep(POLL_INTERVAL_SEC)
    logger.error(
        "quiz_id=%s question_set_id=%s process_id=%s TIMEOUT sau %s phút",
        quiz_id, question_set_id, process_id, POLL_MAX_TRIES * POLL_INTERVAL_SEC // 60,
    )
    return "timeout"


def call_import(process_ids, token: str) -> dict:
    resp = requests.post(
        f"{AGENTS_BASE_URL}/v1/guided-retry/process/import",
        headers=_headers(token),
        json={"process_ids": process_ids},
        timeout=120,
    )
    _raise_for_status(resp)
    return resp.json()


def call_delete_practice_flows(practice_flow_ids, token: str) -> dict:
    resp = requests.delete(
        f"{AGENTS_BASE_URL}/v1/guided-retry/practice-flow",
        headers=_headers(token),
        json={"ids": practice_flow_ids},
        timeout=60,
    )
    _raise_for_status(resp)
    return resp.json()


def generate_and_import(
    quiz_id, part_id, question_set_id, agents_token: str, log: list, question_ids=None,
) -> Optional[int]:
    """Generate 1 question_set, poll tới completed, import. Trả về process_id nếu import ok."""
    gen = call_generate(quiz_id, part_id, question_set_id, agents_token, question_ids=question_ids)
    process_id = extract_process_id(gen)
    if not process_id:
        logger.warning(
            "quiz_id=%s part_id=%s question_set_id=%s generate không trả process "
            "(set đã imported hoặc không có question) — bỏ qua. raw=%s",
            quiz_id, part_id, question_set_id, gen,
        )
        _append_log(log, {
            "quiz_id": quiz_id, "part_id": part_id, "question_set_id": question_set_id,
            "status": "skipped", "error": "no migrate_process_id in generate response",
            "raw": gen,
        })
        return None

    logger.info(
        "quiz_id=%s part_id=%s question_set_id=%s generate ok, migrate_process_id=%s",
        quiz_id, part_id, question_set_id, process_id,
    )
    final_status = poll_until_completed(process_id, agents_token, quiz_id, question_set_id)
    if final_status == "imported":
        logger.info(
            "quiz_id=%s question_set_id=%s process_id=%s đã imported sẵn",
            quiz_id, question_set_id, process_id,
        )
        _append_log(log, {
            "quiz_id": quiz_id, "part_id": part_id, "question_set_id": question_set_id,
            "migrate_process_id": process_id, "status": "imported",
        })
        return process_id
    if final_status != "completed":
        raise RuntimeError(f"generate chưa completed (status={final_status}, process_id={process_id})")

    import_result = call_import([process_id], agents_token)
    logger.info(
        "quiz_id=%s question_set_id=%s import result=%s",
        quiz_id, question_set_id, import_result.get("message"),
    )
    _append_log(log, {
        "quiz_id": quiz_id, "part_id": part_id, "question_set_id": question_set_id,
        "migrate_process_id": process_id, "status": "imported",
        "question_ids": question_ids,
    })
    return process_id


def run_generate_flow(quiz_id, quiz_detail, agents_token: str, log: list) -> None:
    for part in quiz_detail.get("parts") or []:
        part_id = part["id"]
        question_sets = part.get("question_sets") or []
        if not question_sets:
            continue
        for qset in question_sets:
            question_set_id = qset["id"]
            try:
                generate_and_import(quiz_id, part_id, question_set_id, agents_token, log)
            except Exception as e:
                logger.exception(
                    "quiz_id=%s part_id=%s question_set_id=%s generate/import FAILED: %s",
                    quiz_id, part_id, question_set_id, e,
                )
                _append_log(log, {
                    "quiz_id": quiz_id, "part_id": part_id, "question_set_id": question_set_id,
                    "status": "error", "error": str(e),
                })


def run_retry_flow(quiz_id, quiz_detail, question_ids, agents_token: str, api_token: str, log: list) -> None:
    groups = {}
    for question_id in question_ids:
        part_id, question_set_id = find_question_location(quiz_detail, question_id)
        if part_id is None:
            logger.error("quiz_id=%s question_id=%s không tìm thấy trong quiz detail, bỏ qua", quiz_id, question_id)
            _append_log(log, {
                "quiz_id": quiz_id, "question_id": question_id,
                "status": "error", "error": "question_id not found in quiz detail",
            })
            continue
        groups.setdefault((part_id, question_set_id), []).append(question_id)

    for (part_id, question_set_id), grouped_question_ids in groups.items():
        try:
            old_practice_flow_ids = []
            for question_id in grouped_question_ids:
                old_practice_flow_ids += get_old_practice_flow_ids(quiz_id, question_id, api_token)

            process_id = generate_and_import(
                quiz_id, part_id, question_set_id, agents_token, log,
                question_ids=grouped_question_ids,
            )
            if not process_id:
                continue

            if old_practice_flow_ids:
                call_delete_practice_flows(old_practice_flow_ids, agents_token)
                logger.info(
                    "quiz_id=%s question_ids=%s đã xoá %s practice_flow cũ: %s",
                    quiz_id, grouped_question_ids, len(old_practice_flow_ids), old_practice_flow_ids,
                )
        except Exception as e:
            logger.exception(
                "quiz_id=%s part_id=%s question_set_id=%s question_ids=%s retry/import FAILED: %s",
                quiz_id, part_id, question_set_id, grouped_question_ids, e,
            )
            _append_log(log, {
                "quiz_id": quiz_id, "part_id": part_id, "question_set_id": question_set_id,
                "question_ids": grouped_question_ids, "status": "error", "error": str(e),
            })


def process_item(item: dict, agents_token: str, api_token: str, log: list) -> None:
    quiz_id = item["quiz_id"]
    question_ids = item.get("question_ids") or []
    try:
        quiz_detail = get_quiz_detail(quiz_id, api_token)
    except Exception as e:
        logger.exception("quiz_id=%s lấy quiz detail FAILED: %s", quiz_id, e)
        _append_log(log, {"quiz_id": quiz_id, "status": "error", "error": f"quiz detail fetch failed: {e}"})
        return

    if question_ids:
        run_retry_flow(quiz_id, quiz_detail, question_ids, agents_token, api_token, log)
    else:
        run_generate_flow(quiz_id, quiz_detail, agents_token, log)


def run(items: list, agents_token: str, api_token: str) -> None:
    log: list = []
    logger.info("=== Chạy %s quiz (tối đa %s song song, 1 question_set / request): %s ===",
                len(items), BATCH_SIZE, [it["quiz_id"] for it in items])
    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
        futures = [executor.submit(process_item, it, agents_token, api_token, log) for it in items]
        for f in futures:
            f.result()

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    logger.info("Hoàn tất. Kết quả chi tiết: %s", RESULT_FILE)
    for entry in log:
        logger.info("  %s", entry)


def main():
    agents_token = os.environ.get("GUIDED_RETRY_TOKEN") or _FALLBACK_TOKEN
    api_token = os.environ.get("YOUPASS_API_TOKEN") or _FALLBACK_TOKEN

    exp = _jwt_exp(agents_token)
    if exp is None:
        logger.error("GUIDED_RETRY_TOKEN không phải JWT hợp lệ — export token mới rồi chạy lại.")
        sys.exit(1)
    if exp <= time.time():
        logger.error(
            "GUIDED_RETRY_TOKEN đã hết hạn (exp=%s). Export token mới rồi chạy lại:\n"
            "  export GUIDED_RETRY_TOKEN='ey...'\n"
            "  export YOUPASS_API_TOKEN='ey...'",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)),
        )
        sys.exit(1)

    if not QUIZZES:
        logger.error("QUIZZES đang rỗng — khai báo danh sách quiz_id/question_id trực tiếp trong script.")
        sys.exit(1)

    run(QUIZZES, agents_token, api_token)


if __name__ == "__main__":
    main()
