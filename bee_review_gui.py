#!/usr/bin/env python3
"""
bee_review_gui.py — Browser-based GUI for reviewing Bee agent task extractions.

Starts a local HTTP server and opens the review UI in your browser.
Labels are saved to .bee_labels/ as JSONL (same format as bee_review.py CLI).

Usage:
    python3 bee_review_gui.py                   # serves on http://localhost:8788
    python3 bee_review_gui.py --port 9000       # custom port
    python3 bee_review_gui.py --all             # include already-labeled extractions
    python3 bee_review_gui.py --date 2026-06-24 # limit to one date
"""
from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

TRANSCRIPT_DIR = Path(__file__).parent / ".bee_transcripts"
LABEL_DIR      = Path(__file__).parent / ".bee_labels"
TODOIST_BASE   = "https://api.todoist.com/api/v1"
BEE_LABEL      = "bee"

ACTION_ITEMS_RE = __import__("re").compile(r"## Action Items\s*\n+\s*((?:[-*].+\n?)+)", __import__("re").IGNORECASE)
_RE             = __import__("re")


# ---------------------------------------------------------------------------
# Todoist helpers (for auto-closing outdated staged tasks)
# ---------------------------------------------------------------------------

def load_env(key: str) -> str | None:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                return val or None
    return os.environ.get(key) or None


def _item_key(s: str) -> str:
    return _RE.sub(r"\s+", " ", s.lower().strip()).rstrip(".,;")


def _todoist_get(path: str, token: str) -> dict | None:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError
    req = Request(
        f"{TODOIST_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception:
        return None


def _todoist_post(path: str, token: str) -> bool:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError
    req = Request(
        f"{TODOIST_BASE}{path}",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def complete_bee_task_by_text(task_text: str) -> bool:
    """Find an open bee-labeled Todoist task by content and close it. Returns True if closed."""
    token = load_env("TODOIST_API_TOKEN")
    if not token:
        return False
    target_key = _item_key(task_text)
    cursor = None
    while True:
        path = f"/tasks?label={BEE_LABEL}" + (f"&cursor={cursor}" if cursor else "")
        data = _todoist_get(path, token)
        if not data:
            break
        for task in data.get("results", []):
            if _item_key(task.get("content", "")) == target_key:
                return _todoist_post(f"/tasks/{task['id']}/close", token)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return False


def _remove_from_jsonl(path: Path, task_text: str) -> bool:
    """Rewrite a JSONL file omitting any candidate whose task matches task_text."""
    target_key = _item_key(task_text)
    lines = path.read_text().splitlines()
    kept, removed = [], False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if _item_key(r.get("task", "")) == target_key:
                removed = True
                continue
        except Exception:
            pass
        kept.append(line)
    if removed:
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return removed


CANDIDATE_DIR = Path(__file__).parent / ".bee_candidates"


def remove_from_candidates(task_text: str, date_str: str) -> bool:
    """Remove a task from the candidates file. Checks the conversation date first,
    then scans all candidate files as a fallback."""
    if not CANDIDATE_DIR.exists():
        return False
    primary = CANDIDATE_DIR / f"{date_str}.jsonl"
    if primary.exists() and _remove_from_jsonl(primary, task_text):
        return True
    for path in sorted(CANDIDATE_DIR.glob("*.jsonl")):
        if path == primary:
            continue
        if _remove_from_jsonl(path, task_text):
            return True
    return False


def handle_outdated(task_text: str, date_str: str) -> dict:
    """Close in Todoist if present; otherwise remove from candidates."""
    if complete_bee_task_by_text(task_text):
        return {"todoist_completed": True, "candidate_removed": False}
    candidate_removed = remove_from_candidates(task_text, date_str)
    return {"todoist_completed": False, "candidate_removed": candidate_removed}


# ---------------------------------------------------------------------------
# Data layer (same logic as bee_review.py)
# ---------------------------------------------------------------------------

def extract_action_items(summary: str) -> list[str]:
    m = ACTION_ITEMS_RE.search(summary or "")
    if not m:
        return []
    return [ln.strip(" -*\t").lstrip("[ ]").strip()
            for ln in m.group(1).splitlines() if ln.strip(" -*\t")]


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


def load_all_labels() -> dict[str, dict]:
    """Return {label_key: label_record} for every saved label."""
    labels: dict[str, dict] = {}
    if not LABEL_DIR.exists():
        return labels
    for path in LABEL_DIR.glob("*.jsonl"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    key = f"{r['conversation_id']}::{r['task']}"
                    labels[key] = r
                except Exception:
                    pass
    return labels


def append_label(date_str: str, record: dict) -> None:
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    path = LABEL_DIR / f"{date_str}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_data(date_filter: str | None, include_labeled: bool) -> dict:
    all_records  = load_transcripts(date_filter)
    all_labels   = load_all_labels()
    conversations: list[dict] = []

    for date_str, conv in all_records:
        conv_id  = conv.get("conversation_id", 0)
        analysis = conv.get("analysis") or {}
        staged   = set(conv.get("staged_tasks", []))
        summary  = conv.get("bee_summary", "")
        tasks: list[dict] = []

        for item in analysis.get("new_tasks", []):
            task       = (item.get("task", "") if isinstance(item, dict) else str(item)).strip()
            evidence   = (item.get("evidence_quote", "") if isinstance(item, dict) else "").strip()
            confidence = (item.get("confidence", "") if isinstance(item, dict) else "").strip()
            reject_if  = (item.get("reject_if", "") if isinstance(item, dict) else "").strip()
            if not task:
                continue
            label_key = f"{conv_id}::{task}"
            existing  = all_labels.get(label_key)
            if existing and not include_labeled:
                continue
            tasks.append({
                "task":       task,
                "evidence":   evidence,
                "confidence": confidence,
                "reject_if":  reject_if,
                "staged":     task in staged,
                "type":       "new_task",
                "label_key":  label_key,
                "label":      existing,
            })

        for item in analysis.get("completed", []):
            task       = (item.get("task", "") if isinstance(item, dict) else str(item)).strip()
            evidence   = (item.get("evidence_quote", "") if isinstance(item, dict) else "").strip()
            confidence = (item.get("confidence", "") if isinstance(item, dict) else "").strip()
            if not task:
                continue
            task_key  = f"[completed] {task}"
            label_key = f"{conv_id}::{task_key}"
            existing  = all_labels.get(label_key)
            if existing and not include_labeled:
                continue
            tasks.append({
                "task":       task_key,
                "evidence":   evidence,
                "confidence": confidence,
                "reject_if":  "",
                "staged":     False,
                "type":       "completion",
                "label_key":  label_key,
                "label":      existing,
            })

        if not tasks:
            continue

        conversations.append({
            "date_str":    date_str,
            "conv_id":     conv_id,
            "time":        conv.get("conversation_time", "")[:16],
            "short":       conv.get("short_summary", f"conv {conv_id}"),
            "reasoning":   (analysis.get("reasoning") or "").strip(),
            "action_items": extract_action_items(summary),
            "tasks":       tasks,
        })

    total   = sum(len(c["tasks"]) for c in conversations)
    labeled = sum(1 for c in conversations for t in c["tasks"] if t["label"])
    return {"conversations": conversations, "total": total, "labeled": labeled}


def build_stats() -> dict:
    total = approved = rejected = edited = outdated = staged_correct = staged_wrong = 0
    reasons: list[str] = []

    if LABEL_DIR.exists():
        for path in LABEL_DIR.glob("*.jsonl"):
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r      = json.loads(line)
                    action = r.get("action", "skip")
                    if action == "skip":
                        continue
                    total += 1
                    staged = r.get("was_staged", False)
                    if action == "approve":
                        approved += 1
                        if staged:
                            staged_correct += 1
                    elif action == "reject":
                        rejected += 1
                        if staged:
                            staged_wrong += 1
                        reason = r.get("rejection_reason", "").strip()
                        if reason:
                            reasons.append(reason)
                    elif action == "edit":
                        edited += 1
                    elif action == "outdated":
                        outdated += 1
                except Exception:
                    pass

    from collections import Counter
    top_reasons = [{"reason": r, "count": c}
                   for r, c in Counter(reasons).most_common(10)]
    return {
        "total": total, "approved": approved, "rejected": rejected,
        "edited": edited, "outdated": outdated,
        "staged_correct": staged_correct, "staged_wrong": staged_wrong,
        "top_reasons": top_reasons,
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    config: dict = {}  # set before server starts

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # suppress access logs

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/":
            body = HTML_APP.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/data":
            data = build_data(
                self.config.get("date_filter"),
                self.config.get("include_labeled", False),
            )
            self.send_json(data)

        elif path == "/api/stats":
            self.send_json(build_stats())

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/label":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                payload  = json.loads(body)
                date_str = payload.get("date_str", "unknown")
                record   = payload.get("record", {})
                record["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                todoist_completed = False
                candidate_removed = False
                if record.get("action") == "outdated" and record.get("was_staged"):
                    result = handle_outdated(record.get("task", ""), date_str)
                    todoist_completed = result["todoist_completed"]
                    candidate_removed = result["candidate_removed"]
                    record["todoist_completed"] = todoist_completed
                    record["candidate_removed"] = candidate_removed
                append_label(date_str, record)
                self.send_json({"ok": True, "todoist_completed": todoist_completed, "candidate_removed": candidate_removed})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Embedded single-page app
# ---------------------------------------------------------------------------

HTML_APP = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bee Review</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #f5f5f7;
    --surface:   #ffffff;
    --border:    #d2d2d7;
    --text:      #1d1d1f;
    --muted:     #6e6e73;
    --accent:    #0071e3;
    --green:     #34c759;
    --red:       #ff3b30;
    --orange:    #ff9500;
    --yellow:    #ffcc00;
    --sidebar-w: 300px;
    --header-h:  56px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px;
    color: var(--text);
  }

  body { background: var(--bg); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

  /* ── Header ── */
  #header {
    height: var(--header-h);
    background: rgba(255,255,255,.85);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 20px;
    gap: 16px;
    flex-shrink: 0;
    position: sticky; top: 0; z-index: 10;
  }
  #header h1 { font-size: 17px; font-weight: 600; }
  #progress-wrap { flex: 1; background: var(--border); border-radius: 4px; height: 6px; overflow: hidden; }
  #progress-bar  { height: 100%; background: var(--accent); border-radius: 4px; transition: width .3s; }
  #progress-text { font-size: 12px; color: var(--muted); white-space: nowrap; }
  #stats-btn {
    font-size: 13px; color: var(--accent); cursor: pointer; background: none;
    border: none; padding: 4px 8px; border-radius: 6px; font-family: inherit;
  }
  #stats-btn:hover { background: #e8f0fb; }

  /* ── Layout ── */
  #main { display: flex; flex: 1; overflow: hidden; }

  /* ── Sidebar ── */
  #sidebar {
    width: var(--sidebar-w);
    border-right: 1px solid var(--border);
    background: var(--surface);
    overflow-y: auto;
    flex-shrink: 0;
  }
  .conv-item {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 4px;
    transition: background .12s;
  }
  .conv-item:hover    { background: #f0f0f5; }
  .conv-item.active   { background: #e8f0fb; }
  .conv-item .conv-time  { font-size: 11px; color: var(--muted); }
  .conv-item .conv-title { font-size: 13px; font-weight: 500; line-height: 1.3; }
  .conv-item .conv-meta  { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
  .badge {
    font-size: 11px; font-weight: 600; border-radius: 10px;
    padding: 1px 7px; white-space: nowrap;
  }
  .badge-total    { background: #e8e8ed; color: var(--muted); }
  .badge-done     { background: #e3f9e9; color: #1a7f37; }
  .badge-staged   { background: #fff3cd; color: #7d5300; }

  /* ── Detail panel ── */
  #detail {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  #empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: var(--muted);
    gap: 8px;
    text-align: center;
  }
  #empty-state .emoji { font-size: 48px; }

  .detail-header { display: flex; flex-direction: column; gap: 6px; }
  .detail-time   { font-size: 12px; color: var(--muted); }
  .detail-title  { font-size: 20px; font-weight: 600; line-height: 1.3; }
  .detail-reasoning {
    font-size: 13px; color: var(--muted); background: var(--bg);
    border-radius: 8px; padding: 10px 14px; border-left: 3px solid var(--border);
  }

  .section-label {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted); margin-bottom: 6px;
  }

  .action-items { display: flex; flex-wrap: wrap; gap: 6px; }
  .ai-chip {
    font-size: 12px; background: #fff3cd; color: #7d5300;
    border: 1px solid #f0c040; border-radius: 12px; padding: 3px 10px;
  }

  /* ── Task cards ── */
  .task-card {
    background: var(--surface);
    border: 1.5px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    transition: border-color .15s, box-shadow .15s;
  }
  .task-card.approved { border-color: var(--green); }
  .task-card.rejected { border-color: var(--red); }
  .task-card.edited   { border-color: var(--orange); }

  .task-top { display: flex; align-items: flex-start; gap: 10px; }
  .task-text { font-size: 15px; font-weight: 500; flex: 1; line-height: 1.4; }

  .chip {
    font-size: 11px; font-weight: 600; border-radius: 10px;
    padding: 2px 8px; white-space: nowrap; flex-shrink: 0;
  }
  .chip-staged   { background: #fff3cd; color: #7d5300; }
  .chip-filtered { background: #f0f0f5; color: var(--muted); }
  .chip-completion { background: #e3f9e9; color: #1a7f37; }

  .task-evidence { font-size: 12px; color: var(--muted); font-style: italic; }
  .task-reject-if { font-size: 12px; color: var(--red); }

  .task-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .btn {
    border: none; border-radius: 8px; padding: 7px 16px; font-size: 13px;
    font-family: inherit; font-weight: 500; cursor: pointer;
    transition: opacity .12s, transform .1s;
  }
  .btn:active { transform: scale(.96); }
  .btn-approve   { background: var(--green);  color: #fff; }
  .btn-reject    { background: var(--red);    color: #fff; }
  .btn-edit      { background: var(--orange); color: #fff; }
  .btn-outdated  { background: #8b5cf6;       color: #fff; }
  .btn-skip      { background: var(--bg);     color: var(--muted); border: 1px solid var(--border); }
  .task-card.outdated { border-color: #8b5cf6; }
  .btn-undo    { background: var(--bg);     color: var(--accent); border: 1px solid var(--border); font-size: 12px; padding: 6px 12px; }
  .btn:hover   { opacity: .85; }

  /* inline inputs for reject/edit */
  .task-input-row { display: flex; gap: 8px; align-items: center; }
  .task-input {
    flex: 1; border: 1.5px solid var(--border); border-radius: 8px;
    padding: 7px 12px; font-size: 13px; font-family: inherit;
    outline: none; background: var(--bg);
  }
  .task-input:focus { border-color: var(--accent); background: #fff; }
  .btn-save { background: var(--accent); color: #fff; }

  /* status label shown after review */
  .task-status { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .status-dot.approved  { background: var(--green); }
  .status-dot.rejected  { background: var(--red); }
  .status-dot.edited    { background: var(--orange); }
  .status-dot.outdated  { background: #8b5cf6; }
  .status-dot.skip      { background: var(--muted); }
  .task-card.skip       { border-color: var(--border); opacity: .5; }
  .status-label { font-weight: 500; }
  .status-detail { color: var(--muted); }

  /* ── Stats modal ── */
  #modal-bg {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4);
    z-index: 100; align-items: center; justify-content: center;
  }
  #modal-bg.open { display: flex; }
  #modal {
    background: var(--surface); border-radius: 16px; padding: 28px;
    width: 480px; max-width: 90vw; box-shadow: 0 8px 40px rgba(0,0,0,.18);
    display: flex; flex-direction: column; gap: 16px;
  }
  #modal h2 { font-size: 18px; font-weight: 600; }
  .stat-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--muted); font-size: 13px; }
  .stat-value { font-weight: 600; font-size: 15px; }
  .reasons-list { display: flex; flex-direction: column; gap: 6px; }
  .reason-row { display: flex; gap: 10px; align-items: baseline; font-size: 13px; }
  .reason-count { font-weight: 600; color: var(--red); min-width: 28px; text-align: right; }
  #modal-close { align-self: flex-end; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 7px 16px; font-size: 13px; font-family: inherit; cursor: pointer; }
</style>
</head>
<body>

<div id="header">
  <h1>🐝 Bee Review</h1>
  <div id="progress-wrap"><div id="progress-bar" style="width:0%"></div></div>
  <span id="progress-text">Loading…</span>
  <button id="stats-btn" onclick="openStats()">Stats</button>
</div>

<div id="main">
  <div id="sidebar"></div>
  <div id="detail">
    <div id="empty-state">
      <div class="emoji">🐝</div>
      <div>Select a conversation to review</div>
    </div>
  </div>
</div>

<div id="modal-bg" onclick="closeStats(event)">
  <div id="modal">
    <h2>Label Stats</h2>
    <div id="stats-content"></div>
    <button id="modal-close" onclick="closeStats()">Close</button>
  </div>
</div>

<script>
let DATA = { conversations: [], total: 0, labeled: 0 };
let LABELS = {};   // label_key → {action, ...}
let activeIdx = null;

// ── Fetch & render ──────────────────────────────────────────────────────────

async function init() {
  const res  = await fetch('/api/data');
  DATA       = await res.json();

  // seed LABELS from server data
  for (const conv of DATA.conversations) {
    for (const t of conv.tasks) {
      if (t.label) LABELS[t.label_key] = t.label;
    }
  }

  renderSidebar();
  updateProgress();
}

function updateProgress() {
  let total = 0, done = 0;
  for (const conv of DATA.conversations) {
    for (const t of conv.tasks) {
      total++;
      const lbl = LABELS[t.label_key];
      if (lbl && lbl.action !== 'skip') done++;
    }
  }
  const pct = total ? Math.round(100 * done / total) : 0;
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('progress-text').textContent = `${done} / ${total} reviewed`;
}

// ── Sidebar ─────────────────────────────────────────────────────────────────

function renderSidebar() {
  const sb = document.getElementById('sidebar');
  sb.innerHTML = '';
  DATA.conversations.forEach((conv, i) => {
    const total  = conv.tasks.length;
    const done   = conv.tasks.filter(t => {
      const l = LABELS[t.label_key];
      return l && l.action !== 'skip';
    }).length;
    const staged = conv.tasks.filter(t => t.staged).length;

    const el = document.createElement('div');
    el.className = 'conv-item' + (i === activeIdx ? ' active' : '');
    el.innerHTML = `
      <div class="conv-time">${conv.time || conv.date_str}</div>
      <div class="conv-title">${esc(conv.short)}</div>
      <div class="conv-meta">
        <span class="badge badge-total">${done}/${total}</span>
        ${staged ? `<span class="badge badge-staged">${staged} staged</span>` : ''}
        ${done === total && total > 0 ? `<span class="badge badge-done">✓ done</span>` : ''}
      </div>
    `;
    el.onclick = () => selectConv(i);
    sb.appendChild(el);
  });
}

function selectConv(i) {
  activeIdx = i;
  renderSidebar();
  renderDetail(DATA.conversations[i]);
}

// ── Detail panel ─────────────────────────────────────────────────────────────

function renderDetail(conv) {
  const panel = document.getElementById('detail');
  panel.innerHTML = '';

  // Header
  const hdr = div('detail-header');
  hdr.innerHTML = `
    <div class="detail-time">${conv.time || conv.date_str}</div>
    <div class="detail-title">${esc(conv.short)}</div>
    ${conv.reasoning ? `<div class="detail-reasoning">Claude: ${esc(conv.reasoning)}</div>` : ''}
  `;
  panel.appendChild(hdr);

  // Bee action items
  if (conv.action_items && conv.action_items.length) {
    const sec = div();
    sec.innerHTML = `<div class="section-label">Bee Action Items</div>`;
    const chips = div('action-items');
    conv.action_items.forEach(ai => {
      const c = document.createElement('span');
      c.className = 'ai-chip';
      c.textContent = ai;
      chips.appendChild(c);
    });
    sec.appendChild(chips);
    panel.appendChild(sec);
  }

  // Task cards
  const tasksSec = div();
  tasksSec.innerHTML = `<div class="section-label">Proposed Tasks (${conv.tasks.length})</div>`;
  conv.tasks.forEach(task => {
    tasksSec.appendChild(buildTaskCard(task, conv.date_str, conv.conv_id));
  });
  panel.appendChild(tasksSec);
}

function buildTaskCard(task, date_str, conv_id) {
  const label   = LABELS[task.label_key];
  const action  = label?.action;
  const cardEl  = div('task-card' + (action && action !== 'skip' ? ' ' + action : ''));
  cardEl.id     = 'card-' + CSS.escape(task.label_key);

  // chip
  let chipHtml = '';
  if (task.type === 'completion') {
    chipHtml = `<span class="chip chip-completion">completion</span>`;
  } else if (task.staged) {
    chipHtml = `<span class="chip chip-staged">candidate</span>`;
  } else {
    chipHtml = `<span class="chip chip-filtered">filtered</span>`;
  }

  cardEl.innerHTML = `
    <div class="task-top">
      <div class="task-text">${esc(task.task)}</div>
      ${chipHtml}
    </div>
    ${task.evidence ? `<div class="task-evidence">"${esc(task.evidence)}"</div>` : ''}
    ${task.reject_if ? `<div class="task-reject-if">⚠ ${esc(task.reject_if)}</div>` : ''}
    <div class="task-actions-wrap"></div>
  `;

  renderCardActions(cardEl, task, date_str, conv_id, label);
  return cardEl;
}

function renderCardActions(cardEl, task, date_str, conv_id, label) {
  const wrap  = cardEl.querySelector('.task-actions-wrap');
  wrap.innerHTML = '';
  const action = label?.action;

  if (action) {
    // Already labeled — show status + undo
    const row = div('task-status');
    const dot = div('status-dot ' + action);
    const lbl = document.createElement('span');
    lbl.className = 'status-label';
    let detail = '';
    if (action === 'approve') { lbl.textContent = 'Approved'; }
    else if (action === 'skip')     { lbl.textContent = 'Skipped'; detail = 'won\'t appear again'; }
    else if (action === 'reject')   { lbl.textContent = 'Rejected'; detail = label.rejection_reason || ''; }
    else if (action === 'edit')     { lbl.textContent = 'Edited';   detail = '→ ' + (label.edited_text || ''); }
    else if (action === 'outdated') {
      lbl.textContent = 'Outdated';
      if (label.todoist_completed)    detail = '✓ closed in Todoist';
      else if (label.candidate_removed) detail = '✓ removed from candidates';
      else if (label.was_staged)       detail = '⚠ not found';
    }
    const detEl = document.createElement('span');
    detEl.className = 'status-detail';
    detEl.textContent = detail;
    row.appendChild(dot);
    row.appendChild(lbl);
    row.appendChild(detEl);

    const undoBtn = document.createElement('button');
    undoBtn.className = 'btn btn-undo';
    undoBtn.textContent = 'Undo';
    undoBtn.onclick = () => {
      delete LABELS[task.label_key];
      cardEl.className = 'task-card';
      renderCardActions(cardEl, task, date_str, conv_id, null);
      updateProgress(); renderSidebar();
    };
    wrap.appendChild(row);
    wrap.appendChild(undoBtn);
    return;
  }

  // Not yet labeled — show action buttons
  const actions = div('task-actions');

  const approveBtn = btn('Approve ✓', 'btn-approve', async () => {
    await saveLabel(date_str, conv_id, task, { action: 'approve' });
  });
  const rejectBtn = btn('Reject ✗', 'btn-reject', () => {
    showInput(wrap, task, date_str, conv_id, 'reject', cardEl);
  });
  const editBtn = btn('Edit ✎', 'btn-edit', () => {
    showInput(wrap, task, date_str, conv_id, 'edit', cardEl);
  });
  const outdatedBtn = btn('Outdated', 'btn-outdated', async () => {
    await saveLabel(date_str, conv_id, task, { action: 'outdated' });
  });
  outdatedBtn.title = task.staged
    ? 'Task was correctly extracted but is no longer needed — will close in Todoist or remove from candidates'
    : 'Task was correctly extracted but is no longer needed';
  if (!task.staged) outdatedBtn.style.opacity = '.6';

  const skipBtn = btn('Skip', 'btn-skip', async () => {
    await saveLabel(date_str, conv_id, task, { action: 'skip' });
  });

  [approveBtn, rejectBtn, editBtn, outdatedBtn, skipBtn].forEach(b => actions.appendChild(b));
  wrap.appendChild(actions);
}

function showInput(wrap, task, date_str, conv_id, mode, cardEl) {
  wrap.innerHTML = '';
  const row = div('task-input-row');
  const input = document.createElement('input');
  input.className = 'task-input';
  input.placeholder = mode === 'reject' ? 'Rejection reason…' : task.task;
  if (mode === 'edit') input.value = task.task;
  input.focus();
  const saveBtn = btn('Save', 'btn btn-save', async () => {
    const val = input.value.trim();
    const extra = mode === 'reject'
      ? { rejection_reason: val }
      : { edited_text: val || task.task };
    await saveLabel(date_str, conv_id, task, { action: mode, ...extra });
  });
  const cancelBtn = btn('Cancel', 'btn btn-skip', () => {
    renderCardActions(cardEl, task, date_str, conv_id, null);
  });
  row.appendChild(input);
  row.appendChild(saveBtn);
  row.appendChild(cancelBtn);
  wrap.appendChild(row);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') saveBtn.click();
    if (e.key === 'Escape') cancelBtn.click();
  });
}

async function saveLabel(date_str, conv_id, task, extra) {
  const record = {
    conversation_id:   conv_id,
    task:              task.task,
    task_type:         task.type,
    evidence_quote:    task.evidence,
    confidence_claimed: task.confidence,
    was_staged:        task.staged,
    ...extra,
  };
  const resp = await fetch('/api/label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ date_str, record }),
  });
  const respData = await resp.json();
  if (respData.todoist_completed !== undefined) {
    record.todoist_completed = respData.todoist_completed;
  }
  if (respData.candidate_removed !== undefined) {
    record.candidate_removed = respData.candidate_removed;
  }
  LABELS[task.label_key] = record;

  // Re-render the card
  const cardId  = 'card-' + CSS.escape(task.label_key);
  const cardEl  = document.getElementById(cardId);
  if (cardEl) {
    cardEl.className = 'task-card ' + record.action;
    renderCardActions(cardEl, task, date_str, conv_id, record);
  }
  updateProgress();
  renderSidebar();
}

// ── Stats modal ──────────────────────────────────────────────────────────────

async function openStats() {
  const res   = await fetch('/api/stats');
  const stats = await res.json();
  const el    = document.getElementById('stats-content');
  const pct   = n => stats.total ? Math.round(100 * n / stats.total) + '%' : '—';

  let html = `
    <div class="stat-row"><span class="stat-label">Total labeled</span><span class="stat-value">${stats.total}</span></div>
    <div class="stat-row"><span class="stat-label">Approved</span><span class="stat-value" style="color:var(--green)">${stats.approved} (${pct(stats.approved)})</span></div>
    <div class="stat-row"><span class="stat-label">Rejected</span><span class="stat-value" style="color:var(--red)">${stats.rejected} (${pct(stats.rejected)})</span></div>
    <div class="stat-row"><span class="stat-label">Edited</span><span class="stat-value" style="color:var(--orange)">${stats.edited} (${pct(stats.edited)})</span></div>
    <div class="stat-row"><span class="stat-label">Outdated</span><span class="stat-value" style="color:#8b5cf6">${stats.outdated} (${pct(stats.outdated)})</span></div>
  `;
  if (stats.staged_correct + stats.staged_wrong > 0) {
    const fp = Math.round(100 * stats.staged_wrong / (stats.staged_correct + stats.staged_wrong));
    html += `
      <div style="margin-top:12px" class="section-label">Tasks that reached Todoist</div>
      <div class="stat-row"><span class="stat-label">Correct</span><span class="stat-value" style="color:var(--green)">${stats.staged_correct}</span></div>
      <div class="stat-row"><span class="stat-label">False positives</span><span class="stat-value" style="color:var(--red)">${stats.staged_wrong} (${fp}%)</span></div>
    `;
  }
  if (stats.top_reasons && stats.top_reasons.length) {
    html += `<div style="margin-top:12px" class="section-label">Top rejection reasons</div><div class="reasons-list">`;
    for (const r of stats.top_reasons) {
      html += `<div class="reason-row"><span class="reason-count">${r.count}×</span><span>${esc(r.reason)}</span></div>`;
    }
    html += '</div>';
  }
  el.innerHTML = html;
  document.getElementById('modal-bg').classList.add('open');
}

function closeStats(e) {
  if (!e || e.target === document.getElementById('modal-bg')) {
    document.getElementById('modal-bg').classList.remove('open');
  }
}

// ── Utils ────────────────────────────────────────────────────────────────────

function div(cls) {
  const el = document.createElement('div');
  if (cls) el.className = cls;
  return el;
}

function btn(label, cls, onclick) {
  const el = document.createElement('button');
  el.className = 'btn ' + cls;
  el.textContent = label;
  el.onclick = onclick;
  return el;
}

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    port = 8788
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            port = int(args[i + 1])

    Handler.config = {
        "include_labeled": "--all" in args,
        "date_filter": None,
    }
    if "--date" in args:
        i = args.index("--date")
        if i + 1 < len(args):
            Handler.config["date_filter"] = args[i + 1]

    server = HTTPServer(("127.0.0.1", port), Handler)
    url    = f"http://localhost:{port}"
    print(f"Bee Review  →  {url}")
    print("Ctrl-C to stop\n")

    threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
