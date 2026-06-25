# Bee → Second Brain (Python, local-first)

An ambient capture pipeline that turns conversations from a Bee AI wearable into a
structured Obsidian daily journal and an autonomous, evidence-grounded task manager.
Runs entirely locally via `launchd` — **no model API bill**: it drives the local
`claude` CLI against a Claude subscription (`claude -p`).

## Hard rules (these are the whole point of the project)
1. **Subscription, not API.** Reasoning shells out to `claude -p`. Never add a model
   API SDK or per-token billing path.
2. **Stdlib only.** Python 3.14, zero third-party dependencies. Do not add pip deps.
3. **Evidence or it didn't happen.** Tasks are only created/completed with a direct
   transcript quote (`evidence_quote` + `confidence`). Code-side guardrails
   (`should_create_task`) reject low-signal, transient, or proximity-inferred items —
   keep that gate; don't loosen it casually.
4. **Privacy: code-only repo.** All captured/derived data (transcripts, decisions,
   staged candidates, runtime state) lives in gitignored `.bee_*` dirs and **must
   never be committed**. Secrets (Todoist token, optional Telegram) live in `.env`.

## Pipeline
```
Bee wearable → Bee cloud API → local proxy (127.0.0.1:8787)
  bee_agent.py          every 30 min — read new conversations, ask Claude which open
                        tasks were completed + what new commitments were made; stage
                        evidence-backed candidates
  bee_daily_reducer.py  3×/day — review the day's candidates with full context; create
                        only durable, non-duplicate Todoist tasks
  bee_to_obsidian.py    3×/day — rewrite the Obsidian daily note (conversations, open
                        to-dos, stale items, master action-item list)
  bee_speech_profile.py weekly (Mon 9am) — analyze HOW Michael talks across the week's
                        transcripts; write an Obsidian profile note + a machine-readable
                        focus sidecar
```

## Speech feedback loop (closed loop across two scripts)
1. **Weekly** `bee_speech_profile.py` picks ONE focus pattern and writes it to
   `.bee_speech_profile/{week}.json` (the sidecar contract).
2. **Every ~2h** `bee_agent.py` reads that sidecar and runs one extra evidence-gated
   Claude pass over conversations buffered since the last pass, logging live instances
   of the focus pattern (with quotes) to `.bee_speech_flags/{date}.jsonl`. The agent
   itself runs every 30 min for task extraction, but the speech pass is throttled:
   each run buffers transcripts to `.bee_speech_pending.jsonl` and the detection call
   fires only once `SPEECH_DETECTION_INTERVAL_S` (2h) has elapsed
   (`.bee_speech_state.json` holds the clock). Surfaced in the run's notify.
3. **Next weekly run** ingests those flags so "changes_since_last_week" is graded against
   actual logged counts, not memory. The loop is silently inactive until a sidecar exists.

## Key paths
- Scripts: `bee_agent.py`, `bee_daily_reducer.py`, `bee_to_obsidian.py`, `bee_speech_profile.py`.
- State: `.bee_agent_state.json` (gitignored). Data: `.bee_candidates/`,
  `.bee_decisions/`, `.bee_transcripts/`, `.bee_speech_profile/`, `.bee_speech_flags/`
  (all gitignored).
- Scheduling: `launchd` plists (`launchctl`). Context: `README.md`, `HANDOFF.md`.

## Output targets
- **Todoist** (task sync) and **Obsidian** (daily note) — both configured via `.env`.
