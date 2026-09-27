"""Chẩn đoán (READ-ONLY) các report lỗi Listening Builder / Guided Retry trên PRODUCTION.

KHÔNG ghi/sửa bất kỳ dữ liệu production nào. Chỉ gọi GET, log, phân loại.

Nguồn:
  - docs/you-ra/bug_reported.md  (mỗi dòng 1 report; có thể trùng question)
  - GET https://api.youpass.vn/v1/quizzes/{quiz_id}?included_vocabs=true   -> quiz/part/vocab(L1->L2 meta.from/to = transcript+timestamp)/question_set/question
  - GET https://api.youpass.vn/v1/practice-flows?quiz_id=&question_id=      -> practice_flow đã import (audio_start/end, correct_answers, suggested_vocabs)
    (KHÔNG truyền answer_id: answer_id gắn với 1 user/answer cụ thể, sẽ fail validate khi quét nhiều quiz.)

Cách dùng:
    export YOUPASS_API_TOKEN="ey..."      # token gọi api.youpass.vn
    python diagnose_reports.py            # quét toàn bộ bug_reported.md
    python diagnose_reports.py 23321 23969  # chỉ quét vài question_id

Output (cạnh script):
    report_diagnosis.json   -- chi tiết từng (quiz_id, question_id) + từng câu DICTATION
    report_diagnosis.md     -- bảng tổng hợp: đếm theo bucket, mức khớp với lý do học sinh, list question_id mỗi bucket

Bucket phân loại (ưu tiên từ trên xuống khi 1 câu dính nhiều dấu hiệu):
    NO_AUDIO        #3  DICTATION có audio_start==audio_end (thường 0/0) hoặc start>=end
    TRANSCRIPT_WRONG#1  correct_answers[0] của DICTATION không khớp câu L2 nào của part (sim < SIM_WRONG)
    MAPPING_OFF     #5  khớp 1 câu L2 (sim >= SIM_MATCH) nhưng [audio_start,audio_end] lệch câu đó > DELTA_BIG giây ở 1 đầu
    CLIP           #2  khớp câu L2, lệch <= DELTA_BIG nhưng > DELTA_SMALL (thiếu/thừa chữ đầu-cuối)
    TRANSLATE_MISSING#4 mọi DICTATION step: suggested_vocabs rỗng/null hoặc mọi "meaning" rỗng
    CANT_TYPE_DATA #6  DICTATION có correct_answers rỗng/[""]
    REVIEW             sim trong [SIM_WRONG, SIM_MATCH) — cần xem tay
    OK                không thấy bất thường ở dữ liệu (khả năng lỗi FE hoặc file audio)
"""
import base64
import difflib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Optional

import requests

API_BASE_URL = "https://api.youpass.vn"
HERE = os.path.dirname(os.path.abspath(__file__))
BUG_FILE = os.path.join(HERE, "bug_reported.md")
OUT_JSON = os.path.join(HERE, "report_diagnosis.json")
OUT_MD = os.path.join(HERE, "report_diagnosis.md")

# Token production của bạn (fallback). Override bằng env YOUPASS_API_TOKEN.
_FALLBACK_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhZG1pbl9hY2Nlc3MiOmZhbHNlLCJhcHBfYWNjZXNzIjpmYWxzZSwiZXhwIjoxNzkwMzI1MTYxLCJpYXQiOjE3OTAwNjU5NjEsImlkIjoiMDMxNzkwMWMtNDA1ZS00NWU4LTlmY2YtNzljZWEwMGQ2OGMwIiwiaXNzIjoiZGlyZWN0dXMiLCJyb2xlIjoiNDliMTliM2EtM2NjZi00OWM5LWExMGItYzc2N2ZiMGRmMzgxIn0.CYBHDBlBc34AbWUFb09SmfMyssy-fKz0-bydU1WvpSE"
)

# Ngưỡng — giả định ban đầu, chỉnh lại sau khi nhìn ~10 case thật.
SIM_WRONG = 0.60   # < ngưỡng này: coi như không khớp câu L2 nào
SIM_MATCH = 0.85   # >= ngưỡng này: coi như khớp chắc 1 câu L2
DELTA_SMALL = 0.35 # giây; lệch <= mức này coi như chấp nhận được
DELTA_BIG = 1.5    # giây; lệch > mức này ở 1 đầu = MAPPING_OFF, còn lại = CLIP

REASON_RE = re.compile(
    r"order_question:\s*(?P<order>\d+)\s*-\s*quiz_id\s*:\s*(?P<quiz>\d+)\s*-\s*"
    r"flow_id:\s*(?P<flow>\d+)\s*-\s*question_id:\s*(?P<question>\d+)\s*-\s*"
    r"practice_question:\s*(?P<pq>\d+)\s*-\s*reason:\s*(?P<reason>.*?)\s*$"
)

# Ánh xạ lý do học sinh (tiếng Việt tự do) -> bucket kỳ vọng, để đối chiếu với chẩn đoán tự động.
REASON_KEYWORDS = [
    ("NO_AUDIO", ["ko nghe", "không nghe", "khong nghe", "ko nghe dc", "ko nghe được", "audio không phát",
                  "audio khong hoat dong", "không nghe được", "khong nghe duoc", "ko co audio", "audio bị lỗi"]),
    ("TRANSLATE_MISSING", ["translate", "ko co translate", "không có translate", "ko dịch", "không dịch"]),
    ("CANT_TYPE_DATA", ["gõ chữ", "gõ được", "không gõ", "ko gõ", "nhập chữ"]),
    ("TRANSCRIPT_WRONG", ["transcript bị sai", "transcript sai"]),
    ("MAPPING_OFF", ["audio không đúng với transcript", "audio khong dung", "không đúng với transcript"]),
    ("CLIP", ["thiếu các chữ đầu", "thiếu các chữ sau", "thiếu chữ", "thừa chữ", "thiếu các chữ"]),
]

BUCKET_PRIORITY = [
    "NO_AUDIO", "TRANSCRIPT_WRONG", "MAPPING_OFF", "CLIP",
    "TRANSLATE_MISSING", "CANT_TYPE_DATA", "REVIEW", "OK", "NO_DICTATION", "FETCH_ERROR",
]


def _headers(token: str) -> dict:
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def _jwt_exp(token: str) -> Optional[int]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


def _norm(s: Any) -> str:
    t = str(s or "").lower()
    t = re.sub(r"[^\w\s']", " ", t, flags=re.UNICODE)
    return " ".join(t.split())


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _to_float(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_reports(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            m = REASON_RE.search(line)
            if not m:
                continue
            out.append({
                "lineno": lineno,
                "order_question": int(m.group("order")),
                "quiz_id": int(m.group("quiz")),
                "flow_id": int(m.group("flow")),
                "question_id": int(m.group("question")),
                "practice_question": int(m.group("pq")),
                "reason": m.group("reason").strip(),
            })
    return out


def reason_to_bucket(reason: str) -> Optional[str]:
    r = reason.lower()
    for bucket, kws in REASON_KEYWORDS:
        if any(k in r for k in kws):
            return bucket
    return None


class Client:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update(_headers(token))
        self._quiz_cache: dict[int, Any] = {}

    def quiz_detail(self, quiz_id: int) -> dict:
        if quiz_id not in self._quiz_cache:
            r = self.s.get(f"{API_BASE_URL}/v1/quizzes/{quiz_id}",
                           params={"included_vocabs": "true"}, timeout=60)
            r.raise_for_status()
            self._quiz_cache[quiz_id] = r.json()["data"]
            time.sleep(0.3)
        return self._quiz_cache[quiz_id]

    def practice_flows(self, quiz_id: int, question_id: int) -> list:
        r = self.s.get(f"{API_BASE_URL}/v1/practice-flows",
                       params={"quiz_id": quiz_id, "question_id": question_id}, timeout=45)
        r.raise_for_status()
        body = r.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        time.sleep(0.3)
        return data or []


def find_part_and_question(quiz_detail: dict, question_id: int):
    for part in quiz_detail.get("parts") or []:
        for qs in part.get("question_sets") or []:
            for q in qs.get("questions") or []:
                if q.get("id") == question_id:
                    return part, qs, q
    return None, None, None


def level2_sentences(part: dict) -> list[dict]:
    out = []
    for para in part.get("vocabs") or []:
        for sent in para.get("children") or []:
            if not isinstance(sent, dict):
                continue
            val = (sent.get("value") or "").strip()
            if not val:
                continue
            meta = sent.get("meta") if isinstance(sent.get("meta"), dict) else {}
            out.append({
                "value": val,
                "from": _to_float(meta.get("from")),
                "to": _to_float(meta.get("to")),
            })
    return out


def best_l2_match(text: str, sentences: list[dict]) -> dict:
    best = {"sim": 0.0, "value": None, "from": None, "to": None, "index": None}
    for i, s in enumerate(sentences):
        r = _sim(text, s["value"])
        if r > best["sim"]:
            best = {"sim": round(r, 4), "value": s["value"], "from": s["from"], "to": s["to"], "index": i}
    return best


def locate_in_transcript(text: str, sentences: list[dict]) -> dict:
    """Định vị `text` (correct_answer của 1 câu DICTATION) trong pool câu L2.

    mode:
      EXACT     -> khớp trọn 1 câu L2 (sim >= SIM_MATCH)
      SPAN      -> khớp chuỗi >=2 câu L2 liên tiếp (AI gộp nhiều câu)
      FRAGMENT  -> text là 1 phần của 1 câu L2 (AI cắt nhỏ 1 câu dài; không có word-timing riêng)
      NONE      -> không khớp câu nào (sim < SIM_WRONG)  => nghi chọn nhầm vùng transcript
    Trả true_from/true_to = biên "đúng" suy ra được (với FRAGMENT là biên câu cha).
    """
    nt = _norm(text)
    b = best_l2_match(text, sentences)

    if b["sim"] >= SIM_MATCH:
        return {"mode": "EXACT", "sim": b["sim"], "indices": [b["index"]],
                "true_from": b["from"], "true_to": b["to"], "parent_value": b["value"]}

    # SPAN: cửa sổ trượt 2..6 câu liên tiếp
    best_span = None
    for w in range(2, 7):
        for i in range(0, len(sentences) - w + 1):
            joined = " ".join(s["value"] for s in sentences[i:i + w])
            r = _sim(text, joined)
            if r >= 0.90 and (best_span is None or r > best_span["sim"]):
                fr = sentences[i]["from"]
                to = sentences[i + w - 1]["to"]
                best_span = {"mode": "SPAN", "sim": round(r, 4), "indices": list(range(i, i + w)),
                             "true_from": fr, "true_to": to, "parent_value": joined}
    if best_span:
        return best_span

    # FRAGMENT: text nằm gọn trong 1 câu L2
    for i, s in enumerate(sentences):
        ns = _norm(s["value"])
        if nt and len(nt) >= 8 and nt in ns:
            return {"mode": "FRAGMENT", "sim": round(_sim(text, s["value"]), 4), "indices": [i],
                    "true_from": s["from"], "true_to": s["to"], "parent_value": s["value"]}

    return {"mode": "NONE", "sim": b["sim"], "indices": [],
            "true_from": None, "true_to": None, "parent_value": b["value"]}


def iter_dictation_questions(flows: list) -> list[dict]:
    out = []
    for flow in flows:
        for step in flow.get("practice_steps") or []:
            for q in step.get("questions") or []:
                if (q.get("type") or "").upper() == "DICTATION":
                    out.append({"flow_id": flow.get("flow_id"), "step_id": step.get("id"),
                                "step_type": step.get("type"), **q})
    return out


def diagnose_dictation(dq: dict, sentences: list[dict]) -> dict:
    a_start = _to_float(dq.get("audio_start"))
    a_end = _to_float(dq.get("audio_end"))
    cas = dq.get("correct_answers") or []
    ca0 = (cas[0] if cas else "") or ""
    res = {
        "practice_question_id": dq.get("id"),
        "flow_id": dq.get("flow_id"),
        "step_id": dq.get("step_id"),
        "sort": dq.get("sort"),
        "audio_start": a_start,
        "audio_end": a_end,
        "correct_answer_0": ca0[:200],
        "n_correct_answers": len(cas),
        "buckets": [],
        "match": None,
        "delta_start": None,
        "delta_end": None,
    }

    if not ca0.strip():
        res["buckets"].append("CANT_TYPE_DATA")

    if a_start is None or a_end is None or abs(a_end - a_start) < 1e-6 or a_start >= a_end:
        res["buckets"].append("NO_AUDIO")
        return res

    loc = locate_in_transcript(ca0, sentences)
    res["match"] = loc
    dur = a_end - a_start
    # sanity thời lượng theo độ dài text (giọng đọc IELTS ~11-16 ký tự/giây)
    exp_min = len(ca0) / 22.0
    if dur + 1e-6 < exp_min and len(ca0) >= 25:
        res["buckets"].append("NO_AUDIO")  # quá ngắn để chứa câu -> nghe như mất tiếng/không đủ

    if loc["mode"] == "NONE":
        res["buckets"].append("TRANSCRIPT_WRONG")
        return res

    tf, tt = loc["true_from"], loc["true_to"]
    if tf is None or tt is None:
        res["buckets"].append("REVIEW")
        return res

    ds = a_start - tf   # >0: bắt đầu trễ -> mất chữ đầu ; <0: dư phần đầu
    de = a_end - tt     # <0: kết thúc sớm -> mất chữ cuối ; >0: dư phần cuối
    res["delta_start"] = round(ds, 3)
    res["delta_end"] = round(de, 3)

    if loc["mode"] == "FRAGMENT":
        # audio phải nằm gọn trong [tf, tt] của câu cha; lệch ra ngoài hoặc quá ngắn = hỏng
        out_left = a_start < tf - DELTA_SMALL
        out_right = a_end > tt + DELTA_SMALL
        if out_left or out_right or (dur + 1e-6 < exp_min):
            res["buckets"].append("MAPPING_OFF")
        else:
            res["buckets"].append("REVIEW")  # fragment nhưng biên hợp lý; timestamp là nội suy, cần tai người
        return res

    worst = max(abs(ds), abs(de))
    clip_start = ds > DELTA_SMALL      # mất chữ đầu
    clip_end = de < -DELTA_SMALL       # mất chữ cuối
    if worst > DELTA_BIG:
        res["buckets"].append("MAPPING_OFF")
    elif clip_start or clip_end:
        res["buckets"].append("CLIP")
    else:
        res["buckets"].append("OK")
    return res


def translate_status(flows: list) -> dict:
    total_steps = 0
    steps_with_vocab = 0
    steps_all_meaning_empty = 0
    for flow in flows:
        for step in flow.get("practice_steps") or []:
            has_dict = any((q.get("type") or "").upper() == "DICTATION" for q in step.get("questions") or [])
            if not has_dict:
                continue
            total_steps += 1
            vocs = []
            for q in step.get("questions") or []:
                sv = q.get("suggested_vocabs")
                if isinstance(sv, list):
                    vocs += sv
            if vocs:
                steps_with_vocab += 1
                if all(not str((v or {}).get("meaning") or "").strip() for v in vocs):
                    steps_all_meaning_empty += 1
    missing = total_steps > 0 and (steps_with_vocab == 0 or steps_all_meaning_empty == steps_with_vocab)
    return {
        "dictation_steps": total_steps,
        "steps_with_vocab": steps_with_vocab,
        "steps_all_meaning_empty": steps_all_meaning_empty,
        "translate_missing": missing,
    }


def primary_bucket(buckets: list[str]) -> str:
    for b in BUCKET_PRIORITY:
        if b in buckets:
            return b
    return "OK"


def run(only_qids: Optional[set[int]]):
    token = os.environ.get("YOUPASS_API_TOKEN") or _FALLBACK_TOKEN
    exp = _jwt_exp(token)
    if exp is None:
        print("ERROR: token không phải JWT hợp lệ."); sys.exit(1)
    if exp <= time.time():
        print(f"ERROR: token hết hạn lúc {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))}. "
              f"export YOUPASS_API_TOKEN='ey...' rồi chạy lại."); sys.exit(1)

    reports = parse_reports(BUG_FILE)
    # gom theo (quiz_id, question_id), giữ tất cả lý do
    grouped: dict[tuple[int, int], dict] = {}
    for r in reports:
        key = (r["quiz_id"], r["question_id"])
        g = grouped.setdefault(key, {"quiz_id": r["quiz_id"], "question_id": r["question_id"],
                                     "flow_ids": set(), "orders": set(), "reasons": []})
        g["flow_ids"].add(r["flow_id"])
        g["orders"].add(r["order_question"])
        g["reasons"].append(r["reason"])
    if only_qids:
        grouped = {k: v for k, v in grouped.items() if k[1] in only_qids}

    print(f"{len(reports)} dòng report -> {len(grouped)} (quiz_id, question_id) duy nhất. "
          f"token hết hạn {time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))}")

    client = Client(token)
    results = []
    for i, ((quiz_id, question_id), g) in enumerate(sorted(grouped.items()), 1):
        entry = {
            "quiz_id": quiz_id,
            "question_id": question_id,
            "flow_ids": sorted(g["flow_ids"]),
            "orders": sorted(g["orders"]),
            "student_reasons": g["reasons"],
            "student_buckets": sorted({reason_to_bucket(x) for x in g["reasons"] if reason_to_bucket(x)}),
        }
        try:
            qd = client.quiz_detail(quiz_id)
            part, qs, q = find_part_and_question(qd, question_id)
            if part is None:
                entry["primary_bucket"] = "FETCH_ERROR"
                entry["error"] = "question_id không có trong quiz detail"
                results.append(entry); continue
            entry["part_id"] = part.get("id")
            entry["passage"] = part.get("passage")
            entry["file_id"] = part.get("file_id")
            entry["question_type"] = q.get("question_type")
            entry["question_correct"] = q.get("correct_answers") or q.get("correct_answer")
            sentences = level2_sentences(part)
            entry["n_level2_sentences"] = len(sentences)

            flows = client.practice_flows(quiz_id, question_id)
            entry["n_flows"] = len(flows)
            dqs = iter_dictation_questions(flows)
            entry["n_dictation"] = len(dqs)
            entry["translate"] = translate_status(flows)

            per = [diagnose_dictation(dq, sentences) for dq in dqs]
            entry["dictation"] = per

            all_buckets = []
            for p in per:
                all_buckets += p["buckets"]
            if entry["translate"]["translate_missing"]:
                all_buckets.append("TRANSLATE_MISSING")
            if not dqs:
                all_buckets.append("NO_DICTATION")
            entry["auto_buckets"] = sorted(set(all_buckets))
            entry["primary_bucket"] = primary_bucket(all_buckets)
            # lệch hệ thống toàn part? (mọi CLIP/MAPPING cùng dấu, magnitude gần nhau)
            deltas = [(p["delta_start"], p["delta_end"]) for p in per
                      if p["delta_start"] is not None]
            if len(deltas) >= 3:
                starts = [d[0] for d in deltas]
                if all(x > DELTA_SMALL for x in starts) or all(x < -DELTA_SMALL for x in starts):
                    entry["systematic_shift_start_avg"] = round(sum(starts) / len(starts), 3)
        except requests.HTTPError as e:
            entry["primary_bucket"] = "FETCH_ERROR"
            entry["error"] = f"{e.response.status_code} {e.response.reason} :: {(e.response.text or '')[:300]}"
        except Exception as e:
            entry["primary_bucket"] = "FETCH_ERROR"
            entry["error"] = repr(e)
        results.append(entry)
        print(f"[{i}/{len(grouped)}] quiz={quiz_id} q={question_id} "
              f"-> {entry.get('primary_bucket')} (student: {entry['student_buckets'] or '-'})")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    write_md(results)
    print(f"\nĐã ghi:\n  {OUT_JSON}\n  {OUT_MD}")


def write_md(results: list[dict]):
    by_bucket = defaultdict(list)
    for r in results:
        by_bucket[r.get("primary_bucket", "OK")].append(r)

    agree = miss = 0
    for r in results:
        sb = set(r.get("student_buckets") or [])
        if not sb:
            continue
        if r.get("primary_bucket") in sb:
            agree += 1
        else:
            miss += 1

    lines = []
    lines.append("# Chẩn đoán report Listening Builder (READ-ONLY, production)\n")
    lines.append(f"- Tổng (quiz_id, question_id) duy nhất: **{len(results)}**")
    lines.append(f"- Ngưỡng: SIM_WRONG={SIM_WRONG}, SIM_MATCH={SIM_MATCH}, DELTA_SMALL={DELTA_SMALL}s, DELTA_BIG={DELTA_BIG}s")
    lines.append(f"- Chẩn đoán tự động **khớp** lý do học sinh: {agree}; **lệch**: {miss}\n")

    lines.append("## Đếm theo bucket (primary)\n")
    lines.append("| Bucket | Số câu | question_id |")
    lines.append("|---|---:|---|")
    for b in BUCKET_PRIORITY:
        rs = by_bucket.get(b)
        if not rs:
            continue
        qids = ", ".join(str(r["question_id"]) for r in rs)
        lines.append(f"| {b} | {len(rs)} | {qids} |")
    lines.append("")

    lines.append("## Chi tiết từng câu\n")
    lines.append("| quiz | question | order | type | passage | auto | student | ghi chú |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in sorted(results, key=lambda x: (x.get("primary_bucket", ""), x["quiz_id"], x["question_id"])):
        note = ""
        if r.get("error"):
            note = r["error"][:120]
        elif r.get("systematic_shift_start_avg") is not None:
            note = f"lệch hệ thống start ~{r['systematic_shift_start_avg']}s (cân nhắc sync cả part)"
        elif r.get("dictation"):
            ds = [d for d in r["dictation"] if d.get("delta_start") is not None]
            if ds:
                worst = max(ds, key=lambda d: max(abs(d["delta_start"]), abs(d["delta_end"])))
                note = (f"pq={worst['practice_question_id']} Δstart={worst['delta_start']}s "
                        f"Δend={worst['delta_end']}s sim={worst['match']['sim'] if worst.get('match') else '-'}")
            else:
                bad = next((d for d in r["dictation"] if "TRANSCRIPT_WRONG" in d["buckets"] or "NO_AUDIO" in d["buckets"]), None)
                if bad:
                    note = (f"pq={bad['practice_question_id']} audio=[{bad['audio_start']},{bad['audio_end']}] "
                            f"sim={bad['match']['sim'] if bad.get('match') else '-'} ca={bad['correct_answer_0'][:60]!r}")
        lines.append("| {q} | {qid} | {order} | {qt} | {ps} | {ab} | {sb} | {note} |".format(
            q=r["quiz_id"], qid=r["question_id"],
            order=",".join(map(str, r.get("orders") or [])),
            qt=r.get("question_type", "-"), ps=r.get("passage", "-"),
            ab=r.get("primary_bucket", "-"),
            sb=",".join(r.get("student_buckets") or []) or "-",
            note=note.replace("|", "/")))
    lines.append("")

    lines.append("## Ghi chú giới hạn\n")
    lines.append("- `GET /v1/practice-flows` (app-api DTO `PracticeQuestions`) **không trả `word_tokens`** — "
                 "bug #6 (không gõ được) cần kiểm ở FE `apps/fe` xem ô nhập dictation render từ field nào.")
    lines.append("- Script không kiểm được file audio có phát được không (chỉ có `file_id`). "
                 "Bucket `NO_AUDIO` ở đây = dữ liệu `audio_start/audio_end` hỏng; "
                 "case audio 0<start<end mà học sinh vẫn 'không nghe' -> nghi FE/player hoặc file.")
    lines.append("- `TRANSLATE_MISSING` dựa trên `suggested_vocabs[].meaning`. Nếu 'translate' ý học sinh là "
                 "bản dịch transcript câu L2 thì phải kiểm ở quiz detail (`vocab` L2 không có field dịch).")
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    qids = {int(x) for x in sys.argv[1:]} if len(sys.argv) > 1 else None
    run(qids)
