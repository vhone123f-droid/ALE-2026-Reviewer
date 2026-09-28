#!/usr/bin/env python3
"""
ALE 2026 automatic question-bank generator.

Designed for the existing ALE Reviewer V2 JSON format.
It:
1) loads the current bank announced by updates/latest.json,
2) downloads official PRC references (with a bundled fallback),
3) asks an OpenAI model for balanced new original practice questions,
4) asks a second pass to verify factual/assessment quality,
5) rejects malformed and near-duplicate items,
6) appends only new questions while preserving all old IDs/order,
7) writes a new semantic-versioned bank and updates updates/latest.json.

This never claims to discover leaked or actual future PRC questions.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUTO = ROOT / "automation"
UPDATES = ROOT / "updates"
CONFIG_PATH = AUTO / "config.json"
SOURCE_STATE_PATH = AUTO / "source-state.json"
FALLBACK_PATH = AUTO / "tos_fallback.txt"

DEFAULT_MODEL = "gpt-5.6-terra"

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def semver_tuple(v: str) -> tuple[int, int, int]:
    parts = [int(x) if str(x).isdigit() else 0 for x in str(v).split(".")[:3]]
    return tuple((parts + [0, 0, 0])[:3])

def bump_minor(v: str) -> str:
    major, minor, _patch = semver_tuple(v)
    return f"{major}.{minor + 1}.0"

def normalize_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s.lower()).strip()
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return s

def near_duplicate(stem: str, existing_stems: list[str], threshold: float = 0.84) -> bool:
    a = normalize_text(stem)
    if not a:
        return True
    for b_raw in existing_stems:
        b = normalize_text(b_raw)
        if a == b:
            return True
        if SequenceMatcher(None, a, b).ratio() >= threshold:
            return True
    return False

def fetch_source(source: dict, fallback_text: str) -> tuple[str, str, bool]:
    """Return (text, sha256, fetched_live)."""
    import requests
    from bs4 import BeautifulSoup
    from pypdf import PdfReader
    url = source["url"]
    try:
        r = requests.get(
            url,
            timeout=45,
            headers={"User-Agent": "ALE-2026-Reviewer-AutoUpdater/1.0 (+GitHub Actions)"}
        )
        r.raise_for_status()
        content_type = (r.headers.get("content-type") or "").lower()
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            reader = PdfReader(io.BytesIO(r.content))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        else:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = "\n".join(x.strip() for x in soup.get_text("\n").splitlines() if x.strip())
        text = text.strip()
        if len(text) < 500:
            raise RuntimeError("Downloaded source contained too little readable text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text, digest, True
    except Exception as e:
        print(f"WARNING: could not fetch {source['name']}: {e}", file=sys.stderr)
        text = fallback_text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text, digest, False

def build_source_context(config: dict) -> tuple[str, dict, bool]:
    fallback = FALLBACK_PATH.read_text(encoding="utf-8")
    previous = {}
    if SOURCE_STATE_PATH.exists():
        try:
            previous = load_json(SOURCE_STATE_PATH)
        except Exception:
            previous = {}

    blocks = []
    state = {"checkedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z", "sources": {}}
    changed = False
    char_budget = 90000

    for src in config["officialSources"]:
        text, digest, live = fetch_source(src, fallback)
        old_hash = (previous.get("sources", {}).get(src["url"], {}) or {}).get("sha256")
        if old_hash and old_hash != digest:
            changed = True
        state["sources"][src["url"]] = {
            "name": src["name"],
            "sha256": digest,
            "fetchedLive": live
        }

        remain = max(0, char_budget - sum(len(b) for b in blocks))
        if remain <= 0:
            break
        excerpt = text[:min(len(text), remain)]
        blocks.append(f"\n### {src['name']}\nURL: {src['url']}\n{excerpt}")

    return "\n".join(blocks), state, changed

def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end+1])
        raise

def call_json(client: Any, model: str, prompt: str, label: str) -> dict:
    last_error = None
    for attempt in range(2):
        p = prompt if attempt == 0 else (
            prompt
            + "\n\nIMPORTANT RETRY: Your previous response could not be parsed. "
              "Return ONLY one valid JSON object, with double-quoted keys/strings and no markdown."
        )
        try:
            response = client.responses.create(
                model=model,
                input=p,
                max_output_tokens=24000,
            )
            return extract_json(response.output_text)
        except Exception as e:
            last_error = e
            print(f"{label} attempt {attempt+1} failed: {e}", file=sys.stderr)
    raise RuntimeError(f"{label} failed after retry: {last_error}")

def valid_question(q: dict, subjects: list[str]) -> tuple[bool, str]:
    required = ["subject", "topic", "question", "options", "answer", "explanation", "optionExplanations"]
    for k in required:
        if k not in q:
            return False, f"missing {k}"
    if q["subject"] not in subjects:
        return False, "unknown subject"
    if not isinstance(q["question"], str) or len(q["question"].strip()) < 20:
        return False, "question too short"
    if not isinstance(q["options"], list) or len(q["options"]) != 4:
        return False, "must have four options"
    if len(set(str(x).strip().casefold() for x in q["options"])) != 4:
        return False, "duplicate options"
    if not isinstance(q["answer"], int) or not (0 <= q["answer"] <= 3):
        return False, "answer must be 0..3"
    if not isinstance(q["optionExplanations"], list) or len(q["optionExplanations"]) != 4:
        return False, "must have four option explanations"
    if len(q["explanation"].strip()) < 20:
        return False, "main explanation too short"
    lower_opts = " ".join(q["options"]).lower()
    if "all of the above" in lower_opts or "none of the above" in lower_opts:
        return False, "disallowed all/none option"
    return True, ""

def load_current_bank() -> tuple[dict, Path, dict]:
    latest_path = UPDATES / "latest.json"
    if not latest_path.exists():
        raise FileNotFoundError("updates/latest.json is missing")
    latest = load_json(latest_path)
    bank_url = latest.get("bankUrl", "")
    bank_name = Path(bank_url).name
    if not bank_name:
        raise RuntimeError("updates/latest.json has no usable bankUrl")
    bank_path = ROOT / bank_name
    if not bank_path.exists():
        raise FileNotFoundError(f"Current bank file not found: {bank_path}")
    bank = load_json(bank_path)
    return bank, bank_path, latest

def generate_questions(
    client: Any,
    model: str,
    config: dict,
    source_context: str,
    existing_stems: list[str],
    per_subject: int
) -> list[dict]:
    subjects = config["subjects"]
    candidate_each = max(per_subject + 1, per_subject * int(config.get("candidateMultiplier", 2)))
    existing_digest = "\n".join(f"- {x[:240]}" for x in existing_stems[-350:])

    prompt = f"""
You are creating ORIGINAL practice questions for the Philippine 2026 Agriculturists
Licensure Examination. These are NOT leaked or recalled PRC items and you must never
imply that they are.

Use the official PRC TOS context below as the authority for scope. Produce exactly
{candidate_each} candidate questions for EACH of these six subjects:
{json.dumps(subjects, ensure_ascii=False)}

QUALITY RULES:
{chr(10).join("- " + x for x in config["qualityRules"])}

Question mix:
- Favor Applying and Analyzing.
- Include useful board-style computations where naturally appropriate.
- Make distractors plausible and diagnostically useful.
- Avoid simple paraphrases of the existing stems listed below.
- Use Philippine context when appropriate, but do not invent regulations, rates,
  dates, official programs, pesticide recommendations, or legal claims.
- Do not ask about the probability of a specific item appearing in the real exam.

Return ONLY this JSON shape:
{{
  "questions": [
    {{
      "subject": "exact subject name",
      "topic": "short topic",
      "question": "stem",
      "options": ["A text","B text","C text","D text"],
      "answer": 0,
      "explanation": "why the keyed answer is correct",
      "optionExplanations": [
        "why option A is correct/wrong",
        "why option B is correct/wrong",
        "why option C is correct/wrong",
        "why option D is correct/wrong"
      ],
      "difficulty": "Core|Application|Analysis|Calculation",
      "priority": "High|Medium",
      "sourceTag": "short description of the TOS competency or official source basis"
    }}
  ]
}}

OFFICIAL SOURCE CONTEXT:
{source_context}

RECENT/EXISTING QUESTION STEMS TO AVOID DUPLICATING:
{existing_digest}
"""
    obj = call_json(client, model, prompt, "generation")
    qs = obj.get("questions", [])
    if not isinstance(qs, list):
        raise RuntimeError("Generator JSON does not contain a questions array")
    return qs

def verify_candidates(
    client: Any,
    model: str,
    config: dict,
    source_context: str,
    candidates: list[dict]
) -> set[int]:
    compact_context = source_context[:65000]
    payload = json.dumps(candidates, ensure_ascii=False)
    prompt = f"""
Act as a strict independent quality reviewer for a Philippine Agriculturists
Licensure Examination PRACTICE question bank.

Review the candidate questions below against the official PRC TOS/source context.
Reject any item that is factually questionable, ambiguous, outside scope, has more
than one defensible best answer, has a bad calculation, has a weak/misleading
explanation, relies on an unsupported current law/policy/rate, or resembles a
leaked/actual exam claim.

Also reject items with poor distractors or wording that would teach a misconception.
Do not rewrite the items. Only decide which candidate indices are safe to publish.

Return ONLY:
{{
  "accepted": [0, 2, 5],
  "rejected": [
    {{"index": 1, "reason": "brief reason"}}
  ]
}}

OFFICIAL CONTEXT:
{compact_context}

CANDIDATES:
{payload}
"""
    obj = call_json(client, model, prompt, "verification")
    accepted = obj.get("accepted", [])
    if not isinstance(accepted, list):
        return set()
    return {int(x) for x in accepted if isinstance(x, int) or str(x).isdigit()}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-subject", type=int, default=None)
    ap.add_argument("--force-after-exam", action="store_true")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()

    config = load_json(CONFIG_PATH)
    bank, bank_path, latest = load_current_bank()
    subjects = config["subjects"]

    if args.validate_only:
        bad = []
        for i, q in enumerate(bank.get("questions", [])):
            ok, reason = valid_question(q, subjects)
            if not ok:
                bad.append((i, reason))
        if bad:
            print("INVALID BANK:", bad[:20])
            return 2
        print(f"Bank v{bank.get('bankVersion')} validated: {len(bank['questions'])} questions.")
        return 0

    today = date.today().isoformat()
    stop_after = config.get("stopAutomaticUpdatesAfter")
    if stop_after and today > stop_after and not args.force_after_exam:
        print(f"No automatic update: stop date {stop_after} has passed.")
        return 0

    max_questions = int(config.get("maxQuestions", 600))
    current_count = len(bank.get("questions", []))
    if current_count >= max_questions:
        print(f"No automatic update: bank already has {current_count}/{max_questions} questions.")
        return 0

    per_subject = args.per_subject or int(os.getenv("QUESTIONS_PER_SUBJECT") or config.get("questionsPerSubject", 2))
    per_subject = max(1, min(per_subject, 10))

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Add it under GitHub repository "
            "Settings > Secrets and variables > Actions > Secrets."
        )
    model = os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    source_context, source_state, sources_changed = build_source_context(config)
    existing_questions = bank.get("questions", [])
    existing_stems = [q.get("question", "") for q in existing_questions]
    existing_ids = {q.get("id") for q in existing_questions if q.get("id")}

    print(f"Generating candidates with {model}; current bank has {current_count} questions.")
    candidates = generate_questions(client, model, config, source_context, existing_stems, per_subject)

    locally_valid = []
    original_indices = []
    for i, q in enumerate(candidates):
        ok, reason = valid_question(q, subjects)
        if not ok:
            print(f"Reject local candidate {i}: {reason}")
            continue
        if near_duplicate(q["question"], existing_stems + [x["question"] for x in locally_valid]):
            print(f"Reject local candidate {i}: near duplicate")
            continue
        locally_valid.append(q)
        original_indices.append(i)

    if len(locally_valid) < len(subjects):
        raise RuntimeError(f"Too few locally valid candidates ({len(locally_valid)}) to make a balanced update.")

    accepted_local_indices = verify_candidates(client, model, config, source_context, locally_valid)
    reviewed = [q for i, q in enumerate(locally_valid) if i in accepted_local_indices]

    by_subject: dict[str, list[dict]] = defaultdict(list)
    for q in reviewed:
        by_subject[q["subject"]].append(q)

    # Keep the update balanced. If the verifier leaves fewer than requested for
    # any subject, publish the same smaller count for all subjects.
    balanced_each = min([len(by_subject[s]) for s in subjects] + [per_subject])
    if balanced_each < 1:
        counts = {s: len(by_subject[s]) for s in subjects}
        raise RuntimeError(f"Verifier left no publishable item for at least one subject: {counts}")

    remaining_capacity = max_questions - current_count
    balanced_each = min(balanced_each, remaining_capacity // len(subjects))
    if balanced_each < 1:
        print("No update: fewer than six slots remain before maxQuestions.")
        return 0

    selected = []
    for s in subjects:
        selected.extend(by_subject[s][:balanced_each])

    next_version = bump_minor(str(bank.get("bankVersion", latest.get("bankVersion", "2.0.0"))))
    stamp = next_version.replace(".", "-")
    seq = 1
    for q in selected:
        while True:
            qid = f"auto-v{stamp}-{seq:03d}"
            seq += 1
            if qid not in existing_ids:
                break
        q["id"] = qid
        q.setdefault("difficulty", "Application")
        q.setdefault("priority", "High")
        q.setdefault("sourceTag", "PRC TOS-aligned automated practice")
        q["reviewStatus"] = "AI generated + independent AI verification + local validation"

    new_questions = existing_questions + selected
    new_bank = dict(bank)
    new_bank["schemaVersion"] = 1
    new_bank["bankVersion"] = next_version
    new_bank["released"] = today
    new_bank["questionCount"] = len(new_questions)
    new_bank["subjects"] = subjects
    new_bank["notes"] = (
        f"Automated update: +{len(selected)} original TOS-aligned practice questions. "
        "Existing question IDs/order preserved. Questions are practice material, not leaked PRC items."
    )
    new_bank["questions"] = new_questions

    new_filename = f"question-bank-v{next_version}.json"
    new_bank_path = ROOT / new_filename
    write_json(new_bank_path, new_bank)

    release_notes = [
        f"Added {len(selected)} new original practice questions ({balanced_each} per subject)",
        "Preserved all earlier question IDs so phone progress remains compatible",
        "New items passed structural, duplicate, and second-pass AI quality checks",
    ]
    if sources_changed:
        release_notes.append("At least one monitored official reference changed since the previous run")

    new_latest = {
        "schemaVersion": 1,
        "appMinVersion": latest.get("appMinVersion", "2.0.0"),
        "bankVersion": next_version,
        "bankUrl": f"../{new_filename}",
        "released": today,
        "questionCount": len(new_questions),
        "releaseNotes": release_notes,
        "verifiedAgainst": [s["name"] for s in config["officialSources"]]
    }
    write_json(UPDATES / "latest.json", new_latest)

    history_path = UPDATES / "history.json"
    history = []
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            history = loaded if isinstance(loaded, list) else []
        except Exception:
            history = []
    history.append({
        "bankVersion": next_version,
        "released": today,
        "questionCount": len(new_questions),
        "added": len(selected),
        "perSubject": balanced_each,
        "model": model,
        "officialSourceChanged": sources_changed
    })
    write_json(history_path, history[-50:])
    write_json(SOURCE_STATE_PATH, source_state)

    print(f"Created {new_filename}: {current_count} -> {len(new_questions)} questions")
    print(f"Updated updates/latest.json to v{next_version}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
