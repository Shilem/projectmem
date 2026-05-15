# Changelog

## 0.1.1

**First stable public release.** v0.0.6 was the intelligence layer; v0.1.1 is the polish + cross-client verification + cross-project memory wiring that makes it ready for general use. 46 lessons logged from soft-launch dogfooding and live verification, a 22-item batch polish-pass + 6 follow-up fixes, plus **end-to-end verification across all 4 major MCP clients** (Antigravity, Claude Desktop, Cursor, Codex) **and across 3 language ecosystems** (JavaScript, Python, Go) for cross-project memory.

This is the version we're comfortable putting on real PyPI. The CLI surface, MCP tool list, event schema, and `.projectmem/` layout are stable from here — future minor versions (0.2.0, etc.) will add features, not break the existing contract.

### Cross-project memory — wiring restored (the big one)

The "Cross-Project Knowledge" diagram on the landing page promised library gotchas to propagate machine-wide. Verification revealed it shipped half-built — three concrete gaps fixed before 0.1.1:

- **Auto-promote never fired on writes (L-043)** — `auto_promote_event` existed in `global_memory.py` but no write path called it. Every `record_attempt` / `add_decision` / `add_note` (MCP and CLI) silently skipped global promotion. Wired into `storage.append_event` so every write surface now promotes consistently. Plus word-boundary library matching (no more "gin" inside "imagineering") + stack-filter (a vite project mentioning "next" in plain English no longer creates a fake Next.js gotcha).
- **Library set was JS/Python-only (L-045)** — the hardcoded `PROMOTABLE_LIBRARIES` set covered React/Vue/Next/Vite/FastAPI/Django and not much else. Go, Rust, Java, Ruby, .NET, mobile — all silently dropped at promotion time. Replaced with a self-curating cache at `~/.projectmem/global/.promotable.json`: every library `detect_stack` ever sees in a manifest on this machine becomes promotable. A Go user's `gin` decisions now propagate exactly like a React user's `vite` ones.
- **Every library mention was treated as a gotcha (L-046)** — `add_decision("Use FastAPI for this project")` used to pollute the global store with project-local setup choices. Now there's an explicit signal filter: failed/partial attempts always promote (the outcome is the signal); decisions/notes only promote when their summary opens with `gotcha:` / `lesson:` / `warning:` / `caution:` / `pitfall:` / `avoid:` / `don't` / `do not` / `never` / `bug:`. Result on the test cycle: signal-to-noise went from 14% to 100%.

End-to-end verification across `globaltest/proj-react`, `proj-next`, `proj-python`, and `proj-go`: a `vite` gotcha logged in proj-react surfaces in proj-next with `source_project` attribution, stays out of proj-python and proj-go's responses, and a `gin` gotcha logged in proj-go promotes correctly under the new library cache + signal filter. Full results in [report/CROSS_PROJECT_TEST_PLAN.md](report/CROSS_PROJECT_TEST_PLAN.md).

### Bug fixes (the launch blockers)

- **MCP stdio integrity (L-009 + L-010)** — write tools used to corrupt the JSON-RPC stream via `typer.echo`, and one bad call would kill the entire session. Every tool body now runs inside a stdout-suppression context + a `@safe_tool` exception wrapper. Five consecutive write-tool calls survive cleanly in any client.
- **MCP project-root discovery (L-005)** — server used to fail with *"No .projectmem directory found"* when the MCP client launched it from its own CWD. New parent-walk fallback (like git does for `.git/`), plus a `--root` flag and `PROJECTMEM_ROOT` env var for explicit pinning.
- **Silent issue misattribution (L-027a)** — `pjm attempt` after a `pjm fix` used to silently attach to whatever issue was still open. Now uses a `.projectmem/.current_issue` marker, a 5-minute time-fence on the fallback, and an explicit `--issue <id>` flag.
- **Partial attempts dropped from summary (L-027b)** — `summary.md` only surfaced `failed` attempts; `partial` outcomes vanished even though they contained valuable signal. Now both render.
- **Project purpose stuck on placeholder (L-037)** — `summary.md`'s Project purpose section never escaped its init placeholder. Now auto-syncs from `PROJECT_MAP.md`'s `## Project purpose` section on every regeneration.

### Behavioral fixes

- **AI workflow alignment (L-028 / L-031 / L-036)** — three surfaces used to tell the AI different things about the session-start trio (MCP `instructions=` field, `CLAUDE.md` bridge, `AI_INSTRUCTIONS.md` template). All three now mirror each other: `get_instructions` → `get_summary` → `get_project_map`, plus the *"never edit `.projectmem/` files directly via filesystem write"* rule.
- **AI_INSTRUCTIONS.md rewrite (L-036)** — was CLI-only and out of sync with MCP. Now lists both MCP tools and CLI commands per trigger, distinguishes Setup Mode vs Maintenance Mode by concrete placeholder phrases (not "files populated"), and gives AI clients an imperative 6-step Setup procedure.
- **`pjm init` writes a CLAUDE.md bridge (L-004f)** — marker-bounded block at project root, idempotent on re-init. AI clients (Claude Code, Antigravity, Cursor) honor the memory layer by default.

### Quality of life

- **ROI surfaces reconciled (L-025d)** — `pjm stats` and `pjm score` used to report different `tokens_saved` numbers. `pjm stats` is now a thin presentation layer over `score.calculate_score` — single source of truth.
- **`pjm score --verbose` works (L-025a)** — was a no-op; now appends per-component event detail so you can audit exactly why the score is what it is.
- **`pjm stats --format json` (L-025b)** — CI-friendly JSON output matching `pjm score`'s format flag.
- **`pjm visualize --output / --no-open` (L-024b)** — choose where the HTML lands, skip auto-open (CI / headless).
- **`pjm search --regex` (L-027c)** — opt-in regex / OR-pattern search.
- **`pjm attempt --issue <id> / --auto-issue`** — explicit issue attribution + auto-creation of an implicit parent issue when none is open.
- **`pjm wrap` File Gotchas filter (L-022a)** — auto-backfill events used to pollute the gotchas section with the same generic note per file. Filtered out.
- **`pjm global` ergonomics (L-026b)** — `pjm global add "..." --library X` auto-routes to `add-gotcha`. Plus `--format json` on `list` / `detect` (L-026c).
- **Framework detection word-boundary fix (L-026a)** — `pjm global detect` no longer flags `gin` (Go framework) when scanning a React project with `eslint-plugin-react` (substring match on `plugin`). Word-boundary regex now.
- **Timestamp normalization (L-024a)** — `pjm visualize`'s Timeline used to show "INVALID DATE" on auto-backfill events. All events normalized to ISO-Zulu on write + defensive parser in the dashboard.
- **HIGH CHURN counter via git log (L-023a)** — was reading from event log (stale); now sources from `git log --since=N.days.ago` (live).

### Cross-client MCP verification

All four major 2026 MCP clients tested end-to-end against a real project:

- **Antigravity** — first client dogfooded; entire v0.0.6 bug list was found here, batch fix landed, every category re-verified.
- **Claude Desktop** — must use **Auto mode** (Plan mode bypasses MCP), pass project root via **`--root` in args** (the `cwd` JSON field is silently ignored in current builds), worktree mode requires init inside the worktree.
- **Cursor** — same `--root` workaround for the cwd-ignored bug. Per-project `.cursor/mcp.json` supported.
- **Codex** — config is **TOML at `~/.codex/config.toml`** (not JSON), UI Save button can silently fail (edit the file directly), set **reasoning effort to `medium` or higher** for the full session-start trio.

### Docs

- **README hero + demo images** now hosted on a separate public asset repo (`github.com/projectmem/projectmemdoc`) and referenced via `raw.githubusercontent.com`. PyPI's README renderer can fetch them regardless of the projectmem repo's visibility. Animated GIF (8-frame, Safari-safe) replaces the SVG that PyPI rendered inconsistently.
- **All 4 MCP clients' UI navigation paths documented** in README + Guide (Settings → Developer → ..., Settings → Tools & MCPs → ..., etc.).
- **First-run permission prompts callout** — documented as normal MCP-client behavior, not a bug.
- **Stale MCP server process troubleshooting** — common gotcha when iterating on MCP config; documented diagnostic commands + recovery.

### Carried over to v0.0.8 backlog

- L-038 (`pjm watch` duplicate churn events for one incident) — known cosmetic noise; documented fix queued for v0.0.8 polish.
- Universal AI Bridge (`pjm bridge install`, `pjm doctor`) — multi-bridge `pjm init` writing `.cursor/rules/`, `.github/copilot-instructions.md`, `AGENTS.md`, etc. — full design in FUTURE_PLAN.md.

---

## 0.0.6

projectmem transforms from a passive memory logger into an active intelligence layer. Zero-friction capture, intelligent injection, provable ROI, cross-project knowledge.

### Major Features

- **Auto-Capture Engine** — git hooks now classify commits into the right event types automatically. `revert` becomes a failed attempt, `fix:` becomes a fix event, `feat:` becomes a note, `BREAKING` becomes a decision. Zero manual logging required for common cases.
- **Pre-Commit Warnings (`pjm precheck`)** — the killer feature: warns you BEFORE you commit if you're about to repeat a failed approach, modify a high-churn file, or touch an unresolved issue. No other AI tool can do this — it requires the memory layer underneath.
- **Smart Context Injection (`pjm wrap`)** — wraps your AI agent (Claude, Cursor, Aider) and auto-injects a token-budgeted context block before the session starts. Inject into `CLAUDE.md`, `.cursorrules`, or clipboard.
- **Failure Prevention Score (`pjm score`)** — quantifiable ROI metric with letter grade (A+ through F). Tracks failed approaches on record, decisions documented, debugging hours saved, tokens saved, USD saved. Outputs as terminal display, JSON for CI, or shields.io badge for README.
- **Context Budget Optimizer (`pjm context`)** — generate token-budgeted project memory tailored to file focus and time window. Four compression levels (full / compressed / ultra / emergency). Git-aware — boosts events for files currently being worked on.
- **Cross-Project Global Memory (`pjm global`)** — knowledge that follows the developer across projects. Stores patterns, library gotchas, and stack preferences in `~/.projectmem/global/`. Auto-detects stack on `pjm init` (Python, JS, Rust, Go, Java) and inherits relevant gotchas. Export/import for team sharing.

### Enhancements

- **Auto-installed hooks on `pjm init`** — git hooks are installed automatically. No need to remember `pjm hooks install`. Opt out with `--no-hooks`.
- **Three hooks now installed**: `post-commit`, `post-merge`, and `pre-commit` (for `pjm precheck`).
- **Safe hook installation** — appends to existing hooks with clearly-marked snippets. Never overwrites. Clean uninstall removes only projectmem's section.
- **Visualization overhaul for auto-capture**:
  - New header stat: auto-captured event count
  - `AUTO` badge on auto-captured events in Timeline
  - New ROI cards: Manual / Auto-captured / Would Be Lost / Auto-capture Rate
  - New Capture Sources donut chart (git commits vs reverts vs manual)
  - New File Churn heatmap (top 10 files by activity, color-coded severity)
  - Manual/Auto filter pills in Timeline
  - Dashed borders + transparency on auto-captured nodes in Story Map
- **Event model extension**: `auto_captured`, `capture_source`, `capture_confidence`, `git_message` (backward compatible).
- **AI_INSTRUCTIONS.md template updated** with auto-capture awareness section telling AI agents what's auto-captured vs what still needs manual logging.
- **Hidden `_auto-capture` command** for internal use by git hooks.

### Real-Time File Watcher (`pjm watch`)

- **New command:** `pjm watch [--daemon|--stop|--status]` — opt-in real-time file watcher that detects high churn (4+ edits to the same file within 10 min) and auto-logs them as churn-detector events.
- **Auto-starts on `pjm init`** in interactive terminals — zero-touch experience. Skipped in CI/CD, piped output, and non-TTY environments to avoid zombie daemons.
- **Battery-aware:** idles when no activity, gitignore-aware, single-instance lock via PID file at `.projectmem/watch.pid`, graceful SIGTERM shutdown.
- **Project Map tree view:** new horizontal dendrogram (D3 cluster + bezier links) with zoom/pan, toggleable against the existing force-graph view.
- **Opt-out flag:** `pjm init --no-watch` for power users or battery-conscious environments.

### Zero-Touch Setup

- **Auto-backfill on `pjm init`** — automatically ingests the last 20 git commits as classified events. Fresh repos = silent no-op. Existing repos = instant dashboard with real data. Opt out with `pjm init --no-backfill`.
- **Auto-installed git hooks** + **auto-started watcher** + **auto-backfilled history** + **auto-inherited global memory** all happen in a single `pjm init` call. From `pip install` to active memory in two commands.

### MCP Server Expansion (8 → 14 tools)

Native MCP server now exposes intelligence-layer capabilities to AI agents, not just raw memory:

- **`precheck_file(path)`** — AI can self-check a file's failure history *before* proposing changes (turns memory into proactive judgment).
- **`get_issue(id)`** — lazy-load one specific issue file for token efficiency.
- **`search_events(query, limit)`** — plain-text search over the event log instead of loading the full summary.
- **`get_score()`** — AI can report the prevention score with hours/tokens/dollars saved.
- **`get_context(tokens, focus)`** — AI requests an on-demand token-budgeted context block.
- **`get_global_gotchas(library)`** — AI queries cross-project memory for library-specific lessons.

Existing 8 tools (`get_summary`, `log_issue`, `record_attempt`, `record_fix`, `add_decision`, `add_note`, `get_instructions`, `get_project_map`) unchanged.

### Privacy & Security

- **`SECURITY.md`** at repo root with vulnerability disclosure policy and threat model.
- **Privacy & Security section** in the user guide explaining the team-memory-via-git pattern, local-first guarantees, prompt-injection considerations, and uninstall path.
- **Cleaner gitignore default** — only `events.jsonl`, `watch.pid`, `watch.log` are ignored by default, allowing `summary.md` / `PROJECT_MAP.md` / `AI_INSTRUCTIONS.md` to be shared via git. Opt into total privacy by adding `.projectmem/` to `.gitignore`.

### Dependencies

- `watchdog>=4.0` promoted from optional to required dependency — required for the auto-started file watcher. Adds ~70KB to install size.

### Breaking Changes

None — v0.0.6 is purely additive. Existing `events.jsonl` files continue to work without modification.

## 0.0.4

- **Major Feature**: Complete overhaul of `viz.html` into a stunning, single-page Tabbed Dashboard (Story Map, ROI Dashboard, Project Map, Timeline).
- **Major Feature**: Automated D3.js Architecture Graph generation—`pjm visualize` now natively parses your Markdown `PROJECT_MAP.md` into an interactive node graph with zero extra AI tokens.
- **Enhancement**: Upgraded dashboard aesthetic to a high-end, soothing "Midnight Blue & Indigo" professional palette to reduce developer eye strain.
- **Enhancement**: Added explicit documentation and guarantees in the README for configuring native MCP vs Custom System Prompts for 100% hands-free workflows.

## 0.0.3

- **Major Feature**: Native MCP Server (`pjm-mcp`) for direct integration with Claude Desktop and Cursor.
- **Major Feature**: Interactive D3.js visualization (`pjm visualize`) showing project story and technical debt heatmap.
- **Major Feature**: Auto-backfill (`pjm backfill`) to ingest git history into project memory.
- **Major Feature**: Token ROI Dashboard (`pjm stats`) to calculate and visualize AI tokens saved.
- **Major Feature**: 3-Level Auto-Tracking system for hands-free memory management:
  - Level 1: Trigger-based `AI_INSTRUCTIONS.md` with MANDATORY rules that force AI agents to log work automatically.
  - Level 2: MCP server with built-in system prompt and proactive tool descriptions (MANDATORY/IMMEDIATELY language).
  - Level 3: Auto-capture Git Hooks that log `revert`, `fix:`, `feat:`, and `BREAKING` commits passively.
- **Enhancement**: Added `pjm` alias globally to prevent conflicts with other system tools.
- **Enhancement**: Added location metadata (`--at`) support to all logging commands.
- **Enhancement**: Added `get_instructions()` MCP tool so AI agents can read project rules natively.
- **Enhancement**: Added "Maintenance Mode" logic to `AI_INSTRUCTIONS.md` to prevent redundant structural mapping.
- **Fix**: Corrected JavaScript syntax error in D3.js forceLink chain that prevented visualization rendering.

## 0.0.2

- Add `.projectmem/AI_INSTRUCTIONS.md` during initialization.
- Add `.projectmem/PROJECT_MAP.md` as the AI-created structural map placeholder.
- Add `pm instructions` to print the project AI memory protocol.
- Add `pm map` to print the project map.
- Improve the initial `summary.md` so new projects are not blank.

## 0.0.1

- Initial local MVP scaffold.
