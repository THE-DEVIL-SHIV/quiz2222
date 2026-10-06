#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==========================================================================
 TELEGRAM QUIZ JSON CONVERTER BOT  -  SINGLE FILE
==========================================================================
PDF / text (MCQ)  ->  Telegram Quiz Bot import-ready JSON (no numbering).

RUN
    python quiz_bot.py

Pehli baar run par zaroori libraries (python-telegram-bot, PyMuPDF,
requests) khud install ho jati hain. Chahein to manually:
    pip install "python-telegram-bot>=21.6" PyMuPDF requests

SETTINGS  (env variable ya .env file ya neeche CONFIG me paste karein)
    BOT_TOKEN           Telegram bot token (neeche default bhi diya hai)
    GEMINI_API_KEY      Google AI Studio key (free)  -> https://aistudio.google.com/apikey
    ANTHROPIC_API_KEY   Claude API key (optional, Gemini ki jagah)
    LLM_PROVIDER        gemini | anthropic   (dono key ho tab choose)
    GEMINI_MODEL        default gemini-2.5-flash
    ANTHROPIC_MODEL     default claude-sonnet-5-5
    ALLOWED_USERS       comma separated Telegram user IDs (khali = sab)
    QUESTION_PREFIX     har question ke aage lagne wala text (optional)
    OPTION_PREFIX       har option ke aage lagne wala text (optional)
    CHUNK_CHARS         default 4500   (ek LLM call me kitna text)
    CONCURRENCY         default 2      (parallel LLM calls)
    PORT                host ko port chahiye ho (Render etc.) to health
                        server khud start ho jata hai

NOTE: Ek token par sirf EK instance chalayein, warna "Conflict" error aata hai.
==========================================================================
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

# ============================== CONFIG ===================================
BOT_TOKEN_DEFAULT = "8671618360:AAFz_fP17JRt1JrkucLzfmJD9TJRACh7EpM"
GEMINI_API_KEY_DEFAULT = "AIzaSyA70nmH7HDbB_FWQuhucErsBqzV4umPQuQ"
ANTHROPIC_API_KEY_DEFAULT = ""   # <- ya yahan Claude key
# =========================================================================


def _load_env_file() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k.startswith("export "):
                        k = k[7:].strip()
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    os.environ.setdefault(k, v)
        except Exception:
            pass


_load_env_file()


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


def env_raw(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v is not None else default


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


BOT_TOKEN = env("BOT_TOKEN") or env("TELEGRAM_BOT_TOKEN") or BOT_TOKEN_DEFAULT
CHUNK_CHARS = env_int("CHUNK_CHARS", 4500)
CONCURRENCY = max(1, env_int("CONCURRENCY", 2))
MAX_OCR_PAGES = env_int("MAX_OCR_PAGES", 120)
OCR_BATCH = max(1, env_int("OCR_BATCH", 3))
OCR_DPI = env_int("OCR_DPI", 120)
MAX_FILE_BYTES = 40 * 1024 * 1024
QUESTION_PREFIX = env_raw("QUESTION_PREFIX", "")
OPTION_PREFIX = env_raw("OPTION_PREFIX", "")
ALLOWED_USERS = {int(x) for x in re.findall(r"\d+", env("ALLOWED_USERS"))}
TMP_DIR = os.path.join(tempfile.gettempdir(), "quizbot_files")
os.makedirs(TMP_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("quizbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULTS = {"options": 2, "explanation": True, "solve": False, "ocr": False}

MSG_UNREADABLE = "PDF se text read nahi ho pa raha. Please clear PDF ya text send karein."
MSG_UNCLEAR = (
    "कुछ questions ka correct answer confidently identify nahi ho paya. "
    "Unhe अलग से review ke liye mark kiya gaya hai."
)


# ========================= TEXT CLEANING HELPERS =========================
WS_RE = re.compile(r"\s+")
_QW = r"(?:Question|Ques|Q|प्रश्न|प्र)"
NUM_PREFIX_RE = re.compile(
    r"^\s*(?:"
    + _QW + r"\s*\.?\s*[\(\[]?\d{1,4}[\)\]]?\s*[\.\):\-–—]*"
    + r"|" + _QW + r"\s*[\.\):]"
    + r"|[\(\[]\d{1,4}[\)\]]"
    + r"|\d{1,3}\s*[\.\)](?!\d)"
    + r")\s*",
    re.I,
)
_LAB = r"(?:[A-Da-d]|[1-4]|[१-४]|[कखगघ])"
OPT_LABEL_RE = re.compile(
    r"^\s*(?:[\(\[]" + _LAB + r"[\)\]]|" + _LAB + r"[\)\]]|" + _LAB + r"\.(?=\s))\s*"
)
REF_RE = re.compile(
    r"(all of the above|none of the above|both\s*[\(\[]?[a-d]\b|\b[a-d]\s*(and|&)\s*[a-d]\b"
    r"|सभी|इनमें से कोई|उपरोक्त|उपर्युक्त)",
    re.I,
)
DEDUPE_STRIP_RE = re.compile(r"[\s\.,;:!?\"'“”‘’()\[\]{}\-–—_।]+")


def squash(s: Any) -> str:
    return WS_RE.sub(" ", str(s if s is not None else "")).strip()


def clean_question(q: Any) -> str:
    q = squash(q)
    for _ in range(2):
        new = NUM_PREFIX_RE.sub("", q, count=1).strip()
        if new == q:
            break
        q = new
    return q


def clean_option(o: Any) -> str:
    o = squash(o)
    new = OPT_LABEL_RE.sub("", o, count=1).strip()
    return new if new else o


def dedupe_key(q: str) -> str:
    return DEDUPE_STRIP_RE.sub("", q.casefold())


def is_ref(opt: str) -> bool:
    return len(opt) <= 40 and bool(REF_RE.search(opt))


def clamp(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    return cut + "…"


def to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


# ============================ BUILD / VALIDATE ===========================
def pick_pos(rng: random.Random, recent: list) -> int:
    if len(recent) >= 3 and len(set(recent[-3:])) == 1:
        return 1 - recent[-1]
    return rng.randint(0, 1)


def build_item(q: str, raw: dict, cfg: dict, rng: random.Random, recent: list):
    """Returns (item | None, flag | None)."""
    opts_raw = raw.get("options")
    if not isinstance(opts_raw, list):
        opts_raw = []
    opts: list = []
    mapping: dict = {}
    for i, o in enumerate(opts_raw):
        t = clean_option(o)
        if not t:
            mapping[i] = None
        elif t in opts:
            mapping[i] = opts.index(t)
        else:
            mapping[i] = len(opts)
            opts.append(t)

    ans = to_int(raw.get("answer"))
    ans = mapping.get(ans) if ans is not None else None
    mode = cfg["options"]

    def flag(reason: str):
        return None, {"question": q, "options": opts, "reason": reason}

    if len(opts) < 2:
        return flag("options missing / incomplete in source")
    if ans is None:
        return flag("correct answer not found in source")
    if mode == 4 and len(opts) > 10:
        return flag("more than 10 options")

    explanation = clamp(squash(raw.get("explanation")), 200) if cfg["explanation"] else ""

    if mode == 2 and len(opts) > 2:
        correct_text = opts[ans]
        if is_ref(correct_text):
            return flag("correct option is 'all/none/both of the above' type - 2 option conversion ambiguous")
        wrongs = [o for i, o in enumerate(opts) if i != ans]
        good = [o for o in wrongs if not is_ref(o)] or wrongs
        wrong = rng.choice(good)
        pos = pick_pos(rng, recent)
        final = [correct_text, wrong] if pos == 0 else [wrong, correct_text]
        correct = pos
    else:
        final, correct = opts[:], ans

    recent.append(correct)
    return {
        "question": q,
        "options": final,
        "correct": correct,
        "explanation": explanation,
    }, None


def validate_item(it: dict, mode: int) -> Optional[str]:
    if not isinstance(it.get("question"), str) or not it["question"].strip():
        return "empty question"
    if NUM_PREFIX_RE.match(it["question"]):
        return "question numbering present"
    opts = it.get("options")
    if not isinstance(opts, list) or not all(isinstance(o, str) and o.strip() for o in opts):
        return "invalid options"
    if mode == 2 and len(opts) != 2:
        return "not exactly 2 options"
    if mode == 4 and not 2 <= len(opts) <= 10:
        return "option count invalid"
    if any(OPT_LABEL_RE.match(o) for o in opts):
        return "option label present"
    c = it.get("correct")
    if isinstance(c, bool) or not isinstance(c, int) or not 0 <= c < len(opts):
        return "correct index invalid"
    if len(set(opts)) != len(opts):
        return "duplicate options"
    return None


def finalize(raw_items: list, cfg: dict, rng: Optional[random.Random] = None):
    """raw LLM dicts -> (items, flagged, stats)."""
    rng = rng or random.Random()
    items: list = []
    flagged: list = []
    seen: set = set()
    recent: list = []
    stats = {"extracted": 0, "duplicates": 0, "solved": 0, "long": 0}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        q = clean_question(raw.get("question"))
        if not q:
            continue
        stats["extracted"] += 1
        key = dedupe_key(q)
        if key in seen:
            stats["duplicates"] += 1
            continue
        seen.add(key)
        item, fl = build_item(q, raw, cfg, rng, recent)
        if item is not None:
            problem = validate_item(item, cfg["options"])
            if problem:
                recent.pop()
                item, fl = None, {"question": q, "options": item["options"], "reason": problem}
        if fl is not None:
            flagged.append(fl)
            continue
        if raw.get("solved") is True:
            stats["solved"] += 1
        items.append(item)

    for it in items:
        it["question"] = QUESTION_PREFIX + it["question"]
        it["options"] = [OPTION_PREFIX + o for o in it["options"]]
        if len(it["question"]) > 300 or any(len(o) > 100 for o in it["options"]):
            stats["long"] += 1

    # final whole-file sanity: valid JSON round-trip
    json.loads(json.dumps(items, ensure_ascii=False))
    return items, flagged, stats


def split_json_files(items: list, base: str = "quiz") -> list:
    """Returns [(filename, bytes)]; splits only if file is too big."""
    data = json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")
    if len(data) <= MAX_FILE_BYTES:
        return [(f"{base}.json", data)]
    parts = math.ceil(len(data) / MAX_FILE_BYTES)
    size = math.ceil(len(items) / parts)
    out = []
    for n in range(parts):
        part = items[n * size : (n + 1) * size]
        if part:
            out.append((f"{base}_part{n + 1}.json",
                        json.dumps(part, ensure_ascii=False, indent=2).encode("utf-8")))
    return out


# ============================ PDF / TEXT INPUT ===========================
PAGE_NO_RE = re.compile(r"^(?:page|पृष्ठ|pg\.?)?\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)


def analyze_pdf(path: str) -> dict:
    """Extract text from all pages (headers/footers removed)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:  # newer package name
        import pymupdf as fitz  # type: ignore
    doc = fitz.open(path)
    if getattr(doc, "needs_pass", False):
        raise ValueError("encrypted")
    pages_lines = []
    for page in doc:
        txt = page.get_text("text") or ""
        pages_lines.append([ln.rstrip() for ln in txt.splitlines()])
    n = len(pages_lines)
    doc.close()

    # detect repeated header / footer lines (first/last 3 lines of pages)
    counter: dict = {}
    for lines in pages_lines:
        non_empty = [l for l in lines if l.strip()]
        edge = non_empty[:3] + non_empty[-3:]
        for k in {re.sub(r"\d+", "#", l.strip().lower()) for l in edge if len(l.strip()) < 90}:
            counter[k] = counter.get(k, 0) + 1
    threshold = max(3, int(n * 0.4))
    repeated = {k for k, v in counter.items() if v >= threshold} if n >= 4 else set()

    cleaned_pages = []
    for lines in pages_lines:
        idx = [i for i, l in enumerate(lines) if l.strip()]
        edge_idx = set(idx[:3] + idx[-3:])
        keep = []
        for i, l in enumerate(lines):
            s = l.strip()
            if i in edge_idx and s:
                if re.sub(r"\d+", "#", s.lower()) in repeated:
                    continue
                if PAGE_NO_RE.match(s) and (i in set(idx[:2] + idx[-2:])):
                    continue
            keep.append(l)
        cleaned_pages.append("\n".join(keep))

    text = "\n\n".join(cleaned_pages).strip()
    chars = len(WS_RE.sub("", text))
    scanned = n > 0 and (chars / n) < 40
    return {"pages": n, "text": text, "scanned": scanned}


def render_pages(path: str, pages: list) -> list:
    try:
        import fitz
    except ImportError:
        import pymupdf as fitz  # type: ignore
    out = []
    doc = fitz.open(path)
    try:
        for p in pages:
            pix = doc[p].get_pixmap(dpi=OCR_DPI)
            out.append(pix.tobytes("png"))
    finally:
        doc.close()
    return out


Q_START_RE = re.compile(r"^\s*(?:(?:Q|प्रश्न|प्र)[\.\s]*)?\(?\d{1,3}[\.\)]\s*\S")


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list:
    lines = text.splitlines()
    chunks, cur, size = [], [], 0
    for ln in lines:
        if size >= limit:
            if Q_START_RE.match(ln):
                chunks.append("\n".join(cur))
                cur, size = [], 0
            elif size >= limit * 1.6:  # hard cut with overlap (dupes get removed)
                chunks.append("\n".join(cur))
                cur = cur[-8:]
                size = sum(len(x) + 1 for x in cur)
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


KEY_HEAD_RE = re.compile(
    r"^\W*(?:answer\s*key|answers?|answer\s*sheet|ans\.?\s*key|key|उत्तर\s*कुंजी|उत्तरकुंजी|उत्तरमाला|सही\s*उत्तर|उत्तर)\W*$",
    re.I,
)
KEY_PAIR_RE = re.compile(r"(?<!\d)(\d{1,4})\s*[\-–—:.\)]*\s*\(?([A-Da-dकखगघ1-4])\)?(?=\s|,|;|$)")


def find_key(text: str):
    """Split (questions_part, answer_key_part) if an Answer Key section exists."""
    lines = text.splitlines()
    best = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i >= 3 and 0 < len(s) <= 30 and KEY_HEAD_RE.match(s):
            rest = "\n".join(lines[i + 1 :])
            if len(KEY_PAIR_RE.findall(rest[:30000])) >= 5 and len(rest) <= 0.6 * len(text):
                best = i
    if best is None:
        return text, ""
    return "\n".join(lines[:best]), "\n".join(lines[best:])[:20000]


def build_tasks(sources: list, cfg: dict):
    tasks: list = []
    warnings: list = []
    buf: list = []

    def flush():
        if not buf:
            return
        full = "\n\n".join(buf)
        buf.clear()
        body, key = find_key(full)
        for ch in split_chunks(body):
            tasks.append({"type": "text", "text": ch, "key": key})

    for s in sources:
        if s["kind"] == "text":
            buf.append(s["text"])
        elif s["scanned"] or cfg["ocr"]:
            flush()
            n = min(s["pages"], MAX_OCR_PAGES)
            if s["pages"] > MAX_OCR_PAGES:
                warnings.append(
                    f"{s['name']}: sirf pehle {MAX_OCR_PAGES} pages OCR hue (MAX_OCR_PAGES)."
                )
            for start in range(0, n, OCR_BATCH):
                tasks.append({
                    "type": "pages", "path": s["path"],
                    "pages": list(range(start, min(n, start + OCR_BATCH))),
                })
        else:
            buf.append(s["text"])
    flush()
    return tasks, warnings


# ================================ LLM ====================================
class FatalLLMError(Exception):
    pass


def get_provider():
    gem = env("GEMINI_API_KEY") or env("GOOGLE_API_KEY") or GEMINI_API_KEY_DEFAULT
    ant = env("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY_DEFAULT
    pref = env("LLM_PROVIDER").lower()
    if pref == "anthropic" and ant:
        return "anthropic", ant
    if pref == "gemini" and gem:
        return "gemini", gem
    if gem:
        return "gemini", gem
    if ant:
        return "anthropic", ant
    return None, None


def build_system_prompt(cfg: dict) -> str:
    if cfg["explanation"]:
        expl = ('explanation: ONE short sentence (max 150 characters) in the SAME language as the question, '
                "giving the reason for the correct answer. Condense the source solution if one exists.")
    else:
        expl = 'explanation: always an empty string "".'
    if cfg["solve"]:
        ans_extra = ('If the source gives NO answer for a question, you MAY decide it from your own reliable '
                     'subject knowledge only when you are completely certain, and then add "solved": true. '
                     "Otherwise use null.")
    else:
        ans_extra = "If the source gives no answer for a question, use null. NEVER guess."
    return f"""You are a precise MCQ extraction engine for a Telegram quiz bot.
The input is raw text (or page images) from an exam PDF or a pasted message, in Hindi and/or English.

Return ONLY a JSON array - no markdown fences, no commentary. One object per multiple-choice question, in the original order:
{{"question": "...", "options": ["...", "..."], "answer": 0, "explanation": "..."}}

RULES
1. question = the question text only. REMOVE all numbering/prefixes (Q1., 1., 2), Question 3, प्रश्न 4 ...). Keep the question's own language: Hindi stays Hindi, English stays English. Never translate.
2. options = ALL options exactly as in the source, same order, with labels removed (A) (B) (a) (1) (क) etc. Do not invent, reword, add or drop options. If the question has no options, use [].
3. Preserve formulas, symbols and Unicode exactly (H₂O, 6.022 × 10²³, x², √, °C ...). Join lines broken by PDF layout into normal sentences.
4. answer = 0-based index (into options) of the correct option, taken ONLY from the source: the ANSWER KEY section when provided (match by the question number in the source text; key letter b or number 2 means index 1), or ticked/bold/marked options, or 'Ans:' lines. {ans_extra}
5. {expl}
6. Ignore headers, footers, page numbers, watermarks, instructions, titles, advertisements and the answer-key list itself.
7. Skip a question only if its text is clearly cut off mid-way at the very start/end of the given text.
8. Never output the same question twice.
9. The output must be valid JSON (escape double quotes inside strings).
If page images are given, read (OCR) all text from the images first and then apply the same rules."""


def build_user_prompt(task: dict) -> str:
    if task["type"] == "pages":
        return "The attached page images contain the source. Extract all MCQs as specified."
    parts = []
    if task.get("key"):
        parts.append("=== ANSWER KEY (from the same document; use it to match question numbers) ===\n"
                     + task["key"])
    parts.append("=== SOURCE TEXT ===\n" + task["text"])
    return "\n\n".join(parts)


def _backoff(attempt: int, resp=None) -> float:
    if resp is not None:
        ra = resp.headers.get("retry-after")
        if ra and ra.replace(".", "", 1).isdigit():
            return min(float(ra) + 1, 90)
    return min(3 * (2 ** attempt), 60)


def call_llm(system: str, user: str, images: Optional[list] = None) -> str:
    import requests

    provider, key = get_provider()
    if not provider:
        raise FatalLLMError("NO_KEY")
    images = images or []
    last_err = ""
    for attempt in range(6):
        resp = None
        try:
            if provider == "gemini":
                model = env("GEMINI_MODEL", "gemini-2.5-flash")
                parts: list = [{"text": user}]
                for img in images:
                    parts.append({"inline_data": {"mime_type": "image/png",
                                                  "data": base64.b64encode(img).decode()}})
                resp = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": parts}],
                        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 32768,
                                             "responseMimeType": "application/json"},
                    },
                    timeout=(15, 240),
                )
            else:
                model = env("ANTHROPIC_MODEL", "claude-sonnet-5-5")
                content: list = []
                for img in images:
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.b64encode(img).decode()}})
                content.append({"type": "text", "text": user})
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": model, "max_tokens": 12000, "system": system,
                          "messages": [{"role": "user", "content": content}]},
                    timeout=(15, 300),
                )
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(_backoff(attempt))
            continue

        code = resp.status_code
        if code == 200:
            data = resp.json()
            if provider == "gemini":
                cands = data.get("candidates") or []
                if not cands:
                    raise RuntimeError(f"empty/blocked response: {data.get('promptFeedback')}")
                ps = (cands[0].get("content") or {}).get("parts") or []
                return "".join(p.get("text", "") for p in ps
                               if isinstance(p, dict) and not p.get("thought"))
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
        body = resp.text[:300]
        if code in (401, 403, 404) or (code == 400 and re.search(r"api key|API_KEY", body, re.I)):
            raise FatalLLMError(f"{provider} API error {code}: {body}")
        last_err = f"HTTP {code}: {body}"
        if code == 429 or code >= 500:
            time.sleep(_backoff(attempt, resp))
            continue
        raise RuntimeError(last_err)
    raise RuntimeError("LLM failed after retries: " + last_err)


def parse_json_array(text: str) -> list:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t).strip()
    data = None
    try:
        data = json.loads(t)
    except Exception:
        s = t.find("[")
        if s == -1:
            raise ValueError("no JSON array")
        e = t.rfind("]")
        for candidate in ([t[s : e + 1]] if e > s else []) + [t[s : t.rfind("}") + 1] + "]"]:
            try:
                data = json.loads(candidate)
                break
            except Exception:
                continue
        if data is None:
            raise ValueError("invalid JSON")
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else [data]
    if not isinstance(data, list):
        raise ValueError("not a list")
    return [d for d in data if isinstance(d, dict)]


def extract_task(task: dict, cfg: dict) -> list:
    """Runs in a worker thread."""
    system = build_system_prompt(cfg)
    user = build_user_prompt(task)
    images = render_pages(task["path"], task["pages"]) if task["type"] == "pages" else None
    err = None
    for _ in range(3):
        out = call_llm(system, user, images)
        try:
            return parse_json_array(out)
        except ValueError as e:
            err = e
            user += "\n\nREMINDER: output ONLY one valid JSON array, nothing else."
    raise ValueError(f"bad JSON from LLM: {err}")


# ============================= TELEGRAM BOT ==============================
def get_cfg(ud: dict) -> dict:
    return ud.setdefault("cfg", dict(DEFAULTS))


def settings_text(cfg: dict) -> str:
    return (
        f"⚙️ Mode: {cfg['options']} options | "
        f"Explanation: {'ON' if cfg['explanation'] else 'OFF'} | "
        f"Solve (answer key na ho to AI): {'ON' if cfg['solve'] else 'OFF'} | "
        f"OCR (force): {'ON' if cfg['ocr'] else 'AUTO'}"
    )


def clear_sources(ud: dict) -> None:
    for s in ud.get("sources", []):
        if s.get("kind") == "pdf":
            try:
                os.remove(s["path"])
            except OSError:
                pass
    ud["sources"] = []
    ud["done"] = False


def prepare_input(ud: dict) -> None:
    if ud.get("done"):
        clear_sources(ud)
    ud.setdefault("sources", [])


async def safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:
        pass


async def guard(update) -> bool:
    if update.effective_user is None or update.message is None:
        return False
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Aap is bot ko use karne ke liye authorized nahi hain.")
        return False
    return True


async def cmd_start(update, context):
    if not await guard(update):
        return
    cfg = get_cfg(context.user_data)
    await update.message.reply_text(
        "👋 Welcome! Main Telegram Quiz JSON Converter Bot hoon.\n\n"
        "📄 Mujhe PDF ya MCQ text bhejiye (kitne bhi questions), phir /convert dabaiye.\n"
        "Main import-ready JSON file bana dunga (bina numbering ke).\n\n"
        "Commands:\n"
        "/2options - 2 option mode (default)\n"
        "/4options - 4 option mode\n"
        "/explanation_on - explanation ke saath\n"
        "/explanation_off - explanation khali\n"
        "/solve_on - answer key na ho to AI se answer nikale\n"
        "/solve_off - sirf source ke answer (default)\n"
        "/ocr_on - PDF ko hamesha OCR se padho (purane Hindi font wale PDF ke liye)\n"
        "/ocr_off - auto (default)\n"
        "/convert - JSON banao\n"
        "/cancel - sab clear / cancel\n\n"
        + settings_text(cfg)
    )


def _setter(key: str, value: Any, label: str):
    async def handler(update, context):
        if not await guard(update):
            return
        cfg = get_cfg(context.user_data)
        cfg[key] = value
        await update.message.reply_text(f"✅ {label}\n{settings_text(cfg)}")
    return handler


cmd_2options = _setter("options", 2, "2-option mode ON")
cmd_4options = _setter("options", 4, "4-option mode ON")
cmd_expl_on = _setter("explanation", True, "Explanation ON")
cmd_expl_off = _setter("explanation", False, "Explanation OFF")
cmd_solve_on = _setter("solve", True, "Solve mode ON (jahan source me answer nahi hoga wahan AI answer dega - review zaroor karein)")
cmd_solve_off = _setter("solve", False, "Solve mode OFF")
cmd_ocr_on = _setter("ocr", True, "OCR forced ON")
cmd_ocr_off = _setter("ocr", False, "OCR AUTO")


async def cmd_cancel(update, context):
    if not await guard(update):
        return
    ud = context.user_data
    t = ud.get("task")
    if t is not None and not t.done():
        t.cancel()
    ud["run"] = None
    ud["busy"] = False
    clear_sources(ud)
    await update.message.reply_text("❌ Cancel ho gaya. Naya PDF ya text bhejiye.")


async def _ack_text(message, ud: dict):
    try:
        await asyncio.sleep(1.5)
        total = sum(len(s["text"]) for s in ud.get("sources", []) if s["kind"] == "text")
        n = len(ud.get("sources", []))
        await message.reply_text(
            f"✅ Add ho gaya ({n} item, {total} characters).\n"
            "Aur bhejna ho to bhejiye, warna /convert dabaiye."
        )
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("ack failed")


async def on_text(update, context):
    if not await guard(update):
        return
    ud = context.user_data
    text = update.message.text or ""
    if not text.strip():
        return
    if ud.get("busy"):
        await update.message.reply_text("⏳ Conversion chal raha hai. /cancel ya thoda wait karein.")
        return
    prepare_input(ud)
    ud["sources"].append({"kind": "text", "text": text})
    prev = ud.get("ack")
    if prev is not None and not prev.done():
        prev.cancel()
    ud["ack"] = asyncio.create_task(_ack_text(update.message, ud))


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


async def on_document(update, context):
    if not await guard(update):
        return
    ud = context.user_data
    msg = update.message
    doc = msg.document
    name = doc.file_name or "file"
    mime = (doc.mime_type or "").lower()
    is_pdf = mime == "application/pdf" or name.lower().endswith(".pdf")
    is_txt = mime.startswith("text/") or name.lower().endswith((".txt", ".md"))
    if not (is_pdf or is_txt):
        await msg.reply_text("Sirf PDF ya .txt file bhejiye.")
        return
    if ud.get("busy"):
        await msg.reply_text("⏳ Conversion chal raha hai. /cancel ya thoda wait karein.")
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await msg.reply_text("❌ File 20MB se badi hai (Telegram bot limit). PDF ko split karke bhejiye.")
        return
    prepare_input(ud)
    status = await msg.reply_text("📥 File le raha hoon...")
    try:
        f = await doc.get_file()
        if is_txt:
            data = await f.download_as_bytearray()
            text = _decode(bytes(data))
            if not text.strip():
                await safe_edit(status, MSG_UNREADABLE)
                return
            ud["sources"].append({"kind": "text", "text": text})
            await safe_edit(status, f"✅ Text file mili ({len(text)} characters).\nAur bhejiye ya /convert dabaiye.")
            return
        path = os.path.join(TMP_DIR, f"{update.effective_user.id}_{int(time.time() * 1000)}.pdf")
        await f.download_to_drive(path)
        try:
            info = await asyncio.to_thread(analyze_pdf, path)
        except Exception:
            log.exception("pdf open failed")
            try:
                os.remove(path)
            except OSError:
                pass
            await safe_edit(status, MSG_UNREADABLE)
            return
        ud["sources"].append({"kind": "pdf", "name": name, "path": path, **info})
        note = " (scanned/image PDF - OCR use hoga)" if info["scanned"] else ""
        await safe_edit(
            status,
            f"✅ PDF mila: {info['pages']} pages{note}.\nAur PDF/text bhejiye ya /convert dabaiye.",
        )
    except Exception:
        log.exception("document handling failed")
        await safe_edit(status, MSG_UNREADABLE)


async def cmd_convert(update, context):
    if not await guard(update):
        return
    ud = context.user_data
    if ud.get("busy"):
        await update.message.reply_text("⏳ Pehle se conversion chal raha hai. /cancel se rok sakte hain.")
        return
    if not ud.get("sources"):
        await update.message.reply_text("Pehle PDF ya MCQ text bhejiye, phir /convert dabaiye.")
        return
    prev = ud.get("ack")
    if prev is not None and not prev.done():
        prev.cancel()
    run_id = object()
    ud["run"] = run_id
    ud["busy"] = True
    ud["task"] = asyncio.create_task(run_conversion(update.message, ud, run_id))


async def run_conversion(message, ud: dict, run_id: object):
    status = None
    try:
        cfg = dict(get_cfg(ud))
        sources = list(ud["sources"])
        status = await message.reply_text("⏳ Content analyze ho raha hai...")

        provider, _ = get_provider()
        if not provider:
            await safe_edit(
                status,
                "❌ LLM API key set nahi hai.\n"
                "GEMINI_API_KEY (free: aistudio.google.com/apikey) ya ANTHROPIC_API_KEY "
                "env/.env me daaliye (ya quiz_bot.py ke upar CONFIG me).",
            )
            return

        tasks, warnings = build_tasks(sources, cfg)
        if not tasks:
            await safe_edit(status, MSG_UNREADABLE)
            return

        total = len(tasks)
        results: list = [None] * total
        failed: list = []
        fatal: list = []
        done = 0
        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(i: int, t: dict):
            nonlocal done
            async with sem:
                if fatal:
                    return
                try:
                    results[i] = await asyncio.to_thread(extract_task, t, cfg)
                except FatalLLMError as e:
                    fatal.append(str(e))
                except Exception:
                    log.exception("chunk %s failed", i)
                    failed.append(i)
                done += 1
                await safe_edit(status, f"⏳ Processing... {done}/{total}")

        await asyncio.gather(*(worker(i, t) for i, t in enumerate(tasks)))

        if fatal:
            await safe_edit(status, f"❌ API error: {fatal[0][:350]}\nKey/model check karein.")
            return

        for i in list(failed):  # one sequential retry
            try:
                results[i] = await asyncio.to_thread(extract_task, tasks[i], cfg)
                failed.remove(i)
            except Exception:
                pass

        raw_all = [r for res in results if res for r in res]
        items, flagged, stats = finalize(raw_all, cfg)

        if not items and not flagged:
            await safe_edit(status, MSG_UNREADABLE)
            return

        await safe_edit(status, "✅ Processing complete, file bhej raha hoon...")
        for fname, data in split_json_files(items):
            await message.reply_document(document=io.BytesIO(data), filename=fname)
        if flagged:
            review = json.dumps(flagged, ensure_ascii=False, indent=2).encode("utf-8")
            await message.reply_document(document=io.BytesIO(review), filename="review_needed.json")

        lines = [
            "✅ Conversion complete!",
            f"📝 Final questions: {len(items)}",
            f"🔀 {settings_text(cfg)}",
        ]
        if stats["duplicates"]:
            lines.append(f"♻️ Duplicates removed: {stats['duplicates']}")
        if flagged:
            lines.append(f"⚠️ Review ke liye: {len(flagged)} (review_needed.json)")
            lines.append(MSG_UNCLEAR)
            if not cfg["solve"]:
                lines.append("Tip: source me answer key na ho to /solve_on karke dobara /convert karein.")
        if stats["solved"]:
            lines.append(f"🤖 {stats['solved']} answers AI ne solve kiye (source me nahi the) - ek baar check kar lein.")
        if stats["long"]:
            lines.append(f"ℹ️ {stats['long']} questions/options Telegram poll limit (300/100 chars) se lambe hain.")
        if failed:
            lines.append(f"⚠️ {len(failed)} part process nahi ho paye (API error). Dobara /convert try karein.")
        lines.extend(f"ℹ️ {w}" for w in warnings)
        lines.append("\nNaya PDF/text bhejkar dobara convert kar sakte hain.")
        await message.reply_text("\n".join(lines))
        ud["done"] = True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("conversion failed")
        try:
            await message.reply_text(f"❌ Conversion me error aaya: {str(e)[:300]}\nDobara /convert try karein.")
        except Exception:
            pass
    finally:
        if ud.get("run") is run_id:
            ud["busy"] = False


async def on_error(update, context):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


# ============================ KEEP-ALIVE / MAIN ==========================
def start_health_server() -> None:
    port = env("PORT")
    if not port.isdigit():
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Quiz bot running")

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    try:
        srv = HTTPServer(("0.0.0.0", int(port)), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("Health server on port %s", port)
    except Exception:
        log.exception("health server failed")


def bootstrap() -> None:
    """Install missing libraries automatically (host friendly)."""
    import importlib

    need = [("telegram", "python-telegram-bot>=21.6,<23"), ("fitz", "PyMuPDF"), ("requests", "requests")]
    for mod, pip_name in need:
        try:
            importlib.import_module(mod)
            continue
        except ImportError:
            pass
        ok = False
        for extra in ([], ["--break-system-packages"]):
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pip_name] + extra)
                importlib.invalidate_caches()
                importlib.import_module(mod)
                ok = True
                break
            except Exception:
                continue
        if not ok:
            raise SystemExit(f"'{pip_name}' install nahi ho paya. Manually: pip install \"{pip_name}\"")


def build_app():
    from telegram import BotCommand, Update  # noqa: F401
    from telegram.ext import Application, CommandHandler, MessageHandler, filters

    async def post_init(app):
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.bot.set_my_commands([
                BotCommand("start", "Welcome / help"),
                BotCommand("2options", "2 option mode"),
                BotCommand("4options", "4 option mode"),
                BotCommand("explanation_on", "Explanation ON"),
                BotCommand("explanation_off", "Explanation OFF"),
                BotCommand("solve_on", "Answer key na ho to AI solve"),
                BotCommand("solve_off", "Solve OFF"),
                BotCommand("ocr_on", "PDF OCR force"),
                BotCommand("ocr_off", "OCR auto"),
                BotCommand("convert", "JSON file banao"),
                BotCommand("cancel", "Cancel / clear"),
            ])
        except Exception:
            log.exception("post_init failed")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .read_timeout(60).write_timeout(120).connect_timeout(30).pool_timeout(30)
        .build()
    )
    for name, fn in [
        ("start", cmd_start), ("help", cmd_start),
        ("2options", cmd_2options), ("4options", cmd_4options),
        ("explanation_on", cmd_expl_on), ("explanation_off", cmd_expl_off),
        ("solve_on", cmd_solve_on), ("solve_off", cmd_solve_off),
        ("ocr_on", cmd_ocr_on), ("ocr_off", cmd_ocr_off),
        ("convert", cmd_convert), ("cancel", cmd_cancel),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    bootstrap()
    from telegram import Update

    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing.")
    for f in os.listdir(TMP_DIR):  # clean leftovers
        try:
            os.remove(os.path.join(TMP_DIR, f))
        except OSError:
            pass
    start_health_server()
    if not get_provider()[0]:
        log.warning("GEMINI_API_KEY / ANTHROPIC_API_KEY set nahi hai - /convert tab tak kaam nahi karega.")
    while True:
        try:
            log.info("Bot starting...")
            build_app().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            break  # clean stop (Ctrl+C / SIGTERM)
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception:
            log.exception("Bot crashed, restarting in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    main()
