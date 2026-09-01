# AGENTS.md

<!-- >>> projectmem codex bridge >>> -->
## Projectmem

Use the global `pjm-mcp-global` server for Projectmem. Pass this exact `project_id` to every project-scoped tool: `proj_31b0c61074df472d90702cd583c706ef`. Do not bind `--root` or infer a project from CWD.

Before the first substantive project task in a new or resumed agent session, call `get_instructions(project_id)`, then `get_summary(project_id)`. Do not repeat them in an unchanged conversation; refresh after context recovery, task switching, or when the project state may have changed. Before editing a file call `precheck_file(project_id, path)`.
<!-- <<< projectmem codex bridge <<< -->
