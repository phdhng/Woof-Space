"""Verify các giả thuyết root cause cho bucket MAPPING_OFF + NO_AUDIO trong
report_diagnosis.json (READ-ONLY, production). Mỗi hàm `verify_<tên hàm thật>`
test đúng 1 hàm thật được copy nguyên văn từ app-agents-service ở ngay phía trên nó.

Cách dùng:
    export YOUPASS_API_TOKEN="ey..."
    python verify_root_causes.py

Output: verify_root_causes_result.json cạnh file này.
"""
import json
import os
import re
import time
from typing import Any, Optional

import requests

API_BASE_URL = "https://api.youpass.vn"
HERE = os.path.dirname(os.path.abspath(__file__))
DIAGNOSIS_JSON = os.path.join(HERE, "report_diagnosis.json")
OUT_JSON = os.path.join(HERE, "verify_root_causes_result.json")

_FALLBACK_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhZG1pbl9hY2Nlc3MiOmZhbHNlLCJhcHBfYWNjZXNzIjpmYWxzZSwiZXhwIjoxNzkwMzMxNTUyLCJpYXQiOjE3OTAzMjA3NTIsImlkIjoiMDMxNzkwMWMtNDA1ZS00NWU4LTlmY2YtNzljZWEwMGQ2OGMwIiwiaXNzIjoiZGlyZWN0dXMiLCJyb2xlIjoiNDliMTliM2EtM2NjZi00OWM5LWExMGItYzc2N2ZiMGRmMzgxIn0.IuqLPyqocG5-LkAPUTjOs6gLMwaMBvoqJ1QdX5ydp94"
)


class _NoopLogger:
    def warning(self, *a, **k):
        pass


logger = _NoopLogger()


# =============================================================================
# Copy nguyên văn từ guided_retry_ai_agent.py:52 (normalize_question_correct_answers)
# =============================================================================
def normalize_question_correct_answers(question: dict) -> list:
    raw = question.get("correct_answers")
    if raw is not None:
        if isinstance(raw, list):
            out = [str(v).strip() for v in raw if v is not None and str(v).strip()]
            if out:
                return out
        else:
            s = str(raw).strip()
            if s:
                return [s]
    single = question.get("correct_answer")
    if single is not None:
        s = str(single).strip()
        if s:
            return [s]
    return []


# =============================================================================
# Copy nguyên văn từ guided_retry_service.py:1066 (_matching_search_terms)
# =============================================================================
def _matching_search_terms(question: dict, question_set: dict) -> list:
    terms: list = []
    item_text = re.sub(r"<[^>]+>", "", question.get("text") or question.get("content") or "").strip()
    if item_text:
        terms.append(item_text)

    ans_options = question.get("options") or question_set.get("options") or []
    answer_letters = normalize_question_correct_answers(question)
    for letter in answer_letters:
        opt_text = next(
            (
                o.get("text")
                for o in ans_options
                if isinstance(o, dict)
                and str(o.get("option") or "").strip().upper() == str(letter).strip().upper()
            ),
            None,
        )
        if opt_text:
            clean_opt = re.sub(r"<[^>]+>", "", str(opt_text)).strip()
            if clean_opt and clean_opt not in terms:
                terms.append(clean_opt)

    return terms or list(answer_letters)


def build_search_list(question: dict, question_set: dict) -> list:
    """Copy nguyên văn logic build search_list từ guided_retry_service.py:429-443
    (phần gọi _extract_vocab_data trong _build_question_context) — bản đã sửa
    #1286 (check question.question_type) + #1287 (thêm MAP_DIAGRAM_LABEL vào
    nhánh _matching_search_terms, bỏ block content/name cũ)."""
    qset_type_upper = (question_set.get("question_type") or "").upper()
    question_type_upper = (question.get("question_type") or "").upper()
    if qset_type_upper in ("MATCHING_FEATURES", "MATCHING_ENDINGS", "NOTE_COMPLETION") or (
        question_type_upper in ("MULTIPLE_CHOICE_ONE", "MULTIPLE_CHOICE_MANY", "MAP_DIAGRAM_LABEL")
    ):
        search_list = _matching_search_terms(question, question_set)
    else:
        search_list = list(normalize_question_correct_answers(question))
    return search_list


def resolve_from_paragraph_ranges(question: dict, vocabs: list) -> tuple:
    """Copy nguyên văn từ guided_retry_service.py (_resolve_from_paragraph_ranges)."""
    locate_info = question.get("locate_info")
    if not isinstance(locate_info, dict):
        return None, None
    ranges = locate_info.get("paragraph_ranges")
    if not isinstance(ranges, list) or not ranges:
        return None, None

    r = ranges[0] if isinstance(ranges[0], dict) else {}
    start, end = r.get("start") or {}, r.get("end") or {}
    p_start, p_end = start.get("paragraph"), end.get("paragraph")
    s_start, s_end = start.get("sentence"), end.get("sentence")
    if None in (p_start, p_end, s_start, s_end):
        return None, None
    if p_start != p_end:
        return None, None

    p_idx = p_start - 1
    if p_idx < 0 or p_idx >= len(vocabs):
        return None, None
    paragraph = vocabs[p_idx]
    children = paragraph.get("children") or []

    lo, hi = s_start - 1, s_end - 1
    if lo < 0 or hi >= len(children) or lo > hi:
        return None, None
    span = [s for s in children[lo : hi + 1] if isinstance(s, dict)]
    if not span:
        return None, None

    froms = [
        s["meta"]["from"]
        for s in span
        if isinstance(s.get("meta"), dict) and s["meta"].get("from") is not None
    ]
    tos = [
        s["meta"]["to"]
        for s in span
        if isinstance(s.get("meta"), dict) and s["meta"].get("to") is not None
    ]
    if not froms or not tos:
        return None, None

    sentence = {
        "value": " ".join((s.get("value") or "").strip() for s in span).strip(),
        "meta": {"from": min(froms), "to": max(tos)},
    }
    return sentence, paragraph


# =============================================================================
# Copy nguyên văn từ guided_retry_service.py:1099 (_extract_vocab_data)
# Hàm gốc khai báo `self` nhưng không dùng — bỏ tham số này khi tách ra đây.
# (bản đọc ngày 2026-09-24 — nếu source đổi, cần copy lại để test còn đúng nghĩa)
# =============================================================================
def _extract_vocab_data(vocabs: list, correct_answers: list):
    all_sentence_values: list = []
    all_candidates: list = []

    normalized_answers = [a.strip().lower() for a in correct_answers if a]

    for paragraph in vocabs:
        children = paragraph.get("children") or []
        for sentence in children:
            val = sentence.get("value") or ""
            if val:
                all_sentence_values.append(val)
                all_candidates.append((sentence, paragraph))

    all_sentences_text = "\n".join(all_sentence_values)

    if not normalized_answers:
        return None, None, all_sentences_text

    boundary_patterns = []
    for ans in normalized_answers:
        try:
            boundary_patterns.append(re.compile(r'\b' + re.escape(ans) + r'\b', re.IGNORECASE))
        except re.error:
            boundary_patterns.append(None)

    exact_matches: list = []
    for sentence, paragraph in all_candidates:
        val = sentence.get("value") or ""
        if not val:
            continue
        for pattern in boundary_patterns:
            if pattern and pattern.search(val):
                exact_matches.append((sentence, paragraph))
                break

    if exact_matches:
        answer_sentence, answer_paragraph = exact_matches[-1]
        return answer_sentence, answer_paragraph, all_sentences_text

    substring_matches: list = []
    substring_answers = [a for a in normalized_answers if len(a) > 2]
    for sentence, paragraph in all_candidates:
        val = sentence.get("value") or ""
        if not val:
            continue
        val_lower = val.lower()
        if any(ans in val_lower for ans in substring_answers):
            substring_matches.append((sentence, paragraph))

    if substring_matches:
        answer_sentence, answer_paragraph = substring_matches[-1]
        logger.warning(
            "[GuidedRetry] _extract_vocab_data: exact word-boundary match failed, "
            "fell back to substring match for answers=%s",
            normalized_answers,
        )
        return answer_sentence, answer_paragraph, all_sentences_text

    return None, None, all_sentences_text


# =============================================================================
# Copy nguyên văn từ guided_retry_ai_agent.py:2370-2493
# (_FILLER_WORDS_RE, _normalize_for_timestamp_match, _best_sentence_meta_for_fragment,
#  _ABBREVIATIONS, _transcription_to_fragments, _lookup_timestamps)
# =============================================================================
_FILLER_WORDS_RE = re.compile(
    r"\b(?:uh|um|er|erm|hmm|you know|i mean|like|well|so|basically|actually|right)\b",
    re.IGNORECASE,
)


def _normalize_for_timestamp_match(s: str) -> str:
    text = (s or "").strip().lower()
    text = _FILLER_WORDS_RE.sub("", text)
    text = re.sub(r"[^\w\s']", "", text)
    return " ".join(text.split())


def _best_sentence_meta_for_fragment(fragment: str, sentences: list, match_threshold: float):
    import difflib

    fn = _normalize_for_timestamp_match(fragment)
    if not fn:
        return None
    best_ratio = 0.0
    best_from: Optional[float] = None
    best_to: Optional[float] = None
    for s in sentences:
        val = (s.get("value") or "").strip()
        if not val:
            continue
        ratio = difflib.SequenceMatcher(None, fn, _normalize_for_timestamp_match(val)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
            fr, to = meta.get("from"), meta.get("to")
            if fr is not None and to is not None:
                try:
                    best_from, best_to = float(fr), float(to)
                except (TypeError, ValueError):
                    best_from, best_to = None, None
            else:
                best_from, best_to = None, None
    if best_ratio >= match_threshold and best_from is not None and best_to is not None:
        return (best_from, best_to)
    return None


_ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "st", "no", "vs", "etc", "approx", "dept", "govt", "inc", "ltd", "jr", "sr"}


def _transcription_to_fragments(text: str) -> list:
    s = (text or "").strip()
    if not s:
        return []
    normalized = re.sub(r"\s+", " ", s)
    parts: list = []
    current = ""
    tokens = re.split(r'([.!?])\s+', normalized)
    i = 0
    while i < len(tokens):
        current += tokens[i]
        if i + 1 < len(tokens) and tokens[i + 1] in ".!?":
            punct = tokens[i + 1]
            current += punct
            last_word = re.findall(r'\b(\w+)\s*$', tokens[i])
            if last_word and last_word[-1].lower() in _ABBREVIATIONS:
                if i + 2 < len(tokens):
                    current += " "
                i += 2
                continue
            parts.append(current.strip())
            current = ""
            i += 2
        else:
            i += 1
    if current.strip():
        parts.append(current.strip())
    fragments = [p for p in parts if p]
    return fragments if fragments else [normalized]


def _lookup_timestamps(text, sentences: list, match_threshold: float = 0.75):
    if not sentences:
        return (None, None)

    if isinstance(text, list):
        fragments = [str(t).strip() for t in text if t and str(t).strip()]
    else:
        fragments = _transcription_to_fragments(str(text))

    if not fragments:
        return (None, None)

    metas: list = []
    for frag in fragments:
        m = _best_sentence_meta_for_fragment(frag, sentences, match_threshold)
        if m is not None:
            metas.append(m)

    if metas and len(metas) >= max(1, len(fragments) * 0.5):
        return (metas[0][0], metas[-1][1])

    return (None, None)
# ============================= hết phần copy =============================


def _to_float(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def paragraph_time_range(paragraph: Optional[dict]) -> Optional[tuple]:
    if not paragraph:
        return None
    starts, ends = [], []
    for s in paragraph.get("children") or []:
        meta = s.get("meta") if isinstance(s.get("meta"), dict) else {}
        fr, to = _to_float(meta.get("from")), _to_float(meta.get("to"))
        if fr is not None:
            starts.append(fr)
        if to is not None:
            ends.append(to)
    if not starts or not ends:
        return None
    return (min(starts), max(ends))


def flatten_level2_sentences(vocabs: list) -> list:
    """Copy nguyên văn từ guided_retry_service.py:1054 (_flatten_level2_sentences)."""
    out: list = []
    for paragraph in vocabs or []:
        for sentence in paragraph.get("children") or []:
            if isinstance(sentence, dict) and (sentence.get("value") or "").strip():
                out.append(sentence)
    return out


def true_range_from_dictation_item(dictation_item: dict) -> Optional[tuple]:
    m = dictation_item.get("match") or {}
    tf, tt = m.get("true_from"), m.get("true_to")
    if tf is None or tt is None:
        return None
    return (tf, tt)


def true_range_from_diagnosis(entry: dict) -> Optional[tuple]:
    """Union [min true_from, max true_to] của mọi dictation item đã match được vị trí thật."""
    froms, tos = [], []
    for d in entry.get("dictation") or []:
        r = true_range_from_dictation_item(d)
        if r:
            froms.append(r[0])
            tos.append(r[1])
    if not froms or not tos:
        return None
    return (min(froms), max(tos))


DELTA_BIG = 1.5  # giây — cùng ngưỡng MAPPING_OFF trong diagnose_reports.py


def range_contains(outer: tuple, inner: tuple, slack: float = 2.0) -> bool:
    """outer (paragraph range) có chứa trọn inner (true range), cho phép sai số nhỏ ở biên."""
    return outer[0] - slack <= inner[0] and inner[1] <= outer[1] + slack


def ranges_close(a: tuple, b: tuple, threshold: float = DELTA_BIG) -> bool:
    """Cả 2 đầu (start/end) đều lệch không quá threshold — dùng cho so khớp vị trí chính xác,
    KHÔNG dùng overlap đơn thuần (1 đầu trùng, đầu kia lệch xa vẫn coi là overlap -> sai)."""
    return abs(a[0] - b[0]) <= threshold and abs(a[1] - b[1]) <= threshold


class Client:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Authorization": f"Bearer {token}"})
        self._cache: dict = {}

    def quiz_detail(self, quiz_id: int) -> dict:
        if quiz_id not in self._cache:
            r = self.s.get(f"{API_BASE_URL}/v1/quizzes/{quiz_id}", params={"included_vocabs": "true"}, timeout=60)
            r.raise_for_status()
            self._cache[quiz_id] = r.json()["data"]
            time.sleep(0.3)
        return self._cache[quiz_id]


def find_part_and_question(quiz_detail: dict, question_id: int):
    for part in quiz_detail.get("parts") or []:
        for qs in part.get("question_sets") or []:
            for q in qs.get("questions") or []:
                if q.get("id") == question_id:
                    return part, qs, q
    return None, None, None


# =============================================================================
# Verify hàm 1: _extract_vocab_data có neo đúng paragraph không.
# =============================================================================
def verify_extract_vocab_data(vocabs: list, q: dict, qs: dict, entry: dict) -> dict:
    search_list = build_search_list(q, qs)
    row: dict = {"search_list": search_list}

    answer_sentence, answer_paragraph, _ = _extract_vocab_data(vocabs, search_list)
    if (q.get("question_type") or "").upper() == "MAP_DIAGRAM_LABEL":
        resolved_sentence, resolved_paragraph = resolve_from_paragraph_ranges(q, vocabs)
        if resolved_sentence is not None:
            answer_sentence, answer_paragraph = resolved_sentence, resolved_paragraph
            row["resolved_via"] = "paragraph_ranges"

    if answer_sentence is None:
        row["verdict"] = "NO_MATCH_IN_EXTRACT"
        return row

    if row.get("resolved_via") == "paragraph_ranges":
        # Đã có range chính xác cấp sentence (không phải fallback cả paragraph) —
        # so thẳng meta của answer_sentence, không nới ra cả paragraph.
        meta = answer_sentence.get("meta") or {}
        pred_range = (meta.get("from"), meta.get("to")) if meta.get("from") is not None and meta.get("to") is not None else None
    else:
        pred_range = paragraph_time_range(answer_paragraph)
    true_range = true_range_from_diagnosis(entry)
    row["answer_sentence_value"] = answer_sentence.get("value")
    row["predicted_paragraph_range"] = pred_range
    row["true_range"] = true_range

    if true_range is None:
        row["verdict"] = "NO_TRUE_RANGE_TO_COMPARE"
    elif pred_range is None:
        row["verdict"] = "NO_PREDICTED_RANGE"
    elif row.get("resolved_via") == "paragraph_ranges":
        # pred_range giờ hẹp (cấp sentence) — kiểm tra pred có NẰM TRONG true_range
        # (thường rộng hơn, gộp từ nhiều dictation), ngược hướng so với nhánh fallback
        # cả paragraph (paragraph rộng hơn true_range).
        if range_contains(true_range, pred_range):
            row["verdict"] = "PARAGRAPH_OK"
        else:
            row["verdict"] = "PARAGRAPH_MISMATCH"
    elif range_contains(pred_range, true_range):
        row["verdict"] = "PARAGRAPH_OK"
    else:
        row["verdict"] = "PARAGRAPH_MISMATCH"
    return row


# =============================================================================
# Verify hàm 3 (đánh số trước hàm 2 vì cùng cấp câu-hỏi với hàm 1): question.locate_info
# .time_ranges — range QC-verified có sẵn trong data, hiện chỉ được dùng ở đúng 1 chỗ
# (_format_matching_step_3, guided_retry_ai_agent.py:3423) thay vì làm nguồn chính cho
# _build_question_context. Test xem range này có đáng tin hơn text-match không.
# =============================================================================
def verify_locate_info_time_ranges(q: dict, entry: dict) -> dict:
    li = q.get("locate_info")
    tr = li.get("time_ranges") if isinstance(li, dict) else None
    if not isinstance(tr, dict) or tr.get("from") is None or tr.get("to") is None:
        return {"verdict": "NO_LOCATE_INFO"}

    try:
        pred_range = (float(tr["from"]), float(tr["to"]))
    except (TypeError, ValueError):
        return {"verdict": "NO_LOCATE_INFO"}

    true_range = true_range_from_diagnosis(entry)
    row = {"predicted_range": pred_range, "true_range": true_range}
    if true_range is None:
        row["verdict"] = "NO_TRUE_RANGE_TO_COMPARE"
    elif range_contains(pred_range, true_range) or ranges_close(pred_range, true_range, threshold=3.0):
        row["verdict"] = "LOCATE_INFO_OK"
    else:
        row["verdict"] = "LOCATE_INFO_MISMATCH"
    return row


# =============================================================================
# Verify hàm 2: với TỪNG câu DICTATION, _lookup_timestamps (search fragment
# trên TOÀN Part, không giới hạn vị trí) có ra đúng khoảng audio thật không.
# Chạy trên chính all_vocab_sentences thật của Part (giống context.sentences_for_
# timestamp_lookup trong code thật khi all_vocab_sentences không rỗng).
# =============================================================================
def verify_lookup_timestamps(all_vocab_sentences: list, entry: dict) -> list:
    rows = []
    for d in entry.get("dictation") or []:
        ca0 = d.get("correct_answer_0") or ""
        true_range = true_range_from_dictation_item(d)
        row = {
            "practice_question_id": d.get("practice_question_id"),
            "correct_answer_0": ca0[:120],
            "true_range": true_range,
        }
        if not ca0.strip():
            row["verdict"] = "EMPTY_CORRECT_ANSWER"
            rows.append(row)
            continue

        lu_from, lu_to = _lookup_timestamps(ca0, all_vocab_sentences)
        pred_range = (lu_from, lu_to) if lu_from is not None and lu_to is not None else None
        row["predicted_range"] = pred_range

        if pred_range is None:
            row["verdict"] = "LOOKUP_NO_MATCH"
        elif true_range is None:
            row["verdict"] = "NO_TRUE_RANGE_TO_COMPARE"
        elif ranges_close(pred_range, true_range):
            row["verdict"] = "LOOKUP_OK"
        else:
            row["verdict"] = "LOOKUP_MISMATCH"
        rows.append(row)
    return rows


# =============================================================================
# ĐỀ XUẤT (không phải code thật): thay "chọn match cuối cùng trong toàn Part"
# bằng "chọn match gần nhất với câu hỏi liền trước đã anchor được", dựa trên
# giả định audio IELTS luôn phát theo 1 chiều nên thứ tự câu hỏi trong 1
# question_set gần như luôn theo đúng thứ tự xuất hiện trong bài nghe.
# =============================================================================
def extract_vocab_data_candidate_monotonic(vocabs: list, correct_answers: list, cursor: int):
    """Trả (answer_sentence, answer_paragraph, paragraph_index) hoặc (None, None, None)."""
    all_candidates: list = []  # (sentence, paragraph, paragraph_index)
    for idx, paragraph in enumerate(vocabs):
        for sentence in paragraph.get("children") or []:
            if sentence.get("value"):
                all_candidates.append((sentence, paragraph, idx))

    normalized_answers = [a.strip().lower() for a in correct_answers if a]
    if not normalized_answers:
        return None, None, None

    boundary_patterns = []
    for ans in normalized_answers:
        try:
            boundary_patterns.append(re.compile(r'\b' + re.escape(ans) + r'\b', re.IGNORECASE))
        except re.error:
            boundary_patterns.append(None)

    def _pick_nearest(cands):
        # Ưu tiên paragraph_index >= cursor (đi tới, đúng chiều audio); trong nhóm đó
        # chọn gần cursor nhất. Nếu không có gì >= cursor, đành lấy gần cursor nhất
        # về phía trước (trường hợp hiếm — câu hỏi tham chiếu ngược).
        return min(cands, key=lambda c: (0 if c[2] >= cursor else 1, abs(c[2] - cursor)))

    exact_matches = []
    for sentence, paragraph, idx in all_candidates:
        val = sentence.get("value") or ""
        for pattern in boundary_patterns:
            if pattern and pattern.search(val):
                exact_matches.append((sentence, paragraph, idx))
                break
    if exact_matches:
        return _pick_nearest(exact_matches)

    substring_answers = [a for a in normalized_answers if len(a) > 2]
    substring_matches = [
        (sentence, paragraph, idx)
        for sentence, paragraph, idx in all_candidates
        if any(ans in (sentence.get("value") or "").lower() for ans in substring_answers)
    ]
    if substring_matches:
        return _pick_nearest(substring_matches)

    return None, None, None


def verify_extract_vocab_data_candidate_monotonic(vocabs: list, qs: dict, target_question_id: int, entry: dict) -> dict:
    """Chạy candidate fix TUẦN TỰ qua mọi câu hỏi trong cùng question_set (theo `order`),
    dời cursor sau mỗi câu — đúng mô phỏng cách nó sẽ chạy thật trong generate flow
    (từng question_set xử lý theo thứ tự câu hỏi)."""
    questions = sorted(qs.get("questions") or [], key=lambda q: q.get("order", 0))
    cursor = 0
    target_row: Optional[dict] = None

    for q in questions:
        search_list = build_search_list(q, qs)
        answer_sentence, answer_paragraph, idx = extract_vocab_data_candidate_monotonic(
            vocabs, search_list, cursor
        )
        if idx is not None:
            cursor = idx

        if q.get("id") == target_question_id:
            if answer_sentence is None:
                target_row = {"verdict": "NO_MATCH_IN_EXTRACT"}
                continue
            pred_range = paragraph_time_range(answer_paragraph)
            true_range = true_range_from_diagnosis(entry)
            target_row = {"predicted_paragraph_range": pred_range, "true_range": true_range}
            if true_range is None:
                target_row["verdict"] = "NO_TRUE_RANGE_TO_COMPARE"
            elif pred_range is None:
                target_row["verdict"] = "NO_PREDICTED_RANGE"
            elif range_contains(pred_range, true_range):
                target_row["verdict"] = "PARAGRAPH_OK"
            else:
                target_row["verdict"] = "PARAGRAPH_MISMATCH"

    return target_row or {"verdict": "QUESTION_NOT_IN_SET"}


def main():
    token = os.environ.get("YOUPASS_API_TOKEN") or _FALLBACK_TOKEN
    with open(DIAGNOSIS_JSON, encoding="utf-8") as f:
        diagnosis = json.load(f)

    targets = [d for d in diagnosis if d.get("primary_bucket") in ("MAPPING_OFF", "NO_AUDIO")]
    print(f"{len(targets)} câu thuộc MAPPING_OFF/NO_AUDIO cần verify.")

    client = Client(token)
    results = []
    for i, entry in enumerate(targets, 1):
        quiz_id, question_id = entry["quiz_id"], entry["question_id"]
        row: dict = {
            "quiz_id": quiz_id,
            "question_id": question_id,
            "primary_bucket": entry.get("primary_bucket"),
        }
        try:
            qd = client.quiz_detail(quiz_id)
            part, qs, q = find_part_and_question(qd, question_id)
            if part is None:
                row["extract_vocab_data"] = {"verdict": "FETCH_ERROR"}
                row["lookup_timestamps"] = []
                row["error"] = "question_id không có trong quiz detail"
                results.append(row)
                print(f"[{i}/{len(targets)}] quiz={quiz_id} q={question_id} -> FETCH_ERROR")
                continue

            vocabs = part.get("vocabs") or []
            all_vocab_sentences = flatten_level2_sentences(vocabs)

            row["extract_vocab_data"] = verify_extract_vocab_data(vocabs, q, qs, entry)
            row["lookup_timestamps"] = verify_lookup_timestamps(all_vocab_sentences, entry)
            row["locate_info_time_ranges"] = verify_locate_info_time_ranges(q, entry)
            row["extract_vocab_data_candidate_monotonic"] = verify_extract_vocab_data_candidate_monotonic(
                vocabs, qs, question_id, entry
            )

        except requests.HTTPError as e:
            row["extract_vocab_data"] = {"verdict": "FETCH_ERROR"}
            row["lookup_timestamps"] = []
            row["locate_info_time_ranges"] = {"verdict": "FETCH_ERROR"}
            row["extract_vocab_data_candidate_monotonic"] = {"verdict": "FETCH_ERROR"}
            row["error"] = f"{e.response.status_code} {e.response.reason}"
        except Exception as e:
            row["extract_vocab_data"] = {"verdict": "FETCH_ERROR"}
            row["lookup_timestamps"] = []
            row["locate_info_time_ranges"] = {"verdict": "FETCH_ERROR"}
            row["extract_vocab_data_candidate_monotonic"] = {"verdict": "FETCH_ERROR"}
            row["error"] = repr(e)

        results.append(row)
        lu_verdicts = ",".join(r["verdict"] for r in row["lookup_timestamps"]) or "-"
        print(f"[{i}/{len(targets)}] quiz={quiz_id} q={question_id} "
              f"-> extract={row['extract_vocab_data']['verdict']} lookup=[{lu_verdicts}] "
              f"locate_info={row['locate_info_time_ranges']['verdict']} "
              f"candidate={row['extract_vocab_data_candidate_monotonic']['verdict']}")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    extract_counts: dict = {}
    lookup_counts: dict = {}
    locate_info_counts: dict = {}
    candidate_counts: dict = {}
    for r in results:
        v = r["extract_vocab_data"]["verdict"]
        extract_counts[v] = extract_counts.get(v, 0) + 1
        for lu in r["lookup_timestamps"]:
            lv = lu["verdict"]
            lookup_counts[lv] = lookup_counts.get(lv, 0) + 1
        liv = r["locate_info_time_ranges"]["verdict"]
        locate_info_counts[liv] = locate_info_counts.get(liv, 0) + 1
        cv = r["extract_vocab_data_candidate_monotonic"]["verdict"]
        candidate_counts[cv] = candidate_counts.get(cv, 0) + 1

    print("\n=== Tổng kết verify_extract_vocab_data (theo câu hỏi) ===")
    for k, v in sorted(extract_counts.items()):
        print(f"  {k}: {v}")

    print("\n=== Tổng kết verify_lookup_timestamps (theo câu DICTATION) ===")
    for k, v in sorted(lookup_counts.items()):
        print(f"  {k}: {v}")

    print("\n=== Tổng kết verify_locate_info_time_ranges (theo câu hỏi) ===")
    for k, v in sorted(locate_info_counts.items()):
        print(f"  {k}: {v}")

    print("\n=== Tổng kết verify_extract_vocab_data_candidate_monotonic (theo câu hỏi) ===")
    for k, v in sorted(candidate_counts.items()):
        print(f"  {k}: {v}")

    print(f"\nChi tiết: {OUT_JSON}")


if __name__ == "__main__":
    main()
