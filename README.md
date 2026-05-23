<!-- mcp-name: io.github.riponcm/projectmem -->

<div align="center">
  <img src="https://raw.githubusercontent.com/projectmem/projectmemdoc/main/logo/projectmem-wordmark-800.png" alt="projectmem" width="420" />

  <p><b>We don't make AI smarter. We make it experienced.</b></p>
  <p><i>The local-first memory + judgment layer for AI coding agents. Save up to 50%+ of AI tokens. Stop repeating yesterday's bug.</i></p>

  <p>
    <a href="https://pypi.org/project/projectmem/"><img src="https://img.shields.io/pypi/v/projectmem.svg?color=4c1d95&label=pypi" alt="PyPI version"></a>
    <a href="https://pypi.org/project/projectmem/"><img src="https://img.shields.io/pypi/pyversions/projectmem.svg?color=3b82f6" alt="Python Versions"></a>
    <a href="https://pypi.org/project/projectmem/"><img src="https://img.shields.io/pypi/dm/projectmem.svg?color=10b981&label=downloads" alt="PyPI Downloads"></a>
    <a href="https://github.com/riponcm/projectmem/stargazers"><img src="https://img.shields.io/github/stars/riponcm/projectmem?style=flat&color=f59e0b&label=stars" alt="GitHub stars"></a>
    <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-3b82f6.svg" alt="License: MIT"></a>
    <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Code style: ruff"></a>
  </p>

  <p>
    <a href="https://projectmem.dev"><b>Website</b></a> •
    <a href="https://projectmem.dev/guide"><b>Guide</b></a> •
    <a href="https://projectmem.dev/demo"><b>Demo</b></a> •
    <a href="https://projectmem.dev/changelog"><b>Changelog</b></a>
  </p>

  <br />

  <img src="https://raw.githubusercontent.com/projectmem/projectmemdoc/main/demo/precheck-warning.gif" alt="projectmem pre-commit warning demo" width="720" />
</div>

---

## 🎬 Watch the demo

<p align="center">
  <a href="https://youtu.be/pELGdXHj_Ls">
    <img src="https://img.youtube.com/vi/pELGdXHj_Ls/maxresdefault.jpg" alt="projectmem — 60-second demo" width="720" />
  </a>
  <br />
  <em>Full screen-recorded tutorial- watch on YouTube</em>
</p>

## 📚 Docs

| Doc | What's in it |
|---|---|
| **[TUTORIAL.md](TUTORIAL.md)** | 15-minute step-by-step walkthrough — set up projectmem on your own project, watch the lifecycle, see the pre-commit warning fire. |
| **[CHANGELOG.md](CHANGELOG.md)** | Release history. Latest: v0.1.3 — schema enrichment, secret redaction, conda/venv hook fix, stack auto-detect, MCP config printed at init. |
| **[LICENSE](LICENSE)** | MIT |

---

## The Problem

Every new AI session starts from zero. Claude, Cursor, Aider — they all forget yesterday's decisions, repeat failed debugging attempts, and burn millions of tokens reconstructing context from raw source files.

The model isn't the problem. **The architecture is.** Stateless models need a memory cortex.

## The Solution

`projectmem` is the local-first memory + judgment layer that sits above your AI tools. It captures every failed attempt, decision, and gotcha — then injects that experience back into future AI sessions. Git tracks *what* changed. `projectmem` tracks *why* it changed, what was tried, and what failed.

## Install

```bash
pip install projectmem
cd your-project
pjm init
```

That's it. `pjm init` installs three git hooks (pre-commit warnings, post-commit classification, post-merge tracking), auto-starts a real-time file watcher, inherits cross-project memory if available, and creates `.projectmem/`. Capture is active from minute one.

> The canonical command is `projectmem`. A `pjm` alias is installed for speed.

## Why You'll Love It

- **Pre-Commit Warnings** — `pjm precheck` warns you *before* you commit if you're about to repeat a failed approach, modify a high-churn file, or touch an unresolved issue. No other AI tool does this — it requires the memory layer underneath.
- **Smart Context Injection** — `pjm wrap claude` (or cursor/aider) injects a token-budgeted memory block into your AI before the session opens. Your AI starts experienced, not blank.
- **Provable ROI Score** — `pjm score` outputs a letter grade (A+ → F) backed by concrete numbers — debugging hours saved, tokens prevented, dollars protected. CI-friendly JSON output and shields.io badge for your README.
- **Cross-Project Memory** — Lessons learned in one repo follow you forever. Library gotchas, decisions, and patterns live in `~/.projectmem/global/` and auto-inherit into every new project that matches your stack.
- **Real-time File Watcher** — Background daemon detects rapid edits to the same file (debugging sessions) between commits. Battery-aware, gitignore-aware, auto-started by `pjm init`.
- **Native MCP Server** — Plugs into Claude Desktop, Cursor, Antigravity, Codex, and any MCP-compatible tool. 14 native tools force the AI to read context, check files for known failures, and log work automatically. Verified end-to-end against all four clients in v0.0.6.
- **Interactive Dashboard** — `pjm visualize` opens a four-tab D3.js dashboard: Story Map (failure heatmap), ROI Dashboard, Project Map (tree or graph view), Timeline.
- **100% Local** — No cloud, no telemetry, no accounts. Your code, your memory, your machine.

## How It Compares

| Capability | **projectmem** | claude-mem | Graphify | mem0 | Cursor |
|---|:---:|:---:|:---:|:---:|:---:|
| Core focus | **Memory + Judgment** | Session capture | Static code map | Chat memory | IDE replacement |
| Captures development history | ✅ classified events | ~ raw log | ❌ | ~ chat-level | ❌ |
| Records architectural decisions | ✅ | ❌ | ❌ | ❌ | ❌ |
| Pre-commit failure warnings | ✅ **unique** | ❌ | ❌ | ❌ | ❌ |
| Cross-project memory | ✅ stack-aware | ~ filter only | ✅ | ~ cloud only | ❌ |
| Provable ROI score | ✅ A+ → F + $ | ❌ | ❌ | ❌ | ❌ |
| Auto-capture from git | ✅ post-commit hooks | ❌ | ~ re-index only | ❌ | ❌ |
| Real-time file watcher | ✅ opt-in daemon | ❌ | ❌ | ❌ | ❌ |
| Native MCP server | ✅ 14 tools | ✅ | ✅ | ~ | ❌ |
| 100% local / no cloud | ✅ | ✅ | ~ | ❌ cloud | ❌ |
| Tool-agnostic | ✅ | ✅ | ✅ | ~ | ❌ vendor-locked |
| Price | ✅ Free · MIT | Free | Free · MIT | Paid SaaS | $20/mo |

## How AI Reads Your Memory (Token Efficiency)

The architecture is built around one rule: **AI reads small, distilled files. Tools generate them from the big raw log.**

| Access mode | Tokens / session | How it works |
|---|---|---|
| No projectmem (baseline) | 5,000 – 20,000+ | AI re-reads source files every session |
| Universal Mode (markdown) | ~2,500 | AI reads 3 small distilled files once |
| **MCP Mode** *(recommended)* | **~800 – 1,500** | AI calls `get_summary()`, then `get_issue(id)` only when relevant |
| `pjm wrap` (pre-injection) | 500 – 2,000 | Pre-generated, you set the budget |

**AI never reads `events.jsonl` directly.** That file is for tools (`pjm score`, `pjm context`, `pjm wrap`). Tools distill the raw log into compact AI-readable summaries.

## MCP Integration (Recommended)

### Claude Desktop

**Easiest — open the config from the UI:**

- **macOS:** Claude menu → `Settings…` → `Developer` tab → **Local MCP servers** → **Edit Config**.
- **Windows / Linux:** same path expected (`Settings → Developer → Edit Config`) — open an issue if your platform differs and we'll update this.

If you prefer the raw file path: `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows.

Paste this block:

```json
"mcpServers": {
  "projectmem": {
    "command": "/opt/anaconda3/bin/python",
    "args": [
      "-m", "projectmem.mcp_server",
      "--root", "/absolute/path/to/your/project"
    ]
  }
}
```

**Two things to know about this block:**

- **Use the absolute path to `python`** (e.g. `/opt/anaconda3/bin/python`, or run `which python` to find yours). Claude Desktop subprocesses don't inherit your shell `PATH`, so bare `"python"` often fails.
- **We pass the project root via `--root`, not the `cwd` JSON field.** Claude Desktop's current build (with the Epitaxy / Cowork workspace system) silently ignores the `cwd` field — the server ends up running with `cwd=/` and can't find `.projectmem/`. The `--root` flag is honored by projectmem directly (read from `sys.argv`) and works regardless of how Claude Desktop spawns the subprocess.

Then **fully quit Claude Desktop (Cmd+Q on Mac)** and reopen — MCP servers only initialize on cold start.

### Cursor

Two ways to register the MCP server — pick whichever fits your workflow:

1. **Global (recommended):** Cursor menu → `Settings…` → left sidebar **Tools & MCPs** → **Installed MCP Servers** → **Add Custom MCP**. Paste the JSON below.
2. **Per-project:** drop the JSON into `<project-root>/.cursor/mcp.json` — only active when that project is open.

```json
{
  "mcpServers": {
    "projectmem": {
      "command": "/opt/anaconda3/bin/python",
      "args": [
        "-m", "projectmem.mcp_server",
        "--root", "/absolute/path/to/your/project"
      ]
    }
  }
}
```

**Two things to know about this block (same gotchas as Claude Desktop):**

- **Use the absolute path to `python`** (run `which python` to find yours). Cursor subprocesses don't reliably inherit your shell `PATH`.
- **Pass the project root via `--root`, not the `cwd` JSON field.** Cursor — like Claude Desktop — silently ignores `cwd`: the server ends up running with `cwd=~` and can't find `.projectmem/`. The `--root` flag is honored by projectmem directly and works around the bug.

Then **fully quit Cursor (Cmd+Q on Mac)** and reopen. projectmem also auto-discovers `.projectmem/` by walking up from CWD (like git does for `.git/`), and honors `PROJECTMEM_ROOT` and a `--root <path>` CLI argument.

### Antigravity

Antigravity (Google's AI IDE) speaks standard MCP.

**Easiest — open the config from the UI:**

1. Open the **Agent** window (the chat panel on the right).
2. Click the **⋯ Additional Options** button in the panel header.
3. Choose **MCP Servers** → **Manage MCP Servers** → **Add new** (or **Edit Config**).

The raw file is at `~/.gemini/antigravity/mcp_config.json` if you prefer editing it directly.

Paste this block:

```json
{
  "mcpServers": {
    "projectmem": {
      "command": "python",
      "args": ["-m", "projectmem.mcp_server"],
      "cwd": "/absolute/path/to/your/project"
    }
  }
}
```

Then **fully quit Antigravity (Cmd+Q on Mac)** and reopen — MCP servers only initialize on cold start. All 14 projectmem tools register identically to Claude Desktop / Cursor.

### Codex

Codex stores MCP config as **TOML** (not JSON) in `~/.codex/config.toml`. There's a UI form at `Settings → MCP Servers → Add MCP Server`, but during v0.0.6 verification the form's **Save button didn't reliably persist** — the file-edit path is faster and more reliable.

**Easiest — edit `~/.codex/config.toml` directly:**

Append this block (preserves any existing config):

```toml
[mcp_servers.projectmem]
command = "/opt/anaconda3/bin/python"
args = ["-m", "projectmem.mcp_server", "--root", "/absolute/path/to/your/project"]
cwd = "/absolute/path/to/your/project"
```

Three things to know about this block:

- **Use the absolute path to `python`** (run `which python` to find yours). Codex subprocesses don't reliably inherit your shell `PATH`.
- **Pass the project root via `--root` in args** (defense in depth). The `cwd` field appears to work in Codex, unlike Claude Desktop and Cursor — but `--root` costs nothing and saves us if any future Codex build regresses.
- **Set your reasoning effort to `medium` or higher.** On low-reasoning Codex skips `get_instructions` from the session-start trio, which can cause the AI to miss the Setup Mode workflow rules. Medium+ honors the full trio automatically.

**Validate the TOML:**

```bash
python -c "import tomllib; tomllib.load(open('/Users/<you>/.codex/config.toml','rb')); print('OK')"
```

Should print `OK`. If not, the parser tells you the offending line.

**Then fully quit Codex (Cmd+Q on Mac) and reopen.** Same cold-start rule as every other MCP client. Codex MCP servers spawn lazily on the first tool call in a chat session — if you don't see the process in `ps aux` right after reopening, send any message to a Codex chat and check again.

**Reasoning-effort note:** Codex's mode selector is at the bottom of the chat input. Set it to `medium` (not `low`) for the full session-start trio behavior. Once set, it persists per-session.

### First-run permission prompts

On first use in any MCP-capable client (Claude Desktop, Cursor, Antigravity, Codex),
your AI will ask permission before each projectmem tool call. **This is
expected security behavior** — MCP clients require explicit consent for
every new tool. Approve each tool once and the prompt won't reappear for
that session.

### Other MCP Tools

Any MCP-compatible client works — point your tool at
`python -m projectmem.mcp_server` and either set `cwd` to your project
root or rely on the parent-walk auto-discovery.

### MCP Tools Exposed

All 14 tools your AI can call:

**Read-side (9 tools):**

| Tool | When to use |
|---|---|
| `get_instructions()` | Start of every session — load workflow rules |
| `get_summary()` | Start and end — distilled project memory |
| `get_project_map()` | Start — understand repo structure |
| `precheck_file(path)` | Before editing any file — surface failure history |
| `get_issue(id)` | Read one specific issue's full history by ID |
| `search_events(query)` | Plain-text search across all logged events |
| `get_context(tokens, focus)` | Token-budgeted memory block with optional focus filter |
| `get_score()` | A+→F prevention score + ROI numbers |
| `get_global_gotchas(library)` | Cross-project library lessons inherited from past repos |

**Write-side (5 tools):**

| Tool | When to use |
|---|---|
| `log_issue(summary, location)` | Immediately when encountering a bug |
| `record_attempt(summary, outcome)` | Immediately after each fix attempt (outcome: `failed`/`partial`/`worked`) |
| `record_fix(summary)` | After confirming a fix resolves the issue |
| `add_decision(summary)` | When making architectural / design decisions |
| `add_note(summary)` | When discovering gotchas, setup details, or constraints |

## CLI Reference

### Core memory

| Command | Purpose |
|---|---|
| `pjm init` | Initialize memory + auto-install hooks + inherit global memory |
| `pjm log <text>` | Start a new issue / debugging session |
| `pjm attempt <text> [--failed\|--worked]` | Record a fix attempt outcome |
| `pjm fix <text>` | Record the confirmed fix and close the issue |
| `pjm decision <text>` | Record an architectural decision |
| `pjm note <text>` | Record durable context or a gotcha |
| `pjm show` | Print the current summary |
| `pjm search <query>` | Plain-text search across all events |

### Intelligence layer (v0.0.6)

| Command | Purpose |
|---|---|
| `pjm watch [--daemon\|--stop\|--status]` | Real-time file churn watcher |
| `pjm precheck` | Warn about repeating failed approaches before commit |
| `pjm wrap <agent>` | Inject token-budgeted memory into Claude/Cursor/Aider |
| `pjm context [--tokens N]` | Generate token-budgeted project context |
| `pjm score [--format text\|json\|badge]` | Letter-grade prevention score |
| `pjm global <action>` | Manage cross-project memory |

### Visualization & utility

| Command | Purpose |
|---|---|
| `pjm visualize` | Open interactive D3.js dashboard |
| `pjm stats` | Token ROI summary in the terminal |
| `pjm backfill` | Auto-populate memory from git history |
| `pjm hooks install\|uninstall` | Manage git hooks manually |
| `pjm regenerate` | Rebuild `summary.md` from `events.jsonl` |

> Use `--at "file.py:42"` with any logging command to attach precise location metadata.

## Example: Pre-Commit Warnings in Action

```bash
$ git commit -m "switch auth to JWT"

projectmem: Pre-Commit Check
─────────────────────────────────────────────
  src/auth/middleware.py
    WARN  3 failed attempts on this file
           Last failure: Tried switching to JWT middleware
             (2 days ago)
    WARN  HIGH CHURN: 5 changes in last 30 days
─────────────────────────────────────────────
2 warning(s). Review before committing.

~30 min re-debugging just saved.
```

## Privacy & Security

By default, `projectmem` commits the **distilled** files (`summary.md`, `PROJECT_MAP.md`, `AI_INSTRUCTIONS.md`, `issues/`) and gitignores the raw log + runtime files (`events.jsonl`, `watch.pid`, `watch.log`). This means your teammate's AI inherits your team's knowledge automatically — just `git clone` and the AI already knows what your team learned.

**Want total privacy?** Add a single line `.projectmem/` to your `.gitignore`. Nothing leaves your machine.

Full security policy and threat model: [SECURITY.md](SECURITY.md) · [Privacy & Security guide](https://projectmem.dev/guide#privacy-security)

## Design Principles

- **Local-first** — No network calls, no cloud, no telemetry. Your data never leaves your machine.
- **Project-scoped** — Memory lives in the repo. When the code moves, the memory moves.
- **AI-tool-agnostic** — Works natively via MCP, or universally via Markdown instructions. Any AI tool, any workflow.

## Built With

`projectmem` stands on the shoulders of these excellent open-source projects:

- [**Typer**](https://github.com/tiangolo/typer) — the CLI framework that makes `pjm` feel ergonomic
- [**Model Context Protocol**](https://modelcontextprotocol.io) — Anthropic's open spec that lets AI agents talk to local tools
- [**watchdog**](https://github.com/gorakhargosh/watchdog) — cross-platform filesystem event monitoring (the heart of `pjm watch`)
- [**D3.js**](https://d3js.org) — the interactive visualizations in `pjm visualize`

## License

MIT — free for personal, commercial, and enterprise use forever.

---

## Help Us Reach More Developers

**We don't need money. We need you.**

`projectmem` is built by one developer for the open-source community. Every star, every share, and every contribution helps the project survive and grow.

- **[Star the repo](https://github.com/riponcm/projectmem)** — takes one click, helps massively with discovery
- **Share on X / LinkedIn** — tell other devs they don't have to keep paying AI to relearn their codebase
- **[Open an issue](https://github.com/riponcm/projectmem/issues)** — bug, feature request, or just feedback
- **[Contribute code](https://github.com/riponcm/projectmem/blob/main/CONTRIBUTING.md)** — PRs welcome, see contributing guide
- **Using `projectmem` at work or in a commercial product?** Reach out to [support@projectmem.dev](mailto:support@projectmem.dev) so we know who's shipping with us. It's free — we just love hearing about it.

*Stars and shares matter more than money — but if you really want to:* [sponsor on GitHub](https://github.com/sponsors/riponcm) →

---

<div align="center">
  <sub>Built with care by the open-source community. Every contribution, no matter how small, makes a difference.</sub>
</div>
