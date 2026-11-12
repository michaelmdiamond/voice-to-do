#!/usr/bin/env python3
"""
bee_speech_profile.py — Weekly speech pattern analysis from Bee transcripts.

Reads the past N days of archived Bee transcripts (.bee_transcripts/) and asks
Claude to identify patterns in HOW Michael speaks: filler words, hedging, recurring
topics, and communication style. Writes a weekly profile note to Obsidian.

Uses: claude CLI (no API key — uses your Claude subscription)
Schedule: ~/Library/LaunchAgents/com.michael.bee-speech-profile.plist (weekly)
"""
from __future__ import annotations

from bee_inference import infer

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
TRANSCRIPT_DIR = ROOT / ".bee_transcripts"
# Machine-readable focus contract that bee_agent.py reads each run, plus the
# real-time flags it writes back. Both are gitignored (captured/derived data).
PROFILE_JSON_DIR = ROOT / ".bee_speech_profile"
FLAG_DIR = ROOT / ".bee_speech_flags"
VAULT = Path("/Users/michael/Obsidian Vault")
SPEECH_PROFILE_DIR = VAULT / "Speech Profile"
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
DEFAULT_DAYS = 7
MAX_TRANSCRIPT_CHARS = 1200  # per conversation — raw transcripts are noisy
MAX_TOTAL_CHARS = 80_000     # trim oldest conversations if exceeded

SYSTEM_PROMPT = """You are analyzing speech patterns from Bee wearable transcripts captured over the past week.

The transcripts are noisy: all speakers show as "Unknown" and words are often mistranscribed. Focus on patterns
that appear consistently across many conversations rather than drawing conclusions from single instances.

Your goal is to identify HOW Michael speaks — not what he decided or committed to, but his communication habits,
verbal patterns, and style tendencies that he could be more intentional about.

Analyze for:
1. Filler words and hedging language (like, basically, kind of, I think, I guess, sort of, literally, you know)
2. Commitment phrasing patterns (how he expresses intent — "I'll", "I'm going to", "we should", "maybe I'll")
3. Recurring topics or concerns that surface across multiple conversations
4. Question vs. statement balance (does he ask for input or make declarations?)
5. Communication register shifts across contexts
6. Anything worth being more intentional about

If a PREVIOUS WEEK'S PROFILE is included, add a "changes_since_last_week" field noting what shifted.
If LOGGED FOCUS INSTANCES are included, they are real moments — captured live during the week — where
Michael exhibited last week's focus pattern. Ground "changes_since_last_week" in these counts and quotes
(e.g. "still slipping into tentative phrasing — 9 logged instances, down from the prior week"), not vibes.
Use them to judge whether the prior focus stuck or should carry over into this week's focus.

Respond ONLY with valid JSON:
{
  "filler_words": [
    {"word": "basically", "frequency": "high|medium|low", "example": "short quote or null"}
  ],
  "speech_patterns": [
    {"pattern": "brief name", "observation": "1-2 sentences", "example": "short quote or null"}
  ],
  "recurring_topics": [
    {"topic": "topic name", "note": "brief observation about how it keeps coming up"}
  ],
  "communication_style": "2-3 sentence overall characterization — honest, not flattering",
  "week_focus": "one specific, actionable thing worth being intentional about this week",
  "changes_since_last_week": "what shifted vs. last week, or null if no prior profile",
  "data_quality": "note any significant issues with transcript quality that limited analysis"
}"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env(key: str) -> str | None:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                return val or None
    return os.environ.get(key) or None


def get_tz() -> ZoneInfo:
    return ZoneInfo(load_env("TZ") or "America/New_York")


def week_label(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def prev_week_label(label: str) -> str:
    """Return the ISO week label one week prior, e.g. 2026-W26 -> 2026-W25."""
    year, week = int(label[:4]), int(label[6:])
    week -= 1
    if week == 0:
        year -= 1
        week = datetime(year, 12, 28).isocalendar()[1]  # last ISO week of previous year
    return f"{year}-W{week:02d}"


def load_window_flags(days: int, tz: ZoneInfo) -> list[dict]:
    """Read the real-time focus flags bee_agent.py logged during the window."""
    flags: list[dict] = []
    today = datetime.now(tz).date()
    for i in range(days - 1, -1, -1):
        date = today - timedelta(days=i)
        path = FLAG_DIR / f"{date.isoformat()}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                flags.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return flags


# ---------------------------------------------------------------------------
# Transcript loading
# ---------------------------------------------------------------------------

def load_transcripts(days: int, tz: ZoneInfo) -> list[dict]:
    records: list[dict] = []
    today = datetime.now(tz).date()
    for i in range(days - 1, -1, -1):
        date = today - timedelta(days=i)
        path = TRANSCRIPT_DIR / f"{date.isoformat()}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def build_corpus(records: list[dict]) -> str:
    """Build a text corpus from transcript records within the char budget."""
    blocks: list[str] = []
    total = 0
    for r in records:
        ts = r.get("conversation_time", "")[:16]
        short = (r.get("short_summary") or "").strip()
        transcript = (r.get("transcript") or "").strip()[:MAX_TRANSCRIPT_CHARS]
        summary = (r.get("bee_summary") or "").strip()

        parts = [f"--- [{ts}] {short}"]
        if summary:
            parts.append(f"SUMMARY:\n{summary[:500]}")
        if transcript:
            parts.append(f"TRANSCRIPT:\n{transcript}")
        block = "\n".join(parts)

        if total + len(block) > MAX_TOTAL_CHARS:
            break
        blocks.append(block)
        total += len(block)

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def summarize_flags(flags: list[dict]) -> str:
    """Render logged focus instances into a compact, evidence-grounded block."""
    if not flags:
        return ""
    focus = next((f.get("focus") for f in flags if f.get("focus")), "")
    lines = [f"Last week's focus was: {focus}", f"Logged instances: {len(flags)}"]
    for f in flags[:25]:  # cap — quotes are the signal, not exhaustiveness
        quote = (f.get("quote") or "").strip()
        note = (f.get("note") or "").strip()
        when = (f.get("logged_at") or "")[:16]
        if quote:
            lines.append(f'- [{when}] "{quote}"' + (f" — {note}" if note else ""))
    return "\n".join(lines)


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


def call_claude(corpus: str, days: int, prev_profile: str | None,
                logged_flags: list[dict] | None = None) -> dict:
    mode = "claude"  # Tool-free subscription inference; agy has no equivalent tool-off mode.
    provider = "gemini" if mode in ("gemini", "consensus", "agy") else "claude"

    parts = [f"Analyze speech patterns across {days} days of conversations:\n\n{corpus}"]
    if prev_profile:
        parts.append(f"\nPREVIOUS WEEK'S PROFILE (for comparison):\n{prev_profile[:3000]}")
    flag_block = summarize_flags(logged_flags or [])
    if flag_block:
        parts.append(f"\nLOGGED FOCUS INSTANCES (live captures of last week's focus pattern):\n{flag_block}")
    user_message = "\n".join(parts)

    output = infer(SYSTEM_PROMPT, user_message, 300)

    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        raise RuntimeError(f"Model returned no JSON: {output[:200]}")
    return json.loads(match.group())



# ---------------------------------------------------------------------------
# Note rendering
# ---------------------------------------------------------------------------

def build_note(week: str, days: int, conv_count: int, profile: dict) -> str:
    lines = [f"# Speech Profile — {week}", ""]
    lines += [f"*{days}-day window · {conv_count} conversations*", ""]

    style = (profile.get("communication_style") or "").strip()
    if style:
        lines += ["## Communication Style", "", style, ""]

    filler = profile.get("filler_words") or []
    if filler:
        lines += ["## Filler Words & Hedging", ""]
        for f in filler:
            word = f.get("word", "")
            freq = f.get("frequency", "")
            example = (f.get("example") or "").strip()
            line = f"- **{word}** ({freq})"
            if example:
                line += f' — *"{example}"*'
            lines.append(line)
        lines.append("")

    patterns = profile.get("speech_patterns") or []
    if patterns:
        lines += ["## Speech Patterns", ""]
        for p in patterns:
            name = p.get("pattern", "")
            obs = (p.get("observation") or "").strip()
            ex = (p.get("example") or "").strip()
            lines.append(f"### {name}")
            if obs:
                lines.append(obs)
            if ex:
                lines.append(f'> "{ex}"')
            lines.append("")

    topics = profile.get("recurring_topics") or []
    if topics:
        lines += ["## Recurring Topics", ""]
        for t in topics:
            topic = t.get("topic", "")
            note = (t.get("note") or "").strip()
            lines.append(f"- **{topic}**" + (f" — {note}" if note else ""))
        lines.append("")

    focus = (profile.get("week_focus") or "").strip()
    if focus:
        lines += ["## This Week's Focus", "", f"> {focus}", ""]

    changes = (profile.get("changes_since_last_week") or "").strip()
    if changes and changes.lower() != "null":
        lines += ["## vs. Last Week", "", changes, ""]

    quality = (profile.get("data_quality") or "").strip()
    if quality:
        lines += ["---", "", f"*Data quality: {quality}*", ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly speech profile from Bee transcripts")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Lookback window in days (default: 7)")
    parser.add_argument("--week", help="Override week label, e.g. 2026-W26")
    parser.add_argument("--dry-run", action="store_true", help="Print note without writing to Obsidian")
    args = parser.parse_args()

    tz = get_tz()
    label = args.week or week_label(datetime.now(tz))
    print(f"[bee-speech-profile] {label} — {args.days}-day window")

    records = load_transcripts(args.days, tz)
    if not records:
        print("  No transcript data in .bee_transcripts/. Run bee_agent.py first.")
        sys.exit(0)
    print(f"  {len(records)} conversation records")

    corpus = build_corpus(records)
    print(f"  Corpus: {len(corpus):,} chars")

    # Load previous week's profile for comparison if it exists
    prev_label = prev_week_label(label)
    prev_path = SPEECH_PROFILE_DIR / f"{prev_label}.md"
    prev_profile: str | None = None
    if prev_path.exists():
        prev_profile = prev_path.read_text()
        print(f"  Previous profile found: {prev_label}")
    else:
        print(f"  No previous profile ({prev_label})")

    # Load the real-time flags the agent logged this week — closes the loop:
    # last week's focus is graded against actual logged instances, not memory.
    logged_flags = load_window_flags(args.days, tz)
    if logged_flags:
        print(f"  {len(logged_flags)} logged focus instances this window")

    print("  Calling Claude...")
    try:
        profile = call_claude(corpus, args.days, prev_profile, logged_flags)
    except RuntimeError as e:
        print(f"  Error: {e}", file=sys.stderr)
        sys.exit(1)

    note = build_note(label, args.days, len(records), profile)

    if args.dry_run:
        print("\n--- NOTE PREVIEW ---")
        print(note)
        return

    SPEECH_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    note_path = SPEECH_PROFILE_DIR / f"{label}.md"
    note_path.write_text(note)
    print(f"  → {note_path}")

    # Machine-readable sidecar: this is the contract bee_agent.py reads to know
    # which pattern to watch for in real time during the coming week.
    focus = (profile.get("week_focus") or "").strip()
    PROFILE_JSON_DIR.mkdir(parents=True, exist_ok=True)
    sidecar = {
        "week": label,
        "generated_at": datetime.now(tz).isoformat(),
        "week_focus": focus,
        "speech_patterns": profile.get("speech_patterns") or [],
        "communication_style": profile.get("communication_style") or "",
    }
    (PROFILE_JSON_DIR / f"{label}.json").write_text(
        json.dumps(sidecar, indent=2, ensure_ascii=False))
    print(f"  → {PROFILE_JSON_DIR / f'{label}.json'}")

    subprocess.run(
        ["osascript", "-e",
         f'display notification "{focus}" with title "Speech Profile — {label}"'],
        capture_output=True,
    )


if __name__ == "__main__":
    main()
