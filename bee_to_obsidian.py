#!/usr/bin/env python3
"""
bee_to_obsidian.py — Pull today's Bee conversations and todos into an Obsidian daily note.

Fetches today's conversations (paginating through the proxy), extracts action items,
surfaces stale items via Todoist (or Obsidian checkboxes as fallback), pushes new
action items to Todoist, and writes a structured markdown file to the vault.

Requires: bee proxy running at http://127.0.0.1:8787
Optional: TODOIST_API_TOKEN in .env for Todoist sync
Optional: BEE_OBSIDIAN_PUSH_ACTIONS=1 to push Bee summary action items to Todoist
Schedule: launchd plist at ~/Library/LaunchAgents/com.michael.bee-to-obsidian.plist
"""
from __future__ import annotations  # PEP 604 (X | Y) hints work on the 3.9 system interpreter

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from zoneinfo import ZoneInfo

BEE_BASE      = "http://127.0.0.1:8787"
TODOIST_BASE  = "https://api.todoist.com/api/v1"
BEE_LABEL     = "bee"           # Todoist label applied to all bee-sourced tasks
VAULT         = Path("/Users/michael/Obsidian Vault")
DAILY_NOTES   = VAULT / "Daily Notes"


# ---------------------------------------------------------------------------
# Config
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


def item_key(s: str) -> str:
    """Normalize an action item string for deduplication matching."""
    return re.sub(r"\s+", " ", s.lower().strip()).rstrip(".,;")


# ---------------------------------------------------------------------------
# Bee API helpers
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


def day_bounds_ms(date: datetime) -> tuple[int, int]:
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = date.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def fetch_conversations_for_day(tz: ZoneInfo, target: datetime) -> list[dict]:
    """Page through /v1/conversations (newest-first) and collect those on target date."""
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
            break
    return results


def fetch_open_bee_todos() -> list[dict]:
    return [t for t in bee_get("/v1/todos").get("todos", []) if not t.get("completed")]


# ---------------------------------------------------------------------------
# Todoist API helpers
# ---------------------------------------------------------------------------

def todoist_request(
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
    params: dict | None = None,
) -> dict:
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
        return {}
    except Exception as e:
        print(f"  [todoist] {method} {path} failed: {e}", file=sys.stderr)
        return {}


def fetch_todoist_tasks(token: str, **params) -> list[dict]:
    tasks: list[dict] = []
    cursor: str | None = None
    while True:
        p = {**params}
        if cursor:
            p["cursor"] = cursor
        data = todoist_request("GET", "/tasks", token, params=p)
        tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return tasks


def push_to_todoist(items: list[str], token: str) -> int:
    """Create Todoist tasks for action items not already tracked. Returns count created."""
    existing = fetch_todoist_tasks(token, label=BEE_LABEL)
    existing_keys = {item_key(t["content"]) for t in existing}
    created = 0
    for item in items:
        if item_key(item) not in existing_keys:
            todoist_request("POST", "/tasks", token, body={
                "content": item,
                "labels": [BEE_LABEL],
                "description": "Captured via Bee wearable",
            })
            created += 1
    return created


def todoist_stale_items(yesterday_items: list[str], token: str) -> list[str]:
    """Return items from yesterday still open in Todoist (not yet completed)."""
    open_tasks  = fetch_todoist_tasks(token, label=BEE_LABEL)
    open_keys   = {item_key(t["content"]) for t in open_tasks}
    return [item for item in yesterday_items if item_key(item) in open_keys]


# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------

def extract_action_items(summary: str) -> list[str]:
    """Pull bullet lines from the ## Action Items section of a Bee summary."""
    m = re.search(r"## Action Items\n+\s*((?:-.+\n?)+)", summary)
    if not m:
        return []
    return [
        line.lstrip("- ").strip()
        for line in m.group(1).strip().splitlines()
        if line.strip().startswith("-")
    ]


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out:  list[str] = []
    for item in items:
        if item_key(item) not in seen:
            seen.add(item_key(item))
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Accountability loop
# ---------------------------------------------------------------------------

def read_yesterday_action_items(tz: ZoneInfo) -> list[str]:
    """Read all action items (checked or not) from yesterday's note."""
    yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
    path = DAILY_NOTES / f"{yesterday}.md"
    if not path.exists():
        return []
    text = path.read_text()
    m = re.search(r"## Today's Action Items\n((?:- \[.\] .+\n?)*)", text)
    if not m:
        return []
    return [line[6:].strip() for line in m.group(1).splitlines() if line.startswith("- [")]


def obsidian_stale_items(tz: ZoneInfo) -> list[str]:
    """Fallback: unchecked items from yesterday's Obsidian note."""
    yesterday = (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
    path = DAILY_NOTES / f"{yesterday}.md"
    if not path.exists():
        return []
    text = path.read_text()
    m = re.search(r"## Today's Action Items\n((?:- \[.\] .+\n?)*)", text)
    if not m:
        return []
    return [line[6:].strip() for line in m.group(1).splitlines() if line.startswith("- [ ] ")]


# ---------------------------------------------------------------------------
# Note builder
# ---------------------------------------------------------------------------

def build_note(
    date_str: str,
    convs: list[dict],
    open_bee_todos: list[dict],
    stale: list[str],
    tz: ZoneInfo,
    todoist_token: str | None,
) -> str:
    all_actions = dedupe([
        item
        for conv in convs
        for item in extract_action_items(conv.get("summary") or "")
    ])

    lines: list[str] = [f"# Daily Note — {date_str}", ""]

    # --- Open Bee todos ---
    lines += ["## Open To-Dos", ""]
    if open_bee_todos:
        for t in open_bee_todos:
            lines.append(f"- [ ] {t['text']}")
    else:
        lines.append("_No open to-dos._")
    lines.append("")

    # --- Accountability: stale items from yesterday ---
    if stale:
        src = "Todoist" if todoist_token else "yesterday"
        lines += [f"## Stale From Yesterday _(still open in {src})_", ""]
        for item in stale:
            lines.append(f"- [ ] {item}")
        lines.append("")

    # --- Conversations ---
    lines += ["## Today's Conversations", ""]
    if convs:
        for conv in sorted(convs, key=lambda c: c["start_time"]):
            dt    = datetime.fromtimestamp(conv["start_time"] / 1000, tz=tz)
            short = (conv.get("short_summary") or "").strip()
            body  = (conv.get("summary") or "").strip()
            lines += [f"### {dt.strftime('%H:%M')} — {short}", "", body, ""]
    else:
        lines += ["_No conversations recorded today._", ""]

    # --- Aggregated action items ---
    lines += ["## Today's Action Items", ""]
    if all_actions:
        for item in all_actions:
            lines.append(f"- [ ] {item}")
    else:
        lines.append("_No action items extracted._")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backfill helpers
# ---------------------------------------------------------------------------

def fetch_all_daily_summary_dates(tz: ZoneInfo) -> list[str]:
    """Return all dates (YYYY-MM-DD) that have a Bee daily summary, oldest first."""
    data = bee_get("/v1/daily")
    dates = []
    for s in data.get("daily_summaries", []):
        dt = datetime.fromtimestamp(s["date_time"] / 1000, tz=tz)
        dates.append(dt.strftime("%Y-%m-%d"))
    return sorted(set(dates))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_for_date(date_str: str, tz: ZoneInfo, todoist_token: str | None, is_today: bool) -> None:
    note_path = DAILY_NOTES / f"{date_str}.md"
    target    = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)

    convs          = fetch_conversations_for_day(tz, target)
    open_bee_todos = fetch_open_bee_todos() if is_today else []

    all_actions = dedupe([
        item
        for conv in convs
        for item in extract_action_items(conv.get("summary") or "")
    ])

    if is_today and todoist_token:
        yesterday_items = read_yesterday_action_items(tz)
        stale           = todoist_stale_items(yesterday_items, todoist_token)
        push_actions    = env_flag("BEE_OBSIDIAN_PUSH_ACTIONS")
        pushed          = push_to_todoist(all_actions, todoist_token) if push_actions else 0
        push_note       = "pushed to Todoist" if push_actions else "push disabled"
        print(f"  {len(convs)} conversations · {len(open_bee_todos)} bee todos · "
              f"{len(stale)} stale · {pushed} {push_note}")
    elif is_today:
        stale  = obsidian_stale_items(tz)
        pushed = 0
        print(f"  {len(convs)} conversations · {len(open_bee_todos)} bee todos · "
              f"{len(stale)} stale (no Todoist token)")
    else:
        stale = []
        print(f"  {len(convs)} conversations · {len(all_actions)} action items")

    note_path.write_text(build_note(date_str, convs, open_bee_todos, stale, tz, todoist_token))
    print(f"  → {note_path}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Bee → Obsidian daily note writer")
    parser.add_argument("--date", help="Write note for a specific date (YYYY-MM-DD)")
    parser.add_argument("--backfill", action="store_true",
                        help="Generate notes for all available Bee daily summary dates")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing notes (used with --backfill)")
    args = parser.parse_args()

    DAILY_NOTES.mkdir(parents=True, exist_ok=True)

    todoist_token = load_env("TODOIST_API_TOKEN")
    tz            = get_tz()
    today_str     = datetime.now(tz).strftime("%Y-%m-%d")

    try:
        if args.backfill:
            dates = fetch_all_daily_summary_dates(tz)
            print(f"[bee→obsidian] backfilling {len(dates)} dates")
            for date_str in dates:
                note_path = DAILY_NOTES / f"{date_str}.md"
                if note_path.exists() and not args.force and date_str != today_str:
                    print(f"  {date_str} — skipping (exists, use --force to overwrite)")
                    continue
                print(f"[bee→obsidian] {date_str}")
                run_for_date(date_str, tz, todoist_token, is_today=(date_str == today_str))
        else:
            date_str = args.date or today_str
            print(f"[bee→obsidian] {date_str}")
            run_for_date(date_str, tz, todoist_token, is_today=(date_str == today_str))

    except URLError as e:
        print(f"Error: Bee proxy unreachable — {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
