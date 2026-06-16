# Bee → Second Brain: Handoff Doc

**Built:** June 1, 2026  
**Status:** Live and running automatically  
**One-liner:** Your Bee wearable automatically captures conversations, extracts what you committed to, syncs tasks to Todoist, marks things done when you mention completing them, and writes a structured daily journal to Obsidian — with zero manual work required.

---

## What This Does (The Full Loop)

1. **You wear your Bee device** → it records ambient audio, transcribes it, and generates conversation summaries with extracted action items
2. **Every 30 minutes**, an agent reads new conversations, asks Claude which existing tasks were completed, and stages evidence-grounded candidate tasks in `.bee_candidates/` instead of immediately creating Todoist items
3. **3x per day** (11:15am, 4:15pm, 10:30pm), a daily reducer reads the day's staged candidates, full-day Bee context, and open Todoist tasks before creating only durable, non-duplicate tasks
4. **You get a macOS notification** if the 30-minute agent marked tasks done or staged candidates
5. **3x per day** (8am, 2pm, 10pm), a script rewrites your Obsidian daily note with all conversations from the day, open Bee todos, stale items that haven't been done yet, and a master action item list. It does **not** push Bee summary action items to Todoist by default; set `BEE_OBSIDIAN_PUSH_ACTIONS=1` only if you want that legacy behavior.
6. **Todoist is the source of truth** — approved tasks live there, you can check them off from your phone, Siri, or anywhere

---

## Day-to-Day Experience

You do nothing. As long as:
- Your **Bee device** is charged and on you
- Your **Mac is on**

Everything else is automatic. The system runs in the background 24/7.

If you marked something done in Todoist from your phone at 2pm, by 2:30pm the agent will see that task is gone from the open list and won't surface it as stale anymore. By 10pm, your Obsidian daily note will reflect the clean state.

---

## Files and Where They Live

| File | Location | Purpose |
|------|----------|---------|
| `bee_to_obsidian.py` | `~/Documents/GitHub/knowledge/` | Writes daily Obsidian notes |
| `bee_agent.py` | `~/Documents/GitHub/knowledge/` | Accountability agent |
| `bee_daily_reducer.py` | `~/Documents/GitHub/knowledge/` | Context-aware staged candidate reducer |
| `.env` | `~/Documents/GitHub/knowledge/` | Todoist API token (gitignored) |
| `.bee_agent_state.json` | `~/Documents/GitHub/knowledge/` | Tracks last-processed conversation |
| `.bee_candidates/` | `~/Documents/GitHub/knowledge/` | JSONL staged candidate tasks by date |
| `.bee_decisions/` | `~/Documents/GitHub/knowledge/` | Daily reducer decisions and audit trail |
| Daily notes | `~/Documents/Obsidian Vault/Daily Notes/YYYY-MM-DD.md` | Output |
| `com.michael.bee-proxy.plist` | `~/Library/LaunchAgents/` | Keeps bee proxy alive |
| `com.michael.bee-to-obsidian.plist` | `~/Library/LaunchAgents/` | Schedules note writer |
| `com.michael.bee-agent.plist` | `~/Library/LaunchAgents/` | Schedules accountability agent |
| `com.michael.bee-daily-reducer.plist` | `~/Library/LaunchAgents/` | Schedules context-aware Todoist creation |
| `bee-proxy.log` | `~/Library/Logs/` | Proxy output |
| `bee-to-obsidian.log` | `~/Library/Logs/` | Note writer output |
| `bee-agent.log` | `~/Library/Logs/` | Agent output |
| `bee-daily-reducer.log` | `~/Library/Logs/` | Reducer output |

---

## How to Check If It's Working

```bash
# Are all three agents running?
launchctl list | grep com.michael.bee

# Is the proxy healthy?
curl http://127.0.0.1:8787/v1/todos

# What did the agent do last?
tail -50 ~/Library/Logs/bee-agent.log

# What did the note writer do last?
tail -20 ~/Library/Logs/bee-to-obsidian.log

# What did the context reducer do last?
tail -50 ~/Library/Logs/bee-daily-reducer.log
```

---

## If Something Breaks

**Proxy is down** (most common failure):
```bash
launchctl start com.michael.bee-proxy
# wait 3 seconds, then check:
curl http://127.0.0.1:8787/v1/todos
```

**Agent logged errors but nothing was processed:**
```bash
# Delete state file to force re-processing from 24h ago
rm ~/Documents/GitHub/knowledge/.bee_agent_state.json
# Manually trigger a run:
python3 ~/Documents/GitHub/knowledge/bee_agent.py
```

**Daily note is stale:**
```bash
python3 ~/Documents/GitHub/knowledge/bee_to_obsidian.py

**Review/apply staged candidates manually:**
```bash
python3 ~/Documents/GitHub/knowledge/bee_daily_reducer.py --dry-run
python3 ~/Documents/GitHub/knowledge/bee_daily_reducer.py
```
```

**Backfill historical notes:**
```bash
python3 ~/Documents/GitHub/knowledge/bee_to_obsidian.py --backfill
# Use --force to overwrite existing notes
python3 ~/Documents/GitHub/knowledge/bee_to_obsidian.py --backfill --force
```

**Reload all launchd agents after editing plists:**
```bash
launchctl unload ~/Library/LaunchAgents/com.michael.bee-proxy.plist
launchctl unload ~/Library/LaunchAgents/com.michael.bee-to-obsidian.plist
launchctl unload ~/Library/LaunchAgents/com.michael.bee-agent.plist
launchctl unload ~/Library/LaunchAgents/com.michael.bee-daily-reducer.plist
launchctl load ~/Library/LaunchAgents/com.michael.bee-proxy.plist
launchctl load ~/Library/LaunchAgents/com.michael.bee-to-obsidian.plist
launchctl load ~/Library/LaunchAgents/com.michael.bee-agent.plist
launchctl load ~/Library/LaunchAgents/com.michael.bee-daily-reducer.plist
```

---

## What's in the Daily Note

```
# Daily Note — 2026-06-01

## Open To-Dos
- [ ] Look up Yankees tickets for the Mariners in early July

## Stale From Yesterday _(still open in Todoist)_
- [ ] Research daycare options in LIC area

## Today's Conversations
### 01:31 — Podcast summarizer idea
[full Bee summary]

### 03:25 — Newborn care and household chaos
[full Bee summary]

## Today's Action Items
- [ ] Feed Julian within the next 30 minutes or so
- [ ] Consider researching DIY digital photo frame setup
```

---

## Todoist Integration

- All Bee-captured tasks have the label **`bee`** in Todoist
- The agent only manages `bee`-labeled tasks — your other Todoist projects are untouched
- New Todoist tasks are created only from transcript-grounded, high-confidence commitments with evidence quotes; Bee-generated summaries are context, not source of truth
- New task creation is staged and reduced against full-day context by default. Set `BEE_AGENT_CREATE_TASKS_IMMEDIATELY=1` only if you want the legacy direct-write behavior.
- Reducer safeguards default to: max 5 creations per run, max 10 creations per day, and no creation when open `bee` tasks are at or above 150. Override with `BEE_REDUCER_MAX_CREATE_PER_RUN`, `BEE_REDUCER_MAX_CREATE_PER_DAY`, and `BEE_REDUCER_MAX_OPEN_TASKS`.
- Medium/low-confidence reducer outputs are kept for review in `.bee_decisions/YYYY-MM-DD.json`; they are not auto-created.
- Deduplication is case-insensitive; the same task won't be created twice
- Completing a `bee` task anywhere (phone, web, Siri) removes it from the stale list

---

## Known Limitations

1. **Daily notes are fully overwritten** on each run — if you write something manually in the note, the next scheduled run will wipe it
2. **Completion detection is conservative** — the agent only marks tasks done when there's clear verbal evidence; it will miss subtle completions
3. **Log files don't rotate** — `~/Library/Logs/bee-agent.log` grows indefinitely (low priority but worth a cleanup occasionally)
4. **Bee proxy is the single point of failure** — if the proxy crashes and launchd can't restart it for any reason, both scripts silently fail until fixed
5. **`claude -p` uses your Claude subscription** — the agent makes roughly 10–15 Claude calls per day on an average day, well within any subscription tier

---

## What's Next (Planned Improvements)

- [ ] **Intent classification** — tag utterances as `decision`, `commitment`, `idea`, `note-to-self` beyond just action items
- [ ] **Smarter cross-day dedup** — consolidate the same task appearing across multiple days rather than creating multiple Todoist entries
- [ ] **Note merge logic** — preserve manually written sections in daily notes instead of full overwrite
- [ ] **Direct Bee API** — replace the local proxy with direct Bearer token calls when productionizing
- [ ] **Log rotation** — cap log files at a reasonable size

---

## Todoist MCP (Interactive Claude Use)

The Todoist MCP is connected to Claude Code at `https://ai.todoist.net/mcp`. In any new Claude Code conversation you can ask:
- *"What bee tasks are still open?"*
- *"Mark 'Research daycares' as done"*
- *"Add a task to follow up with Underdog Fantasy by Friday"*
