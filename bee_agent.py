#!/usr/bin/env python3
"""
bee_agent.py — Ambient accountability agent for Bee wearable + Todoist.

Runs every 30 minutes. Fetches new Bee conversations since last run, uses
Claude to determine which open Todoist tasks were completed and what new tasks
were mentioned, updates Todoist accordingly, and sends a macOS notification.

Requires: bee proxy at http://127.0.0.1:8787, TODOIST_API_TOKEN in .env
Uses: claude CLI (no API key needed — uses your Claude subscription)
Schedule: ~/Library/LaunchAgents/com.michael.bee-agent.plist
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from zoneinfo import ZoneInfo

BEE_BASE     = "http://127.0.0.1:8787"
TODOIST_BASE = "https://api.todoist.com/api/v1"
BEE_LABEL    = "bee"
STATE_FILE   = Path(__file__).parent / ".bee_agent_state.json"
CANDIDATE_DIR = Path(__file__).parent / ".bee_candidates"
TRANSCRIPT_DIR = Path(__file__).parent / ".bee_transcripts"
CLAUDE_PATHS = [
    "/Users/michael/.local/bin/claude",
    "/Users/michael/.npm-global/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]


# ---------------------------------------------------------------------------
# Config & state
# ---------------------------------------------------------------------------

def load_env(key: str) -> str | None:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                return val or None
    return os.environ.get(key) or None


def env_flag(key: str) -> bool:
    return (load_env(key) or "").lower() in {"1", "true", "yes", "on"}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    # First run: look back 24 hours max so we don't process all history
    since_ms = int((datetime.now().timestamp() - 86400) * 1000)
    return {"last_processed_ms": since_ms, "processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def append_candidate(date_str: str, candidate: dict) -> None:
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATE_DIR / f"{date_str}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(candidate, ensure_ascii=False) + "\n")


def save_transcript(date_str: str, record: dict) -> None:
    """Archive a conversation's raw transcript + Bee summary + what the pipeline
    concluded from it, so transcripts can be compared against extracted tasks."""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{date_str}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Bee API
# ---------------------------------------------------------------------------

def bee_get(path: str) -> dict:
    req = Request(f"{BEE_BASE}{path}", headers={"Accept": "application/json"})
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(bee_get("/v1/todos").get("timezone", "America/New_York"))
    except Exception:
        return ZoneInfo("America/New_York")


def fetch_new_conversations(since_ms: int) -> list[dict]:
    """Fetch conversations newer than since_ms, newest-first."""
    results = []
    cursor = None
    while True:
        url = "/v1/conversations" + (f"?cursor={cursor}" if cursor else "")
        data = bee_get(url)
        for conv in data.get("conversations", []):
            ts = conv["start_time"]
            if ts <= since_ms:
                return results  # gone past our window, stop
            results.append(conv)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return results


def fetch_utterances(conv_id: int) -> str:
    """Fetch full transcript text for a conversation."""
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


# ---------------------------------------------------------------------------
# Todoist API
# ---------------------------------------------------------------------------

def todoist_request(method: str, path: str, token: str,
                    body: dict | None = None, params: dict | None = None) -> dict | None:
    url = f"{TODOIST_BASE}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body).encode() if body else None
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        print(f"  [todoist] {method} {path} → HTTP {e.code}", file=sys.stderr)
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
            break
        tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return tasks


def complete_task(task_id: str, token: str) -> bool:
    result = todoist_request("POST", f"/tasks/{task_id}/close", token)
    return result is not None  # 204 returns {}


def create_task(content: str, token: str) -> dict:
    return todoist_request("POST", "/tasks", token, body={
        "content": content,
        "labels": [BEE_LABEL],
        "description": "Captured via Bee wearable agent",
    }) or {}


def item_key(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().strip()).rstrip(".,;")


def coerce_text_item(item: str | dict, key: str = "task") -> str:
    if isinstance(item, dict):
        return str(item.get(key) or item.get("content") or "").strip()
    return str(item or "").strip()


def evidence_quote(item: str | dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("evidence_quote") or item.get("evidence") or "").strip()


def confidence(item: str | dict) -> str:
    if not isinstance(item, dict):
        # Old schema fallback is allowed for completions but not for new task creation.
        return "legacy"
    return str(item.get("confidence") or "").strip().lower()


TRANSIENT_PATTERNS = re.compile(
    r"\b("
    r"today|tonight|later today|this afternoon|this evening|tomorrow|"
    r"when .+ gets back|when .+ returns|once .+ back|"
    r"before .+ arrives|in ~?\d+ minutes|next feeding cycle"
    r")\b",
    re.IGNORECASE,
)

LOW_SIGNAL_PATTERNS = re.compile(
    r"^(consider|maybe|possibly|think about|note:|someone may)\b",
    re.IGNORECASE,
)

BABY_GUIDANCE_PATTERNS = re.compile(
    r"\b("
    r"diaper|rash|burp|swaddle|wake windows|hunger cues|tummy time|"
    r"feeding|spit-up|spit up|bottle|nipple|pacifier"
    r")\b",
    re.IGNORECASE,
)

COMMITMENT_PATTERNS = re.compile(
    r"\b("
    r"i need to|i should|i have to|i'm going to|i am going to|i gotta|"
    r"remind me to|let me|can you remind|we need to|we should|"
    r"i'll|i will|i want to|i'm gonna|i am gonna"
    r")\b",
    re.IGNORECASE,
)


def should_create_task(item: str | dict) -> tuple[bool, str]:
    """Code-side guardrail for LLM task suggestions."""
    task = coerce_text_item(item)
    quote = evidence_quote(item)
    conf = confidence(item)
    reject_if = str(item.get("reject_if") or "").strip() if isinstance(item, dict) else ""

    if not task:
        return False, "empty task"
    if not isinstance(item, dict):
        return False, "legacy task without evidence"
    if conf != "high":
        return False, f"confidence={conf or 'missing'}"
    if not quote:
        return False, "missing evidence quote"
    if reject_if:
        return False, f"model flagged unsafe: {reject_if}"
    if LOW_SIGNAL_PATTERNS.search(task):
        return False, "low-commitment phrasing"
    if TRANSIENT_PATTERNS.search(task):
        return False, "transient/time-bound reminder"
    if BABY_GUIDANCE_PATTERNS.search(task) and not COMMITMENT_PATTERNS.search(quote):
        return False, "baby-care guidance without Michael commitment"
    return True, "accepted"


# ---------------------------------------------------------------------------
# LLM analysis via claude CLI
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an accountability assistant analyzing transcripts from a wearable AI device worn by Michael.

The transcript is the source of truth. The Bee summary is context only. Do not create or complete tasks
from the summary unless the transcript also contains direct evidence.

Your main failure mode to avoid: merging adjacent but unrelated facts. If one person recommends a
service, a different person is discussed elsewhere, and a price appears nearby, do NOT combine those
facts unless the transcript explicitly says they are the same person/recommendation/rate.

COMPLETED tasks — mark done ONLY if the transcript contains direct evidence: past tense confirmation
("I did X", "done", "finished"), someone confirming completion, or the action clearly happening now.

NEW tasks — capture ONLY durable commitments with direct transcript evidence. Keep the task text minimal.
Do not include names, prices, dates, or relationships unless the evidence quote directly links them.

Reject:
- vague ideas, brainstorming, jokes, or notes-to-self without commitment
- generic baby-care guidance or transient reminders ("later today", "when a family member gets back")
- inferred tasks based only on proximity of topics
- tasks where the task text depends on unsupported details

Respond ONLY with valid JSON in this exact format:
{
  "completed": [
    {
      "task": "exact or paraphrased open task that was completed",
      "evidence_quote": "short quote from transcript proving completion",
      "confidence": "high|medium|low"
    }
  ],
  "new_tasks": [
    {
      "task": "specific actionable task with unsupported details removed",
      "evidence_quote": "short quote from transcript proving Michael's commitment",
      "confidence": "high|medium|low",
      "reject_if": "optional short reason this may be unsafe to create"
    }
  ],
  "reasoning": "one sentence explanation"
}

If nothing changed: {"completed": [], "new_tasks": [], "reasoning": "no high-confidence transcript-grounded changes detected"}"""


def analyze_conversation(transcript: str, summary: str, open_tasks: list[dict]) -> dict:
    """Call claude CLI to analyze a conversation. Returns {completed, new_tasks, reasoning}."""
    task_list = "\n".join(f"- {t['content']}" for t in open_tasks) or "(no open tasks)"
    claude_bin = shutil.which("claude") or next((p for p in CLAUDE_PATHS if Path(p).exists()), None)
    if not claude_bin:
        print("  [claude] executable not found", file=sys.stderr)
        return {"completed": [], "new_tasks": [], "reasoning": "analysis failed"}

    user_message = f"""OPEN TASKS:
{task_list}

CONVERSATION SUMMARY:
{summary}

FULL TRANSCRIPT:
{transcript or "(no transcript available)"}

Based on the above, which tasks were completed and what new tasks were mentioned?"""

    try:
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
            [claude_bin, "-p", SYSTEM_PROMPT],  # uses subscription default model
            input=user_message,
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        output = result.stdout.strip()
        if "hit your session limit" in output.lower():
            return {
                "completed": [],
                "new_tasks": [],
                "reasoning": "claude session limit hit; retry later",
                "retry_later": True,
            }
        # Extract JSON even if wrapped in markdown code fences
        json_match = re.search(r'\{.*\}', output, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        print(f"  [claude] no JSON in output: {output[:100]}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("  [claude] timed out after 90s", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"  [claude] invalid JSON: {e}", file=sys.stderr)
    except Exception as e:
        print(f"  [claude] error: {e}", file=sys.stderr)

    return {"completed": [], "new_tasks": [], "reasoning": "analysis failed", "retry_later": True}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(title: str, message: str) -> None:
    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def send_telegram(text: str) -> None:
    """Push a message to the user's phone via the Telegram Bot API.

    No-op (silently) unless both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set
    in .env. Failures never break the run — the desktop notify still fires.
    """
    token = load_env("TELEGRAM_BOT_TOKEN")
    chat_id = load_env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }).encode()
        req = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001 — alerting must never crash the agent
        print(f"  [telegram] send failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    todoist_token = load_env("TODOIST_API_TOKEN")
    if not todoist_token:
        print("Error: TODOIST_API_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    since_ms = state.get("last_processed_ms", 0)
    processed_ids = set(state.get("processed_ids", []))

    tz  = get_tz()
    now = datetime.now(tz)
    print(f"[bee-agent] {now.strftime('%Y-%m-%d %H:%M')} — checking since ms={since_ms}")

    try:
        new_convs = fetch_new_conversations(since_ms)
    except URLError as e:
        print(f"Error: Bee proxy unreachable — {e}", file=sys.stderr)
        sys.exit(1)

    # Filter out already-processed conversations
    new_convs = [c for c in new_convs if c["id"] not in processed_ids]

    if not new_convs:
        print("  No new conversations.")
        return

    print(f"  {len(new_convs)} new conversation(s) to analyze")

    open_tasks = fetch_open_bee_tasks(todoist_token)
    print(f"  {len(open_tasks)} open bee tasks in Todoist")

    create_immediately = env_flag("BEE_AGENT_CREATE_TASKS_IMMEDIATELY")

    total_completed = 0
    total_new       = 0
    completed_names: list[str] = []
    created_names: list[str]   = []
    latest_ms       = since_ms

    for conv in reversed(new_convs):  # process oldest first
        conv_id = conv["id"]
        ts      = conv["start_time"]
        short   = (conv.get("short_summary") or "").strip()
        summary = (conv.get("summary") or "").strip()
        dt      = datetime.fromtimestamp(ts / 1000, tz=tz)

        date_str = dt.strftime("%Y-%m-%d")
        print(f"\n  [{dt.strftime('%H:%M')}] {short[:60]}")

        # Fetch full transcript
        try:
            transcript = fetch_utterances(conv_id)
        except Exception as e:
            print(f"    Utterances unavailable: {e}")
            transcript = ""

        # Base archive record — raw transcript + Bee summary, paired with whatever
        # the pipeline concludes below so transcripts can be reviewed after the fact.
        transcript_record = {
            "conversation_id": conv_id,
            "conversation_start_time": ts,
            "conversation_time": dt.isoformat(),
            "short_summary": short,
            "bee_summary": summary,
            "transcript": transcript,
            "analysis": None,
            "completed_tasks": [],
            "staged_tasks": [],
        }

        if not open_tasks and not summary:
            print("    Skipping — no tasks or summary")
            save_transcript(date_str, transcript_record)
            processed_ids.add(conv_id)
            latest_ms = max(latest_ms, ts)
            continue

        # Analyze with Claude
        analysis = analyze_conversation(transcript, summary, open_tasks)
        transcript_record["analysis"] = analysis
        print(f"    reasoning: {analysis.get('reasoning', '')}")
        if analysis.get("retry_later"):
            save_state({
                "last_processed_ms": latest_ms,
                "processed_ids": list(processed_ids)[-500:],
            })
            print("    stopping backfill/run; this conversation will be retried later")
            return

        # Complete matched tasks
        task_map = {item_key(t["content"]): t for t in open_tasks}
        for completed_item in analysis.get("completed", []):
            completed_text = coerce_text_item(completed_item)
            if not completed_text:
                continue
            if isinstance(completed_item, dict) and confidence(completed_item) == "low":
                print(f"    skipping completion (low confidence): {completed_text}")
                continue
            key = item_key(completed_text)
            # Fuzzy match: find closest open task
            matched = task_map.get(key)
            if not matched:
                # Try substring match
                for k, t in task_map.items():
                    if key in k or k in key:
                        matched = t
                        break
            if matched:
                print(f"    ✅ completing: {matched['content']}")
                if complete_task(matched["id"], todoist_token):
                    open_tasks = [t for t in open_tasks if t["id"] != matched["id"]]
                    task_map.pop(item_key(matched["content"]), None)
                    total_completed += 1
                    completed_names.append(matched["content"])
                    transcript_record["completed_tasks"].append(matched["content"])
            else:
                print(f"    ⚠️  no match for: {completed_text}")

        # Create new tasks (deduped against still-open tasks)
        open_keys = {item_key(t["content"]) for t in open_tasks}
        for new_task_item in analysis.get("new_tasks", []):
            new_task_text = coerce_text_item(new_task_item)
            should_create, gate_reason = should_create_task(new_task_item)
            if not should_create:
                print(f"    skipping new task ({gate_reason}): {new_task_text}")
                continue
            if item_key(new_task_text) not in open_keys:
                if create_immediately:
                    print(f"    ➕ creating: {new_task_text}")
                    created = create_task(new_task_text, todoist_token)
                    if created.get("id"):
                        open_tasks.append(created)
                        open_keys.add(item_key(new_task_text))
                        total_new += 1
                        created_names.append(new_task_text)
                        transcript_record["staged_tasks"].append(new_task_text)
                    else:
                        save_state({
                            "last_processed_ms": latest_ms,
                            "processed_ids": list(processed_ids)[-500:],
                        })
                        print("    stopping backfill/run; Todoist create failed and this conversation will be retried later")
                        return
                else:
                    candidate = {
                        "type": "candidate_task",
                        "task": new_task_text,
                        "evidence_quote": evidence_quote(new_task_item),
                        "confidence": confidence(new_task_item),
                        "conversation_id": conv_id,
                        "conversation_start_time": ts,
                        "conversation_time": dt.isoformat(),
                        "short_summary": short,
                        "reasoning": analysis.get("reasoning", ""),
                    }
                    print(f"    📝 staging: {new_task_text}")
                    append_candidate(date_str, candidate)
                    open_keys.add(item_key(new_task_text))
                    total_new += 1
                    created_names.append(new_task_text)
                    transcript_record["staged_tasks"].append(new_task_text)

        save_transcript(date_str, transcript_record)
        processed_ids.add(conv_id)
        latest_ms = max(latest_ms, ts)

    # Save state
    save_state({
        "last_processed_ms": latest_ms,
        "processed_ids": list(processed_ids)[-500:],  # keep last 500 to bound size
    })

    # Notify if anything changed
    if total_completed > 0 or total_new > 0:
        parts = []
        if total_completed:
            parts.append(f"✅ {total_completed} done")
        if total_new:
            noun = "new" if create_immediately else "staged"
            parts.append(f"➕ {total_new} {noun}")
        notify("Bee Agent", " · ".join(parts))

        # Phone push with the actual task text (no-op unless Telegram is configured)
        tg_lines = [f"🐝 *Bee Agent* — {' · '.join(parts)}"]
        if created_names:
            noun = "New tasks" if create_immediately else "Staged tasks"
            tg_lines.append(f"\n➕ *{noun}:*")
            tg_lines += [f"• {n}" for n in created_names]
        if completed_names:
            tg_lines.append("\n✅ *Completed:*")
            tg_lines += [f"• {n}" for n in completed_names]
        send_telegram("\n".join(tg_lines))

        print(f"\n  Notified: {' · '.join(parts)}")
    else:
        print("\n  No changes to Todoist.")


if __name__ == "__main__":
    main()
