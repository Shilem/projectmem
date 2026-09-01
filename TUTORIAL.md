# ProjectMem tutorial

This guide uses the [Shilem/projectmem](https://github.com/Shilem/projectmem)
fork. It shows how to install the source locally, initialize another Git
repository, and connect an MCP-capable AI client.

---

## What you'll need

- Python ≥ 3.10
- A git repository you can experiment in (or a fresh `git init` somewhere)
- An MCP-capable AI client — any of: Claude Desktop, Cursor, Antigravity
  (legacy IDE), Codex. The flow is identical across all four.

---

## Step 1 — Install this fork

Clone the repository if you want the current source or plan to contribute:

```bash
git clone https://github.com/Shilem/projectmem.git
cd projectmem
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
pjm --help
```

An editable installation follows the source after `git pull`. If you only want
to use the command-line tool, run `pipx install
"git+https://github.com/Shilem/projectmem.git"` instead.

The package installs `pjm`, `pjm-mcp`, and `pjm-mcp-global`. The global MCP
server is the normal choice for new setups.

---

## Step 2 — Initialize in your project

```bash
cd path/to/your/project
pjm init
```

You should see, in order:

1. `.projectmem/` directory created with `events.jsonl`, `summary.md`,
   `PROJECT_MAP.md`, `AI_INSTRUCTIONS.md`.
2. `AGENTS.md` and `CLAUDE.md` receive a small ProjectMem bridge so compatible
   AI clients know to load project memory first.
3. `PROJECT_MAP.md` is pre-populated from your `pyproject.toml` /
   `package.json` / `Cargo.toml` / `go.mod` — no manual stack tour needed.
4. **Git hooks installed** — `pre-commit` for failure warnings,
   `post-commit` and `post-merge` for auto-capture.
5. A printed **global MCP config block** — copy it once. Future `pjm init`
   projects register automatically and use their persisted `project_id`.

---

## Step 3 — Wire up your AI client

Paste the printed global config block once into your client's MCP config file:

| Client | Config file |
|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Cursor | Settings → Tools & MCPs (global) |
| Antigravity (legacy) | `~/.gemini/antigravity/mcp_config.json` |
| Codex | `~/.codex/config.toml` (TOML, not JSON) |

Then **fully quit and restart the client** (cold start — closing the
window isn't enough; MCP servers initialize only on launch).

**Verify the connection:** open your client's tools panel. You should
see `list_projects` and project tools whose first parameter is `project_id`.
This is the boundary that keeps projects isolated even though the client uses
one MCP process.

---

## Step 4 — First chat: Setup Mode

Open a new chat in your AI client. Paste:

> *I just added projectmem to this project. Please get the project
> summary, and since the memory is still empty, read the source files
> and set it up: capture what this project is, its stack, and any
> architectural decisions worth recording. Then regenerate the summary.*

The agent will:
1. Call `get_summary` → see the placeholder content.
2. Read your source files.
3. Call `add_decision` and `add_note` to record what it learns.
4. Call `get_summary` again to confirm the new state.

You're now in **Setup Mode → done.** Memory is populated.

---

## Step 5 — Hit a bug, watch the lifecycle

Pick any real bug in your project. In your AI chat:

> *Bug: \<describe the symptom\>. Log this as an issue in projectmem,
> then investigate the cause.*

Expect: `log_issue` followed by the agent reading the relevant files.

Try a fix that you think might be wrong:

> *Try \<approach A\> first. If it doesn't work, we'll try something
> else.*

When it doesn't fix the bug:

> *That didn't work. Record it as a failed attempt in projectmem, then
> revert it.*

Expect: `record_attempt` with `outcome="failed"`. **This is the seed
that makes the pre-commit warning fire later.**

Now apply the real fix:

> *The real fix is \<approach B\>. Apply it, confirm the bug is gone,
> and record the fix.*

Expect: `record_fix` — the issue closes, memory is sealed.

Run `pjm show` in your terminal — you'll see the full audit trail:
issue → failed attempt → fix.

---

## Step 6 — The killer feature: pre-commit warning

A week from now, in a totally different chat, the AI agent forgets and
suggests **approach A again** (the failed one). You apply it without
thinking, stage it, and:

```bash
git commit -m "fix: try approach A"
```

projectmem's pre-commit hook intercepts and prints:

```
projectmem: Pre-Commit Check
  <file>
    WARN  1 failed attempt on this file
    Last failure: tried approach A — didn't fix the bug
```

You pivot before committing the same dead end. **That's the judgment
layer.** Memory + warning at the moment of action.

> If the warning doesn't fire, run `pjm hooks install` — `pjm init`
> auto-installs hooks but only when `.git/hooks/` exists at the moment
> of init.

---

## Step 7 — New chat: Maintenance Mode

Open a **brand-new** chat in your AI client (fresh context). Paste:

> *Get me up to speed on this project — what is it, what's been worked
> on recently, and are there any known gotchas I should avoid?*

The agent will call `get_summary` / `get_context` and answer **from
memory** — no re-reading of source files. That's the token savings in
action: a fresh session inherits everything the previous one learned.

---

## Step 8 — See your value

```bash
pjm score        # A–F grade + hours saved + tokens saved + USD saved
pjm visualize    # interactive D3 dashboard in your browser
```

The score is your concrete answer to *"is this thing actually saving me
time?"*

---

## What just happened

In about 15 minutes you exercised every part of projectmem:

| Feature | Where you saw it |
|---|---|
| Setup Mode (Step 4) | Agent populated `summary.md` and `PROJECT_MAP.md` |
| Issue → attempt → fix lifecycle (Step 5) | `log_issue`, `record_attempt`, `record_fix` |
| Pre-commit warning (Step 6) | `git commit` printed the warning |
| Maintenance Mode (Step 7) | Fresh chat answered from memory, no file scan |
| Token savings (Step 7 + 8) | `pjm score` reports the saved-token estimate |
| Secret redaction | Any API key you accidentally pasted is auto-scrubbed before disk |

---

## Common gotchas

- **Pre-commit warning doesn't fire** → `.git/hooks/pre-commit` doesn't
  exist. Run `pjm hooks install`, then confirm the hook with
  `head .git/hooks/pre-commit`.
- **Agent re-reads files instead of using memory** → check the ProjectMem
  bridge blocks in `AGENTS.md` and `CLAUDE.md`, then fully restart the AI
  client after adding its MCP configuration.
- **Multiple machines / team members** → `.projectmem/` is meant to be
  **committed** to git. Each clone inherits the memory. Don't
  `.gitignore` it.

---

## Where to go next

- **Cross-project memory:** lessons learned in one repo can surface in
  others with the same stack (`~/.projectmem/global/`). Try
  `pjm global show` to see what's accumulated across your machine.
- **Issues + feature requests:** [github.com/Shilem/projectmem/issues](https://github.com/Shilem/projectmem/issues).

---

*Last updated for the Shilem fork, version 0.2.0.*
