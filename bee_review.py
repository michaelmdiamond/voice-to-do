#!/usr/bin/env python3
"""
bee_review.py — Interactive review of Bee agent task extractions.

Reads from .bee_transcripts/ and lets you approve/reject/edit each proposed task.
Saves labeled examples to .bee_labels/ for prompt improvement over time.

Usage:
    python3 bee_review.py                    # review all unlabeled extractions
    python3 bee_review.py --date 2026-06-24  # review a specific date
    python3 bee_review.py --stats            # show label stats and rejection patterns
    python3 bee_review.py --all              # re-review already-labeled extractions too
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TRANSCRIPT_DIR = Path(__file__).parent / ".bee_transcripts"
LABEL_DIR      = Path(__file__).parent / ".bee_labels"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_transcripts(date_filter: str | None) -> list[tuple[str, dict]]:
    records: list[tuple[str, dict]] = []
    if not TRANSCRIPT_DIR.exists():
        return records
    files = sorted(TRANSCRIPT_DIR.glob("*.jsonl"))
    if date_filter:
        files = [f for f in files if f.stem == date_filter]
    for path in files:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append((path.stem, json.loads(line)))
                except json.JSONDecodeError:
                    pass
    return records


def load_labeled_keys(date_str: str) -> set[tuple[int, str]]:
    """Return (conversation_id, task) pairs already labeled on this date."""
    path = LABEL_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return set()
    keys: set[tuple[int, str]] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                r = json.loads(line)
                keys.add((r["conversation_id"], r["task"]))
            except Exception:
                pass
    return keys


def append_label(date_str: str, record: dict) -> None:
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    path = LABEL_DIR / f"{date_str}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

DIVIDER_THIN  = "─" * 62
DIVIDER_THICK = "═" * 62


def trunc(text: str, n: int = 90) -> str:
    return text[:n] + "…" if len(text) > n else text


def print_wrapped(text: str, indent: str = "  ", width: int = 80) -> None:
    words = text.split()
    line = indent
    for word in words:
        if len(line) + len(word) + 1 > width:
            print(line)
            line = indent + word
        else:
            line += (" " if line != indent else "") + word
    if line.strip():
        print(line)


# ---------------------------------------------------------------------------
# Review loop
# ---------------------------------------------------------------------------

def extract_tasks_for_review(
    conv: dict,
    date_str: str,
    labeled: set[tuple[int, str]],
    include_labeled: bool,
) -> list[dict]:
    """Return task dicts from the LLM analysis that need review."""
    conv_id = conv.get("conversation_id", 0)
    analysis = conv.get("analysis") or {}
    staged_set = set(conv.get("staged_tasks", []))

    items: list[dict] = []

    for item in analysis.get("new_tasks", []):
        if isinstance(item, dict):
            task = item.get("task", "").strip()
            evidence = item.get("evidence_quote", "").strip()
            confidence = item.get("confidence", "").strip()
            reject_if = item.get("reject_if", "").strip()
        else:
            task = str(item).strip()
            evidence = confidence = reject_if = ""

        if not task:
            continue

        was_staged = task in staged_set
        key = (conv_id, task)

        if not include_labeled and key in labeled:
            continue

        items.append({
            "task": task,
            "evidence_quote": evidence,
            "confidence": confidence,
            "reject_if": reject_if,
            "was_staged": was_staged,
            "task_type": "new_task",
        })

    for item in analysis.get("completed", []):
        if isinstance(item, dict):
            task = item.get("task", "").strip()
            evidence = item.get("evidence_quote", "").strip()
            confidence = item.get("confidence", "").strip()
        else:
            task = str(item).strip()
            evidence = confidence = ""

        if not task:
            continue

        labeled_key = f"[completed] {task}"
        key = (conv_id, labeled_key)
        if not include_labeled and key in labeled:
            continue

        items.append({
            "task": task,
            "evidence_quote": evidence,
            "confidence": confidence,
            "reject_if": "",
            "was_staged": False,
            "task_type": "completion",
        })

    return items


def prompt_task(task_info: dict, conv: dict, idx: int, total: int) -> dict | None:
    """
    Prompt for one task. Returns:
      {"action": "approve"|"reject"|"edit"|"skip"}  (with optional extra keys)
      None  → user wants to quit
    """
    task = task_info["task"]
    evidence = task_info["evidence_quote"]
    was_staged = task_info["was_staged"]
    task_type = task_info["task_type"]
    confidence = task_info["confidence"]

    print()
    print(DIVIDER_THIN)

    if task_type == "completion":
        print(f"  [{idx}/{total}] COMPLETION CHECK")
    else:
        status = "✅ STAGED TO TODOIST" if was_staged else "🚫 FILTERED (not staged)"
        print(f"  [{idx}/{total}] {status}")

    print(f"  Task: {task}")
    if evidence:
        print(f"  Evidence: {trunc(evidence)}")
    if confidence:
        print(f"  Confidence: {confidence}")
    if task_info.get("reject_if"):
        print(f"  Model caveat: {task_info['reject_if']}")
    print(DIVIDER_THIN)

    while True:
        raw = input("  [a]pprove  [r]eject  [e]dit  [s]kip  [?]context  [q]uit > ").strip().lower()

        if raw == "q":
            return None

        if raw == "s":
            return {"action": "skip"}

        if raw == "a":
            return {"action": "approve"}

        if raw == "r":
            reason = input("  Rejection reason: ").strip()
            return {"action": "reject", "rejection_reason": reason}

        if raw == "e":
            edited = input(f"  Edit [{task}]: ").strip()
            return {"action": "edit", "edited_text": edited or task}

        if raw == "?":
            print()
            print("  BEE ACTION ITEMS:")
            from bee_agent import extract_action_items  # type: ignore[import]
            summary = conv.get("bee_summary", "")
            for ai in extract_action_items(summary):
                print(f"    • {ai}")
            print()
            print("  CONVERSATION SUMMARY (first 40 lines):")
            for line in summary.splitlines()[:40]:
                print(f"    {line}")
            reasoning = (conv.get("analysis") or {}).get("reasoning", "")
            if reasoning:
                print(f"\n  CLAUDE REASONING: {reasoning}")
            print()
            continue

        print("  Enter a/r/e/s/?/q")


def review_mode(date_filter: str | None, include_labeled: bool) -> None:
    all_records = load_transcripts(date_filter)

    # Group into (date_str, conv, [tasks]) tuples, skipping convs with nothing to review
    work: list[tuple[str, dict, list[dict]]] = []
    for date_str, conv in all_records:
        labeled = load_labeled_keys(date_str)
        tasks = extract_tasks_for_review(conv, date_str, labeled, include_labeled)
        if tasks:
            work.append((date_str, conv, tasks))

    if not work:
        print("Nothing to review.")
        return

    total_tasks = sum(len(t) for _, _, t in work)
    print(f"\nBee Review — {total_tasks} extraction(s) across {len(work)} conversation(s)")
    print("(s = skip without labeling; ? = show conversation context)\n")

    approved = rejected = edited = skipped = task_num = 0

    for date_str, conv, tasks in work:
        conv_id = conv.get("conversation_id", 0)
        conv_time = conv.get("conversation_time", "")[:16]
        short = conv.get("short_summary", f"conv {conv_id}")
        reasoning = (conv.get("analysis") or {}).get("reasoning", "")

        print()
        print(DIVIDER_THICK)
        print(f"  {conv_time}  {trunc(short, 60)}")
        if reasoning:
            print_wrapped(f"Claude: {reasoning}", indent="  ")
        print(DIVIDER_THICK)

        for task_info in tasks:
            task_num += 1
            result = prompt_task(task_info, conv, task_num, total_tasks)

            if result is None:
                print(f"\nExiting. {approved} approved · {rejected} rejected · {edited} edited · {skipped} skipped")
                return

            action = result["action"]
            if action == "skip":
                skipped += 1
                continue

            task_key = task_info["task"] if task_info["task_type"] == "new_task" else f"[completed] {task_info['task']}"

            label: dict = {
                "conversation_id": conv_id,
                "conversation_time": conv.get("conversation_time", ""),
                "short_summary": short,
                "task": task_key,
                "task_type": task_info["task_type"],
                "evidence_quote": task_info["evidence_quote"],
                "confidence_claimed": task_info["confidence"],
                "was_staged": task_info["was_staged"],
                "action": action,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }

            if action == "approve":
                approved += 1
            elif action == "reject":
                rejected += 1
                label["rejection_reason"] = result.get("rejection_reason", "")
            elif action == "edit":
                edited += 1
                label["edited_text"] = result.get("edited_text", task_info["task"])

            append_label(date_str, label)

    print(f"\n{DIVIDER_THIN}")
    print(f"Done. {approved} approved · {rejected} rejected · {edited} edited · {skipped} skipped")


# ---------------------------------------------------------------------------
# Stats mode
# ---------------------------------------------------------------------------

def stats_mode() -> None:
    if not LABEL_DIR.exists() or not any(LABEL_DIR.glob("*.jsonl")):
        print("No labels yet — run bee_review.py to start labeling.")
        return

    total = approved = rejected = edited = 0
    staged_correct = staged_wrong = 0
    rejection_reasons: list[str] = []

    for path in sorted(LABEL_DIR.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                action = r.get("action", "skip")
                if action == "skip":
                    continue
                total += 1
                was_staged = r.get("was_staged", False)
                if action == "approve":
                    approved += 1
                    if was_staged:
                        staged_correct += 1
                elif action == "reject":
                    rejected += 1
                    if was_staged:
                        staged_wrong += 1
                    reason = r.get("rejection_reason", "").strip()
                    if reason:
                        rejection_reasons.append(reason)
                elif action == "edit":
                    edited += 1
            except Exception:
                pass

    def pct(n: int, d: int) -> str:
        return f"{100 * n // d}%" if d else "—"

    print(f"\nBee Label Stats")
    print(DIVIDER_THIN)
    print(f"  Total labeled:   {total}")
    print(f"  Approved:        {approved} ({pct(approved, total)})")
    print(f"  Rejected:        {rejected} ({pct(rejected, total)})")
    print(f"  Edited:          {edited} ({pct(edited, total)})")

    if staged_correct + staged_wrong > 0:
        print(f"\n  Tasks that reached Todoist:")
        print(f"    Correct:   {staged_correct}")
        print(f"    Wrong:     {staged_wrong}  ({pct(staged_wrong, staged_correct + staged_wrong)} false-positive rate)")

    if rejection_reasons:
        print(f"\n  Rejection reasons (most common patterns):")
        from collections import Counter
        for reason, count in Counter(rejection_reasons).most_common(10):
            print(f"    {count:3}×  {reason}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    if "--stats" in args:
        stats_mode()
        return

    include_labeled = "--all" in args

    date_filter: str | None = None
    if "--date" in args:
        i = args.index("--date")
        if i + 1 < len(args):
            date_filter = args[i + 1]

    review_mode(date_filter, include_labeled)


if __name__ == "__main__":
    main()
