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
from __future__ import annotations

from bee_inference import infer  # PEP 604 (X | Y) hints work on the 3.9 system interpreter

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
# Speech feedback loop: weekly profile writes the focus here, agent logs live hits.
PROFILE_JSON_DIR = Path(__file__).parent / ".bee_speech_profile"
FLAG_DIR = Path(__file__).parent / ".bee_speech_flags"
# Task extraction runs every 30 min, but the speech-focus pass is throttled: each run
# buffers its transcripts and one detection call fires only once the interval elapses.
SPEECH_PENDING_FILE = Path(__file__).parent / ".bee_speech_pending.jsonl"
SPEECH_STATE_FILE = Path(__file__).parent / ".bee_speech_state.json"
SPEECH_DETECTION_INTERVAL_S = 2 * 3600  # at most one detection call every 2 hours
CLAUDE_PATHS = [
    "/Users/michael/.local/bin/claude",
    "/Users/michael/.npm-global/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]
AGY_PATHS = [
    "/Users/michael/.local/bin/agy",
    "/opt/homebrew/bin/agy",
    "/usr/local/bin/agy",
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


def week_label(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def load_active_focus(now: datetime) -> dict | None:
    """Read this week's speech-profile sidecar — the single pattern to watch for.
    Returns None (loop silently inactive) until the weekly profiler has run."""
    path = PROFILE_JSON_DIR / f"{week_label(now)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if (data.get("week_focus") or "").strip() else None


def append_speech_flag(date_str: str, flag: dict) -> None:
    FLAG_DIR.mkdir(parents=True, exist_ok=True)
    path = FLAG_DIR / f"{date_str}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(flag, ensure_ascii=False) + "\n")


def buffer_speech_block(block: dict) -> None:
    """Persist one analyzed conversation for the next throttled detection pass."""
    with SPEECH_PENDING_FILE.open("a") as f:
        f.write(json.dumps(block, ensure_ascii=False) + "\n")


def load_speech_pending() -> list[dict]:
    if not SPEECH_PENDING_FILE.exists():
        return []
    out = []
    for line in SPEECH_PENDING_FILE.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def clear_speech_pending() -> None:
    SPEECH_PENDING_FILE.unlink(missing_ok=True)


def speech_last_detection_ms() -> int:
    if SPEECH_STATE_FILE.exists():
        try:
            return int(json.loads(SPEECH_STATE_FILE.read_text()).get("last_detection_ms", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            return 0
    return 0


def set_speech_last_detection_ms(ms: int) -> None:
    SPEECH_STATE_FILE.write_text(json.dumps({"last_detection_ms": ms}))


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
    # Note: baby-care vs. concrete-chore disambiguation is handled in the LLM prompt now.
    # The old quote-commitment gate auto-rejected summary-sourced items, so it was removed.
    return True, "accepted"


# ---------------------------------------------------------------------------
# LLM analysis via claude CLI
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an accountability assistant analyzing a conversation captured by a wearable AI device worn by Michael.

Bee's summary contains a curated "## Action Items" section. Treat that list as the PRIMARY source of
candidate new tasks — it is a high-quality, pre-extracted list. Your job is to decide which of those
items are durable, actionable tasks that MICHAEL should own and put on his to-do list. The raw transcript
is noisy, un-diarized (every speaker shows as "Unknown"), and often mistranscribed — use it only as
supporting context to confirm ownership or completion. The ABSENCE of a clean transcript quote is NOT a
reason to reject a clear action item from the summary; quote the Bee action item or a summary line instead.

Your main failure mode to avoid: merging adjacent but unrelated facts. If one person recommends a
service, a different person is discussed elsewhere, and a price appears nearby, do NOT combine those
facts unless the source explicitly says they are the same person/recommendation/rate.

COMPLETED tasks — mark done if the summary or transcript indicates the action was completed: past tense
confirmation ("I did X", "done", "finished"), someone confirming completion, or the action clearly
happening now.

NEW tasks — promote the Bee action items that are durable commitments Michael owns. Keep the task text
minimal. Do not include names, prices, dates, or relationships unless the source directly links them.

Reject:
- action items that belong to someone other than Michael
- vague ideas, brainstorming, jokes, or notes-to-self without commitment
- generic baby-care guidance or transient reminders ("later today", "within 30 minutes", "when a family member gets back")
- sports-betting/game-watching chatter ("check betting outcomes", "match starts at 1 PM")
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
      "evidence_quote": "the Bee action item or summary/transcript line supporting this task",
      "confidence": "high|medium|low",
      "reject_if": "optional short reason this may be unsafe to create"
    }
  ],
  "reasoning": "one sentence explanation"
}

If nothing changed: {"completed": [], "new_tasks": [], "reasoning": "no high-confidence transcript-grounded changes detected"}"""


ACTION_ITEMS_RE = re.compile(r"## Action Items\s*\n+\s*((?:[-*].+\n?)+)", re.IGNORECASE)


def extract_action_items(summary: str) -> list[str]:
    """Pull Bee's curated '## Action Items' bullet list out of a conversation summary."""
    m = ACTION_ITEMS_RE.search(summary or "")
    if not m:
        return []
    return [ln.strip(" -*\t").lstrip("[ ]").strip()
            for ln in m.group(1).splitlines() if ln.strip(" -*\t")]


def get_env_dict() -> dict[str, str]:
    """Load the entire .env file as a dictionary."""
    env_dict = {}
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                env_dict[key.strip()] = val.strip()
    return env_dict


def call_llm(system_prompt: str, user_message: str, timeout_s: int = 90, force_provider: str | None = None) -> str:
    """Use subscription Claude with tools, MCP, hooks and skills disabled."""
    return infer(system_prompt, user_message, timeout_s)



def reconcile_analyses(transcript: str, open_tasks: list[dict], res_c: dict, res_g: dict) -> dict:
    """Run a reconciliation pass using Gemini/Agy (falling back to Claude) when Claude and Gemini differ."""
    task_list = "\n".join(f"- {t['content']}" for t in open_tasks) or "(no open tasks)"
    reconcile_prompt = """You are arbitrating between two AI analyses of a conversation transcript for an automated task manager.

ANALYSIS A (Claude):
%s

ANALYSIS B (Gemini):
%s

Your job:
1. Examine the open tasks, the transcript, and both analyses.
2. Resolve any differences.
3. Be highly conservative: only check off tasks if there is clear evidence, and only extract new tasks if they are durable, high-confidence commitments with direct quotes.
4. Output a single, finalized JSON resolving all contradictions.

Respond ONLY with valid JSON in the format:
{
  "completed": ["task content"],
  "new_tasks": [
     {"task": "content", "evidence_quote": "...", "confidence": "high|medium|low"}
  ],
  "reasoning": "Brief explanation of how you resolved discrepancies."
}""" % (json.dumps(res_c, indent=2), json.dumps(res_g, indent=2))

    user_message = f"""OPEN TASKS:
{task_list}

TRANSCRIPT:
{transcript or "(no transcript available)"}

Determine the final consolidated list of completed and new tasks."""

    # Reconciliation runs on Gemini to save Claude credits, falling back to Claude if needed.
    for provider in ("gemini", "claude"):
        try:
            output = call_llm(reconcile_prompt, user_message, force_provider=provider)
            m = re.search(r"\{.*\}", output, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception as e:
            print(f"  [reconciliation] failed with {provider}: {e}", file=sys.stderr)
            continue
    
    print("  [reconciliation] all providers failed, falling back to Claude analysis", file=sys.stderr)
    return res_c


def analyze_conversation(transcript: str, summary: str, open_tasks: list[dict]) -> dict:
    """Analyze a conversation using the configured LLM mode (Claude, Gemini, or Consensus)."""
    mode = "claude"  # Tool-free subscription inference; agy has no equivalent tool-off mode.
    task_list = "\n".join(f"- {t['content']}" for t in open_tasks) or "(no open tasks)"
    action_items = extract_action_items(summary)
    action_block = "\n".join(f"- {a}" for a in action_items) or "(none extracted)"
    
    user_message = f"""OPEN TASKS:
{task_list}

BEE ACTION ITEMS (primary candidate source — decide which are Michael's durable tasks):
{action_block}

CONVERSATION SUMMARY:
{summary}

FULL TRANSCRIPT (noisy supporting context only):
{transcript or "(no transcript available)"}

Based on the above, which open tasks were completed and which Bee action items should become Michael's new tasks?"""

    if mode == "consensus":
        print("  [llm] Running consensus mode (Claude + Gemini)...", file=sys.stderr)
        res_c, res_g = {}, {}
        
        # 1. Run Claude
        try:
            output_c = call_llm(SYSTEM_PROMPT, user_message, force_provider="claude")
            m = re.search(r"\{.*\}", output_c, re.DOTALL)
            res_c = json.loads(m.group()) if m else {}
        except Exception as e:
            print(f"  [llm] Claude run failed: {e}", file=sys.stderr)
            
        # 2. Run Gemini
        try:
            output_g = call_llm(SYSTEM_PROMPT, user_message, force_provider="gemini")
            m = re.search(r"\{.*\}", output_g, re.DOTALL)
            res_g = json.loads(m.group()) if m else {}
        except Exception as e:
            print(f"  [llm] Gemini run failed: {e}", file=sys.stderr)

        if not res_c and not res_g:
            return {"completed": [], "new_tasks": [], "reasoning": "both models failed", "retry_later": True}
        if not res_c:
            print("  [llm] Claude failed, using Gemini output directly", file=sys.stderr)
            return res_g
        if not res_g:
            print("  [llm] Gemini failed, using Claude output directly", file=sys.stderr)
            return res_c

        c_comp = sorted(k for t in res_c.get("completed", []) if (k := coerce_text_item(t).lower()))
        g_comp = sorted(k for t in res_g.get("completed", []) if (k := coerce_text_item(t).lower()))
        c_new = sorted(k for t in res_c.get("new_tasks", []) if (k := coerce_text_item(t).lower()))
        g_new = sorted(k for t in res_g.get("new_tasks", []) if (k := coerce_text_item(t).lower()))

        if c_comp == g_comp and c_new == g_new:
            print("  [llm] Claude and Gemini agree perfectly.", file=sys.stderr)
            return res_c
        
        print("  [llm] Disagreement detected. Running reconciliation pass...", file=sys.stderr)
        return reconcile_analyses(transcript, open_tasks, res_c, res_g)

    else:
        provider = "gemini" if mode in ("gemini", "agy") else "claude"
        try:
            output = call_llm(SYSTEM_PROMPT, user_message, force_provider=provider)
            m = re.search(r"\{.*\}", output, re.DOTALL)
            if m:
                return json.loads(m.group())
            print(f"  [{provider}] no JSON in output: {output[:100]}", file=sys.stderr)
        except Exception as e:
            print(f"  [{provider}] error during analysis: {e}", file=sys.stderr)
            if "session limit" in str(e):
                return {"completed": [], "new_tasks": [], "reasoning": f"{provider} session limit hit", "retry_later": True}
            
        return {"completed": [], "new_tasks": [], "reasoning": "analysis failed", "retry_later": True}


# ---------------------------------------------------------------------------
# Speech focus detection (real-time half of the speech feedback loop)
# ---------------------------------------------------------------------------

SPEECH_FOCUS_PROMPT = """You are watching for ONE specific speech pattern in conversations captured by Michael's wearable.

You are given:
- THE FOCUS: a single communication habit Michael decided to be more intentional about this week.
- PATTERN CONTEXT: how that habit tends to show up.
- NEW CONVERSATIONS: noisy, un-diarized transcripts (every speaker is "Unknown"; many words mistranscribed).

Your only job: find concrete moments where MICHAEL exhibited the focus pattern. This is evidence-gated —
every instance MUST include a direct quote from the transcript. If the audio is ambient media (TV, podcasts,
broadcasts) or you cannot attribute a line to Michael with reasonable confidence, do not flag it. Returning
nothing is correct and expected when the pattern did not clearly appear. Never invent instances.

Respond ONLY with valid JSON:
{
  "focus_instances": [
    {"quote": "exact transcript line showing the pattern", "note": "one phrase on why it matches", "confidence": "high|medium|low"}
  ],
  "summary": "one sentence on whether the pattern showed up this run"
}

If nothing clearly matched: {"focus_instances": [], "summary": "no clear instances"}"""


def detect_speech_focus(corpus: str, focus: dict) -> dict:
    """One LLM call per run: scan this run's new conversations for the active focus pattern."""
    mode = "claude"  # Tool-free subscription inference; agy has no equivalent tool-off mode.
    patterns = focus.get("speech_patterns") or []
    pattern_lines = "\n".join(
        f"- {p.get('pattern', '')}: {p.get('observation', '')}" for p in patterns
    ) or "(none provided)"
    user_message = f"""THE FOCUS:
{focus.get('week_focus', '')}

PATTERN CONTEXT:
{pattern_lines}

NEW CONVERSATIONS:
{corpus}

Find concrete moments where Michael exhibited the focus pattern."""

    # Soft flag: run Gemini for consensus/gemini modes, Claude for claude mode
    provider = "gemini" if mode in ("consensus", "gemini", "agy") else "claude"
    
    try:
        output = call_llm(SPEECH_FOCUS_PROMPT, user_message, force_provider=provider)
        m = re.search(r"\{.*\}", output, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"  [speech] detection failed: {e}", file=sys.stderr)
    return {"focus_instances": [], "summary": "detection failed"}


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
    focus = load_active_focus(now)  # speech-focus pattern for this week, if the profiler has run

    for conv in reversed(new_convs):  # process oldest first
        conv_id = conv["id"]
        ts      = conv["start_time"]
        short   = (conv.get("short_summary") or "").strip()
        summary = (conv.get("summary") or "").strip()
        state   = (conv.get("state") or "").upper()
        dt      = datetime.fromtimestamp(ts / 1000, tz=tz)

        date_str = dt.strftime("%Y-%m-%d")
        print(f"\n  [{dt.strftime('%H:%M')}] {short[:60]}")

        # Skip conversations Bee is still recording/processing. Do NOT mark them
        # processed or advance the watermark, so they get picked up on a later run
        # once their final summary (and Action Items) exists. Otherwise an in-progress
        # ("CAPTURING") conversation would be consumed before its tasks are extractable.
        if state in {"CAPTURING", "PROCESSING", "SUMMARIZING", "RECORDING"}:
            print(f"    Skipping — still {state.lower()} (will retry next run)")
            continue

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

        # Buffer for the throttled speech-focus pass (only while a focus is active).
        if focus and transcript:
            buffer_speech_block({"time": dt.isoformat(), "short": short, "transcript": transcript})

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

    # ── Speech feedback loop ────────────────────────────────────────────────
    # Throttled to one detection call every ~2h. Each run buffered its analyzed
    # transcripts above; once the interval elapses we scan the whole buffer in a
    # single call, log evidence-backed instances (with quotes), and reset. Next
    # week's profiler grades the focus against these logs. Silently inactive until
    # the first weekly profile sidecar exists.
    speech_flagged = 0
    if focus:
        now_ms = int(now.timestamp() * 1000)
        pending = load_speech_pending()
        due = now_ms - speech_last_detection_ms() >= SPEECH_DETECTION_INTERVAL_S * 1000
        if due and pending:
            corpus_parts, total = [], 0
            for b in pending:
                block = f"--- [{b['time'][:16]}] {b['short']}\n{b['transcript'][:1500]}"
                if total + len(block) > 40_000:
                    break
                corpus_parts.append(block)
                total += len(block)
            print(f"  [speech] 2h pass — scanning {len(corpus_parts)} buffered conversation(s)")
            result = detect_speech_focus("\n\n".join(corpus_parts), focus)
            date_str_now = now.strftime("%Y-%m-%d")
            for inst in result.get("focus_instances", []):
                quote = (inst.get("quote") or "").strip()
                conf = (inst.get("confidence") or "").strip().lower()
                if not quote or conf == "low":
                    continue  # evidence-gated: require a real quote, drop low confidence
                append_speech_flag(date_str_now, {
                    "week": focus.get("week"),
                    "focus": focus.get("week_focus"),
                    "quote": quote,
                    "note": (inst.get("note") or "").strip(),
                    "confidence": conf,
                    "logged_at": now.isoformat(),
                })
                speech_flagged += 1
            clear_speech_pending()
            set_speech_last_detection_ms(now_ms)
            if speech_flagged:
                print(f"  [speech] logged {speech_flagged} focus instance(s)")
        elif pending:
            print(f"  [speech] buffering {len(pending)} conv(s); 2h pass not yet due")

    # Notify if anything changed
    if total_completed > 0 or total_new > 0 or speech_flagged > 0:
        parts = []
        if total_completed:
            parts.append(f"✅ {total_completed} done")
        if total_new:
            noun = "new" if create_immediately else "staged"
            parts.append(f"➕ {total_new} {noun}")
        if speech_flagged:
            parts.append(f"🗣 {speech_flagged} speech")
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
        if speech_flagged and focus:
            tg_lines.append(f"\n🗣 *Speech focus* — {speech_flagged}× this run")
            tg_lines.append(f"_{(focus.get('week_focus') or '')[:140]}_")
        send_telegram("\n".join(tg_lines))

        print(f"\n  Notified: {' · '.join(parts)}")
    else:
        print("\n  No changes to Todoist.")


if __name__ == "__main__":
    main()
