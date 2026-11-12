# Bee → Second Brain

An ambient capture pipeline that turns conversations recorded by a [Bee](https://bee.computer) AI wearable into a structured daily journal and an autonomous, evidence-grounded task manager — running entirely on your own machine via `launchd`, with **no model API bill** (it drives the local `claude` CLI against a Claude subscription).

It's not just a journal. It closes the loop: it detects what you committed to, stages those commitments behind guardrails, notices when you mention finishing something, and keeps Todoist in sync — all in the background, zero manual work.

## How it works

```
Bee wearable
    │  audio → transcription → summaries
    ▼
Bee cloud API ──► local proxy (127.0.0.1:8787)
    │
    ├─► bee_agent.py        every 30 min — reads new conversations, asks Claude
    │                       which open tasks were completed and what new commitments
    │                       were made, stages evidence-backed candidates
    │
    ├─► bee_daily_reducer.py 3×/day — reviews the day's staged candidates with full
    │                       context, creates only durable, non-duplicate Todoist tasks
    │
    └─► bee_to_obsidian.py   3×/day — rewrites the Obsidian daily note: conversations,
                             open to-dos, stale items, and a master action-item list
```

Everything Claude concludes is **transcript-grounded**: tasks are only created or completed when there's a direct quote as evidence, with a layer of code-side guardrails (`should_create_task`) on top of the model output to reject low-signal, transient, or proximity-inferred items.

## Design notes

- **Stdlib only.** No third-party Python dependencies — just Python 3.14 and the `claude` CLI.
- **Subscription, not API.** `bee_agent.py` shells out to `claude -p`, so there's no per-token billing.
- **Evidence or it didn't happen.** Every staged task carries an `evidence_quote` and `confidence`; guardrails drop anything that can't justify itself.
- **Transcript archive.** Raw transcripts are stored alongside what the pipeline extracted from them (`.bee_transcripts/`), so the system's judgment can be audited and improved over time.

## Privacy

This repository contains **code only**. Everything the pipeline captures or derives from ambient audio — raw transcripts, extracted tasks, decisions, runtime state — is written to gitignored local directories and **never committed**. See [`.gitignore`](.gitignore). Secrets (Todoist token, optional Telegram bot credentials) live in a local `.env`.

## Setup

1. Install the Bee CLI and start the proxy: `bee proxy` (serves `http://127.0.0.1:8787`).
2. Create `.env` with `TODOIST_API_TOKEN=...` (and optionally `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for phone push).
3. Wire up the `launchd` agents (see [`HANDOFF.md`](HANDOFF.md) for the full operational guide, schedules, and management commands).

## License

MIT — see [`LICENSE`](LICENSE).

## Security boundaries

Open Bee Review using the URL printed at startup. Its fragment contains a per-process session token; the UI keeps it in sessionStorage and authenticates API calls. Restarting the server invalidates old browser sessions. The server remains loopback-only, rejects foreign origins, and validates label dates before any Todoist action.

Transcript reasoning now always uses the Claude subscription CLI with tools, MCP servers, hooks, skills, and session persistence disabled. Existing agy/Gemini/consensus selections fall back to this tool-free Claude path because the installed agy CLI has no verified equivalent tool-disable switch. This preserves subscription billing and the existing task evidence gates; multi-provider consensus is intentionally unavailable until it can be isolated safely. No provider or task-service API keys are inherited by the inference process.

Run python3 -m unittest test_security -v before release. Existing runtime data and .env stay outside version control.
