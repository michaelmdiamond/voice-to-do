#!/usr/bin/env python3
"""
bee_daily_reducer.py — Context-aware Bee candidate reducer for Todoist.

Reads the day's staged task candidates, the day's Bee conversations, and existing
open Bee Todoist tasks. Claude reduces them into a small, evidence-grounded
Todoist create list. This is the only default path that creates new Bee tasks.

Requires: bee proxy at http://127.0.0.1:8787, TODOIST_API_TOKEN in .env
Schedule: ~/Library/LaunchAgents/com.michael.bee-daily-reducer.plist
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BEE_BASE = "http://127.0.0.1:8787"
TODOIST_BASE = "https://api.todoist.com/api/v1"
BEE_LABEL = "bee"
ROOT = Path(__file__).parent
CANDIDATE_DIR = ROOT / ".bee_candidates"
DECISION_DIR = ROOT / ".bee_decisions"
DEFAULT_MAX_CREATE_PER_RUN = 5
DEFAULT_MAX_CREATE_PER_DAY = 10
DEFAULT_MAX_OPEN_BEE_TASKS = 150
CLAUDE_PATHS = [
    "/Users/michael/.local/bin/claude",
    "/Users/michael/.npm-global/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]


def load_env(key: str) -> str | None:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                return val or None
    return os.environ.get(key) or None


def env_int(key: str, default: int) -> int:
    raw = load_env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  [config] ignoring invalid {key}={raw!r}; using {default}", file=sys.stderr)
        return default


def bee_get(path: str) -> dict:
    req = Request(f"{BEE_BASE}{path}", headers={"Accept": "application/json"})
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(bee_get("/v1/todos").get("timezone", "America/New_York"))
    except Exception:
        return ZoneInfo("America/New_York")


def day_bounds_ms(date: datetime) -> tuple[int, int]:
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def fetch_conversations_for_day(tz: ZoneInfo, target: datetime) -> list[dict]:
    start_ms, end_ms = day_bounds_ms(target)
    results: list[dict] = []
    cursor: str | None = None
    while True:
        url = "/v1/conversations" + (f"?cursor={cursor}" if cursor else "")
        data = bee_get(url)
        for conv in data.get("conversations", []):
            ts = conv["start_time"]
            if ts < start_ms:
                return results
            if ts <= end_ms:
                results.append(conv)
        cursor = data.get("next_cursor")
        if not cursor:
            return results


def fetch_utterances(conv_id: int) -> str:
    data = bee_get(f"/v1/conversations/{conv_id}")
    conv = data.get("conversation", {})
    lines = []
    for transcription in conv.get("transcriptions", []):
        for u in transcription.get("utterances", []):
            speaker = u.get("speaker", "Unknown")
            text = u.get("text", "").strip()
            if text:
                lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def todoist_request(
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
    params: dict | None = None,
) -> dict | None:
    url = f"{TODOIST_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(body).encode() if body else None
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(f"  [todoist] {method} {path} -> HTTP {e.code}: {body_text[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [todoist] {method} {path} failed: {e}", file=sys.stderr)
        return None


def fetch_open_bee_tasks(token: str) -> list[dict]:
    tasks = []
    cursor = None
    while True:
        params = {"label": BEE_LABEL}
        if cursor:
            params["cursor"] = cursor
        data = todoist_request("GET", "/tasks", token, params=params)
        if data is None:
            return tasks
        tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            return tasks


def create_task(content: str, token: str, evidence: str) -> dict:
    return todoist_request("POST", "/tasks", token, body={
        "content": content,
        "labels": [BEE_LABEL],
        "description": f"Captured via Bee daily reducer\n\nEvidence: {evidence[:800]}",
    }) or {}


def notify(title: str, message: str) -> None:
    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def item_key(s: str) -> str:
    s = re.sub(r"\s+[—-].*$", "", s)
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\s+", " ", s.lower().strip())
    return s.rstrip(".,;")


LOW_SIGNAL_PATTERNS = re.compile(
    r"^(consider|maybe|possibly|think about|note:|someone may)\b",
    re.IGNORECASE,
)
TRANSIENT_PATTERNS = re.compile(
    r"\b(today|tonight|later today|this afternoon|this evening|tomorrow|"
    r"when .+ gets back|when .+ returns|once .+ back|before .+ arrives)\b",
    re.IGNORECASE,
)


def candidate_id(candidate: dict) -> str:
    raw = json.dumps({
        "task": candidate.get("task"),
        "quote": candidate.get("evidence_quote"),
        "conversation_id": candidate.get("conversation_id"),
    }, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def load_candidates(date_str: str) -> list[dict]:
    path = CANDIDATE_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    seen = set()
    candidates = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        candidate = json.loads(line)
        candidate["candidate_id"] = candidate_id(candidate)
        key = (item_key(candidate.get("task", "")), candidate.get("evidence_quote", ""))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def conversation_context(convs: list[dict], tz: ZoneInfo) -> list[dict]:
    out = []
    for conv in sorted(convs, key=lambda c: c["start_time"]):
        dt = datetime.fromtimestamp(conv["start_time"] / 1000, tz=tz)
        transcript = ""
        try:
            transcript = fetch_utterances(conv["id"])
        except Exception as e:
            transcript = f"(utterances unavailable: {e})"
        out.append({
            "conversation_id": conv["id"],
            "time": dt.isoformat(),
            "short_summary": (conv.get("short_summary") or "").strip(),
            "summary": (conv.get("summary") or "").strip()[:2500],
            "transcript_excerpt": transcript[:3000],
        })
    return out


SYSTEM_PROMPT = """You are a conservative daily task reducer for Michael's Bee wearable.

You receive:
- candidate tasks extracted earlier from individual conversations
- all Bee conversation context from the same day
- Michael's currently open Bee Todoist tasks

Your job is to decide which candidates are worth creating in Todoist.

Rules:
- Create only durable, actionable commitments.
- The task must be supported by a direct evidence quote and same-day context.
- Do not create generic baby-care advice, transient reminders, notes, jokes, or brainstorming.
- Do not merge nearby but unrelated facts. Remove unsupported names, rates, dates, and relationships.
- If a task is already represented by an open task, reject it as duplicate.
- Prefer one concise task over several overlapping variants.

Respond ONLY with valid JSON:
{
  "approved": [
    {
      "task": "final Todoist task text",
      "evidence_quote": "quote that supports this exact task",
      "candidate_ids": ["abc123"],
      "confidence": "high",
      "reason": "short reason"
    }
  ],
  "review": [
    {
      "task": "candidate worth human review but not safe to auto-create",
      "evidence_quote": "supporting quote if available",
      "candidate_ids": ["abc123"],
      "confidence": "medium|low",
      "reason": "why this needs human review"
    }
  ],
  "rejected": [
    {
      "candidate_id": "abc123",
      "reason": "duplicate|transient|unsupported merge|low commitment|note/guidance|already open|other"
    }
  ],
  "summary": "one sentence"
}

If nothing should be created, return {"approved": [], "rejected": [], "summary": "no durable tasks approved"}."""


def call_claude(payload: dict) -> dict:
    claude_bin = shutil.which("claude") or next((p for p in CLAUDE_PATHS if Path(p).exists()), None)
    if not claude_bin:
        raise RuntimeError("claude executable not found")
    env = {
        **os.environ,
        "HOME": os.environ.get("HOME", "/Users/michael"),
        "PATH": ":".join([
            "/Users/michael/.local/bin",
            "/Users/michael/.npm-global/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]),
    }
    result = subprocess.run(
        [claude_bin, "-p", SYSTEM_PROMPT],
        input=json.dumps(payload, indent=2, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    output = result.stdout.strip()
    if "hit your session limit" in output.lower():
        raise RuntimeError("claude session limit hit; retry later")
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        raise RuntimeError(f"claude returned no JSON: {output[:200]}")
    return json.loads(match.group())


def should_apply_approved(item: dict, open_keys: set[str]) -> tuple[bool, str]:
    task = str(item.get("task") or "").strip()
    quote = str(item.get("evidence_quote") or "").strip()
    if not task:
        return False, "empty task"
    if str(item.get("confidence") or "").lower() != "high":
        return False, "not high confidence"
    if not quote:
        return False, "missing evidence quote"
    if item_key(task) in open_keys:
        return False, "already open"
    if LOW_SIGNAL_PATTERNS.search(task):
        return False, "low commitment"
    if TRANSIENT_PATTERNS.search(task):
        return False, "transient"
    return True, "accepted"


def save_decision(date_str: str, decision: dict) -> Path:
    DECISION_DIR.mkdir(parents=True, exist_ok=True)
    path = DECISION_DIR / f"{date_str}.json"
    path.write_text(json.dumps(decision, indent=2, ensure_ascii=False))
    return path


def created_today_count(date_str: str) -> int:
    path = DECISION_DIR / f"{date_str}.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return 0
    return len(data.get("_meta", {}).get("created", []))


def previous_meta(date_str: str) -> dict:
    path = DECISION_DIR / f"{date_str}.json"
    if not path.exists():
        return {"created": [], "skipped": [], "review": []}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"created": [], "skipped": [], "review": []}
    meta = data.get("_meta", {})
    return {
        "created": list(meta.get("created", [])),
        "skipped": list(meta.get("skipped", [])),
        "review": list(meta.get("review", [])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reduce Bee candidates into Todoist tasks")
    parser.add_argument("--date", help="Date to reduce, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Write decision JSON but do not create Todoist tasks")
    args = parser.parse_args()

    todoist_token = load_env("TODOIST_API_TOKEN")
    if not todoist_token:
        print("Error: TODOIST_API_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)

    tz = get_tz()
    date_str = args.date or datetime.now(tz).strftime("%Y-%m-%d")
    target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
    print(f"[bee-daily-reducer] {date_str}")

    try:
        candidates = load_candidates(date_str)
        if not candidates:
            print("  No staged candidates.")
            return
        convs = fetch_conversations_for_day(tz, target)
    except URLError as e:
        print(f"Error: Bee proxy unreachable — {e}", file=sys.stderr)
        sys.exit(1)

    open_tasks = fetch_open_bee_tasks(todoist_token)
    open_keys = {item_key(t["content"]) for t in open_tasks}
    max_create_per_run = env_int("BEE_REDUCER_MAX_CREATE_PER_RUN", DEFAULT_MAX_CREATE_PER_RUN)
    max_create_per_day = env_int("BEE_REDUCER_MAX_CREATE_PER_DAY", DEFAULT_MAX_CREATE_PER_DAY)
    max_open_bee_tasks = env_int("BEE_REDUCER_MAX_OPEN_TASKS", DEFAULT_MAX_OPEN_BEE_TASKS)
    prior_meta = previous_meta(date_str)
    prior_created_count = len(prior_meta["created"])

    payload = {
        "date": date_str,
        "candidate_tasks": candidates,
        "open_bee_tasks": [
            {"id": t["id"], "content": t["content"]}
            for t in open_tasks
        ],
        "conversation_context": conversation_context(convs, tz),
    }

    decision = call_claude(payload)
    decision["_meta"] = {
        "date": date_str,
        "candidate_count": len(candidates),
        "conversation_count": len(convs),
        "open_bee_task_count": len(open_tasks),
        "dry_run": args.dry_run,
        "limits": {
            "max_create_per_run": max_create_per_run,
            "max_create_per_day": max_create_per_day,
            "max_open_bee_tasks": max_open_bee_tasks,
            "prior_created_today": prior_created_count,
        },
        "created": prior_meta["created"],
        "skipped": prior_meta["skipped"],
        "review": prior_meta["review"],
    }

    approved = decision.get("approved", [])
    review_items = decision.get("review", [])
    decision["_meta"]["review"].extend(review_items)
    print(f"  {len(candidates)} candidates -> {len(approved)} approved · {len(review_items)} review")

    creation_blocked_reason = ""
    if len(open_tasks) >= max_open_bee_tasks:
        creation_blocked_reason = f"open bee task count {len(open_tasks)} >= limit {max_open_bee_tasks}"
    elif prior_created_count >= max_create_per_day:
        creation_blocked_reason = f"daily create limit reached ({prior_created_count}/{max_create_per_day})"
    if creation_blocked_reason:
        print(f"  creation blocked: {creation_blocked_reason}")
        notify("Bee Reducer", f"Task creation blocked: {creation_blocked_reason}")
        for item in approved:
            decision["_meta"]["skipped"].append({
                "task": str(item.get("task") or "").strip(),
                "reason": creation_blocked_reason,
            })
        path = save_decision(date_str, decision)
        print(f"  -> {path}")
        return

    created_this_run = 0
    for item in approved:
        ok, reason = should_apply_approved(item, open_keys)
        task = str(item.get("task") or "").strip()
        if not ok:
            print(f"  skipping ({reason}): {task}")
            decision["_meta"]["skipped"].append({"task": task, "reason": reason})
            continue
        if args.dry_run:
            print(f"  would create: {task}")
            decision["_meta"]["skipped"].append({"task": task, "reason": "dry-run"})
            continue
        if created_this_run >= max_create_per_run:
            print(f"  skipping (per-run limit reached): {task}")
            decision["_meta"]["skipped"].append({"task": task, "reason": "per-run limit reached"})
            continue
        if prior_created_count + created_this_run >= max_create_per_day:
            print(f"  skipping (daily limit reached): {task}")
            decision["_meta"]["skipped"].append({"task": task, "reason": "daily limit reached"})
            continue
        created = create_task(task, todoist_token, str(item.get("evidence_quote") or ""))
        if created.get("id"):
            print(f"  created: {task}")
            decision["_meta"]["created"].append({"id": created["id"], "task": task})
            open_keys.add(item_key(task))
            created_this_run += 1
        else:
            print(f"  create failed: {task}")
            decision["_meta"]["skipped"].append({"task": task, "reason": "create failed"})

    path = save_decision(date_str, decision)
    print(f"  -> {path}")


if __name__ == "__main__":
    main()
