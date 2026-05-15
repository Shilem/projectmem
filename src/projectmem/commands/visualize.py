from __future__ import annotations

import json
import re
import webbrowser
from pathlib import Path
from typing import Any

import typer

from projectmem.models import Event, normalize_timestamp
from projectmem.storage import read_events, require_mem_dir, project_map_path
from projectmem.commands.stats import calculate_savings


def run(
    root: Path | None = None,
    output: Path | None = None,
    open_browser: bool = True,
) -> None:
    events = read_events(root)
    mem_dir = require_mem_dir(root)

    # 1. Build the graph data
    graph_data = build_graph_data(events)

    # 2. Read PROJECT_MAP.md for the Project Map tab
    map_path = project_map_path(root)
    project_map_text = ""
    project_map_graph = {"nodes": [], "links": []}
    if map_path.exists():
        project_map_text = map_path.read_text(encoding="utf-8")
        project_map_graph = build_project_map_graph(project_map_text)

    # 3. Build timeline data for the Timeline tab
    timeline_data = build_timeline_data(events)

    # 4. Generate the HTML
    html_content = (
        VIZ_TEMPLATE
        .replace("{{GRAPH_DATA}}", json.dumps(graph_data))
        .replace("{{PROJECT_MAP}}", json.dumps(project_map_text))
        .replace("{{PROJECT_MAP_GRAPH}}", json.dumps(project_map_graph))
        .replace("{{TIMELINE_DATA}}", json.dumps(timeline_data))
    )

    # 5. Save and (optionally) open
    viz_path = Path(output) if output else (mem_dir / "viz.html")
    viz_path.parent.mkdir(parents=True, exist_ok=True)
    viz_path.write_text(html_content, encoding="utf-8")

    typer.echo(f"Visualization generated at {viz_path}")
    if open_browser:
        webbrowser.open(viz_path.as_uri())


def build_project_map_graph(map_text: str) -> dict[str, Any]:
    nodes = []
    links = []
    node_set = set()
    
    path_pattern = re.compile(r'`([^`]+)`')
    
    # Pass 1: Extract all path-like nodes
    for match in path_pattern.finditer(map_text):
        name = match.group(1)
        if name not in node_set and len(name) > 1 and ("/" in name or "." in name):
            node_set.add(name)
            node_type = "folder" if name.endswith("/") else "file"
            label = name.split("/")[-2] + "/" if node_type == "folder" else name.split("/")[-1]
            nodes.append({
                "id": name,
                "label": label,
                "type": node_type,
                "full_path": name
            })
            
    # Pass 2: Extract explicit relationships from bullet points
    lines = map_text.splitlines()
    in_rel = False
    for line in lines:
        if line.startswith("## Relationships"):
            in_rel = True
            continue
        elif in_rel and line.startswith("##"):
            in_rel = False
            
        if in_rel and line.strip().startswith("-"):
            paths = [m.group(1) for m in path_pattern.finditer(line) if m.group(1) in node_set]
            if len(paths) >= 2:
                source = paths[0]
                for target in paths[1:]:
                    links.append({"source": source, "target": target})
                    
    # Pass 3: Implicit hierarchy relationships
    for node in nodes:
        node_id = node["id"]
        for parent in nodes:
            parent_id = parent["id"]
            if parent_id != node_id and parent_id.endswith("/") and node_id.startswith(parent_id):
                # Link if it's a direct child (no extra slashes)
                rel_path = node_id[len(parent_id):]
                if "/" not in rel_path or (rel_path.count("/") == 1 and rel_path.endswith("/")):
                    links.append({"source": node_id, "target": parent_id})
                    
    return {"nodes": nodes, "links": links}


def build_timeline_data(events: list[Event]) -> list[dict[str, Any]]:
    timeline = []
    for event in events:
        # Normalize timestamps before serializing — older events from
        # `pjm backfill` use git's "YYYY-MM-DD HH:MM:SS ±HHMM" format which
        # JS `new Date()` can't parse, producing "INVALID DATE" sections
        # in the Timeline tab (L-024a).
        entry: dict[str, Any] = {
            "type": event.type,
            "summary": event.summary,
            "timestamp": normalize_timestamp(event.timestamp),
            "outcome": event.outcome,
            "location": event.location,
            "issue_id": event.issue_id,
        }
        # Auto-capture fields
        if event.auto_captured:
            entry["auto_captured"] = True
            entry["capture_source"] = event.capture_source
            entry["capture_confidence"] = event.capture_confidence
            entry["git_message"] = event.git_message
        timeline.append(entry)
    return timeline


def build_graph_data(events: list[Event]) -> dict[str, Any]:
    nodes = []
    links = []

    # Track nodes to avoid duplicates
    node_ids = set()

    # Failure counts for heatmap
    failure_counts = {}

    # Helper to add file nodes
    def add_file(path: str):
        if path and path not in node_ids:
            node_ids.add(path)
            nodes.append({
                "id": path,
                "type": "file",
                "label": path.split("/")[-1],
                "full_path": path,
                "failures": 0
            })

    # First pass: Collect all files and calculate failures
    for event in events:
        for f in event.files:
            add_file(f)
        if event.location and ":" in event.location:
            f = event.location.split(":")[0]
            add_file(f)
            if event.outcome == "failed":
                failure_counts[f] = failure_counts.get(f, 0) + 1

    # Update failure counts in nodes
    for node in nodes:
        if node["id"] in failure_counts:
            node["failures"] = failure_counts[node["id"]]

    # Second pass: Collect events and links
    for i, event in enumerate(events):
        event_id = event.id or f"evt_{i}"
        node_ids.add(event_id)

        node_data: dict[str, Any] = {
            "id": event_id,
            "type": "event",
            "event_type": event.type,
            "label": event.summary[:30] + ("..." if len(event.summary) > 30 else ""),
            "summary": event.summary,
            "timestamp": event.timestamp,
            "outcome": event.outcome,
            "location": event.location,
        }
        if event.auto_captured:
            node_data["auto_captured"] = True
            node_data["capture_source"] = event.capture_source
        nodes.append(node_data)

        # Link event to its files
        for f in event.files:
            links.append({"source": event_id, "target": f, "type": "mention"})

        # Link event to its location file
        if event.location and ":" in event.location:
            f = event.location.split(":")[0]
            links.append({"source": event_id, "target": f, "type": "at"})

    # 3. Calculate ROI stats
    raw_events = [e.__dict__ for e in events]
    stats = calculate_savings(raw_events)

    return {"nodes": nodes, "links": links, "stats": stats}


VIZ_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>projectmem Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        :root {
            --bg: #0b0f19;
            --bg-glow: radial-gradient(circle at 50% -20%, rgba(59,130,246,0.08), transparent 60%);
            --surface: #131b2c;
            --surface2: #1e293b;
            --surface3: #253347;
            --border: rgba(255,255,255,0.06);
            --border-light: rgba(255,255,255,0.1);
            --text: #f1f5f9;
            --text-dim: #94a3b8;
            --text-muted: #64748b;
            --primary: #3b82f6;
            --primary-glow: rgba(59,130,246,0.15);
            --success: #10b981;
            --error: #ef4444;
            --warning: #f59e0b;
            --accent: #818cf8;
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            background-color: var(--bg);
            background-image: var(--bg-glow);
            color: var(--text);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            height: 100vh;
            overflow: hidden;
            -webkit-font-smoothing: antialiased;
        }

        /* ── Header ── */
        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 24px;
            height: 56px;
            background: rgba(19,27,44,0.8);
            backdrop-filter: blur(16px);
            border-bottom: 1px solid var(--border);
            z-index: 10;
            position: relative;
        }
        .header-brand {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 800;
            font-size: 16px;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, #60a5fa, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .pulse-dot {
            width: 8px; height: 8px;
            background: #60a5fa;
            border-radius: 50%;
            box-shadow: 0 0 8px #60a5fa;
            animation: pulse 2.5s infinite;
        }
        @keyframes pulse {
            0% { transform:scale(0.95); box-shadow:0 0 0 0 rgba(96,165,250,0.5); }
            70% { transform:scale(1); box-shadow:0 0 0 6px rgba(96,165,250,0); }
            100% { transform:scale(0.95); box-shadow:0 0 0 0 rgba(96,165,250,0); }
        }
        .header-stats {
            display: flex;
            gap: 20px;
            font-size: 12px;
            color: var(--text-dim);
            font-weight: 500;
        }
        .header-stats .val {
            color: var(--text);
            font-weight: 700;
            font-size: 13px;
        }

        /* ── Tabs ── */
        .tabs {
            display: flex;
            gap: 0;
            padding: 0 24px;
            background: var(--surface);
            border-bottom: 1px solid var(--border);
        }
        .tab {
            padding: 10px 20px;
            font-size: 13px;
            font-weight: 500;
            color: var(--text-dim);
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
            user-select: none;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .tab:hover { color: var(--text); }
        .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
        .tab svg { width:14px; height:14px; opacity:0.7; }
        .tab.active svg { opacity:1; }

        /* ── Panels ── */
        .panels { height: calc(100vh - 56px - 38px); position: relative; }
        .panel {
            position: absolute; inset: 0;
            opacity: 0; pointer-events: none;
            transition: opacity 0.35s ease;
        }
        .panel.active { opacity: 1; pointer-events: auto; }

        /* ── Shared ── */
        .map-tooltip {
            position: absolute;
            background: rgba(19,27,44,0.95);
            backdrop-filter: blur(16px);
            padding: 12px 16px;
            border-radius: 10px;
            border: 1px solid var(--border-light);
            font-size: 12px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.15s;
            max-width: 340px;
            z-index: 100;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            color: var(--text);
            line-height: 1.5;
        }
        .map-tooltip strong { color: var(--primary); }
        .map-legend {
            position: absolute;
            top: 16px; right: 16px;
            display: flex; flex-direction: column; gap: 8px;
            background: rgba(19,27,44,0.9);
            backdrop-filter: blur(16px);
            padding: 14px 18px;
            border-radius: 12px;
            border: 1px solid var(--border);
            font-size: 11px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.3);
        }
        .map-legend-item { display:flex; align-items:center; gap:8px; color: var(--text-dim); }
        .dot { width:8px; height:8px; border-radius:50%; }

        /* ═══ Story Map ═══ */
        #canvas { width:100%; height:100%; }
        .story-link { stroke-opacity:0.35; stroke-width:1px; }
        .story-node { cursor:pointer; transition: filter 0.2s; }
        .story-node:hover { filter: brightness(1.3); }

        /* ═══ ROI Dashboard ═══ */
        .roi-scroll { overflow-y:auto; height:100%; padding:28px 24px; }
        .roi-container { max-width:960px; margin:0 auto; }
        .roi-top { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }
        .roi-stat {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 20px;
            position: relative;
            overflow: hidden;
        }
        .roi-stat::before {
            content:'';
            position:absolute;
            top:0; left:0; right:0;
            height:2px;
        }
        .roi-stat.green::before { background: linear-gradient(90deg, var(--success), #34d399); }
        .roi-stat.blue::before { background: linear-gradient(90deg, var(--primary), #60a5fa); }
        .roi-stat.purple::before { background: linear-gradient(90deg, var(--accent), #a78bfa); }
        .roi-stat.amber::before { background: linear-gradient(90deg, var(--warning), #fbbf24); }
        .roi-stat-label {
            font-size:11px; font-weight:600;
            text-transform:uppercase; letter-spacing:0.6px;
            color: var(--text-dim);
            margin-bottom:8px;
        }
        .roi-stat-value {
            font-size:36px; font-weight:800;
            line-height:1; letter-spacing:-1.5px;
        }
        .roi-stat.green .roi-stat-value { color: var(--success); }
        .roi-stat.blue .roi-stat-value { color: var(--primary); }
        .roi-stat.purple .roi-stat-value { color: var(--accent); }
        .roi-stat.amber .roi-stat-value { color: var(--warning); }
        .roi-stat-sub { font-size:12px; color:var(--text-dim); margin-top:4px; }

        .roi-charts { display:grid; grid-template-columns:1.2fr 0.8fr; gap:14px; margin-bottom:20px; }
        .roi-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 20px;
        }
        .roi-card-title {
            font-size:11px; font-weight:600;
            text-transform:uppercase; letter-spacing:0.6px;
            color:var(--text-dim);
            margin-bottom:16px;
        }
        .roi-bar-row { display:flex; align-items:center; gap:10px; padding:7px 0; font-size:12px; }
        .roi-bar-label { width:110px; color:var(--text-dim); flex-shrink:0; font-weight:500; }
        .roi-bar-track { flex:1; height:8px; background:var(--surface2); border-radius:4px; overflow:hidden; }
        .roi-bar-fill { height:100%; border-radius:4px; transition:width 0.8s cubic-bezier(0.4,0,0.2,1); }
        .roi-bar-val { width:60px; text-align:right; color:var(--text); font-weight:600; font-size:12px; }

        /* Donut Chart */
        .roi-donut-wrap { display:flex; flex-direction:column; align-items:center; gap:16px; }
        .roi-donut-legend { width:100%; }
        .roi-donut-item { display:flex; align-items:center; gap:8px; padding:4px 0; font-size:12px; }
        .roi-donut-dot { width:10px; height:10px; border-radius:3px; flex-shrink:0; }
        .roi-donut-name { flex:1; color:var(--text-dim); }
        .roi-donut-val { font-weight:600; color:var(--text); }

        /* Area Chart */
        .roi-area-card { grid-column:1/-1; }
        .roi-area-chart { width:100%; height:140px; }

        /* ═══ Project Map ═══ */
        .map-split { display:flex; height:100%; }
        .map-graph-pane { flex:1.5; position:relative; background:var(--bg); border-right:1px solid var(--border); }
        .map-text-pane {
            flex:1; padding:24px 32px; overflow-y:auto;
            background:var(--surface); line-height:1.7;
        }
        #map-canvas { width:100%; height:100%; }
        #map-tree { width:100%; height:100%; display:none; }
        .map-graph-pane.tree-mode #map-canvas { display:none; }
        .map-graph-pane.tree-mode #map-tree { display:block; }
        .map-view-toggle {
            position:absolute; top:14px; left:14px; z-index:5;
            display:flex; gap:0; padding:3px;
            background:var(--surface); border:1px solid var(--border); border-radius:8px;
        }
        .map-view-btn {
            padding:5px 14px; font-size:11px; font-weight:600;
            font-family:'JetBrains Mono', monospace;
            background:transparent; color:var(--text-dim); border:none; cursor:pointer;
            border-radius:6px; transition:all 0.15s;
        }
        .map-view-btn:hover { color:var(--text); }
        .map-view-btn.active { background:var(--primary-glow); color:var(--primary); }
        .tree-link { fill:none; stroke:var(--border-light); stroke-width:1.2; stroke-opacity:0.5; }
        .tree-node-label { font-size:11px; font-family:'JetBrains Mono', monospace; fill:var(--text); }
        .tree-node-circle { stroke:var(--surface); stroke-width:2; cursor:pointer; transition:r 0.15s; }
        .tree-node-circle:hover { stroke:var(--text); }
        .map-text-pane h1 { font-size:20px; font-weight:700; margin-bottom:4px; color:var(--primary); }
        .map-text-pane h2 {
            font-size:12px; font-weight:600;
            text-transform:uppercase; letter-spacing:0.6px;
            color:var(--accent); margin-top:20px; margin-bottom:8px;
            padding-bottom:4px; border-bottom:1px solid var(--border);
        }
        .map-text-pane p,.map-text-pane li { font-size:13px; color:var(--text-dim); }
        .map-text-pane code { background:var(--surface2); padding:2px 6px; border-radius:4px; font-size:12px; color:var(--accent); }
        .map-text-pane ul { padding-left:20px; }
        .map-text-pane li { margin-bottom:3px; }
        .arch-node { cursor:pointer; transition:filter 0.2s; }
        .arch-node:hover { filter:brightness(1.3) drop-shadow(0 0 12px rgba(255,255,255,0.2)); }
        .arch-link { stroke:#475569; stroke-opacity:0.35; stroke-width:1.5px; }

        /* ═══ Timeline ═══ */
        .timeline-view { padding:24px; max-width:800px; margin:0 auto; overflow-y:auto; height:100%; }
        .tl-header { margin-bottom:20px; }
        .tl-activity { display:flex; gap:2px; align-items:flex-end; height:40px; margin-bottom:16px; padding:8px 0; }
        .tl-activity-bar {
            flex:1; min-width:3px; border-radius:2px 2px 0 0;
            background:var(--primary); opacity:0.6; transition:opacity 0.15s, height 0.3s;
        }
        .tl-activity-bar:hover { opacity:1; }
        .tl-filters { display:flex; gap:6px; flex-wrap:wrap; }
        .tl-filter {
            padding:5px 14px; font-size:11px; font-weight:500;
            border-radius:20px; cursor:pointer;
            border:1px solid var(--border); background:var(--surface);
            color:var(--text-dim); transition:all 0.2s;
        }
        .tl-filter.active { background:var(--primary-glow); border-color:var(--primary); color:var(--primary); }
        .tl-filter .count {
            display:inline-block;
            margin-left:4px; padding:1px 6px;
            background:rgba(255,255,255,0.06); border-radius:10px;
            font-size:10px; font-weight:600;
        }
        .tl-filter.active .count { background:rgba(59,130,246,0.2); }

        .tl-date-group { margin-top:20px; }
        .tl-date-label {
            font-size:11px; font-weight:600;
            color:var(--text-muted); text-transform:uppercase;
            letter-spacing:0.5px; padding-bottom:8px;
            border-bottom:1px solid var(--border);
            margin-bottom:4px;
        }
        .tl-item {
            display:flex; gap:12px; padding:10px 0;
            border-bottom:1px solid rgba(255,255,255,0.03);
            font-size:13px; transition:background 0.15s;
        }
        .tl-item:hover { background:rgba(255,255,255,0.015); margin:0 -8px; padding:10px 8px; border-radius:6px; }
        .tl-item:last-child { border-bottom:none; }
        .tl-badge {
            flex-shrink:0; width:70px; text-align:center;
            font-size:10px; font-weight:600; text-transform:uppercase;
            padding:4px 0; border-radius:4px; line-height:1.2; height:fit-content;
        }
        .tl-badge.issue { background:rgba(59,130,246,0.12); color:var(--primary); }
        .tl-badge.attempt { background:rgba(251,191,36,0.12); color:var(--warning); }
        .tl-badge.fix { background:rgba(16,185,129,0.12); color:var(--success); }
        .tl-badge.decision { background:rgba(129,140,248,0.12); color:var(--accent); }
        .tl-badge.note { background:rgba(100,116,139,0.1); color:var(--text-dim); }
        .tl-badge.backfill { background:rgba(100,116,139,0.08); color:var(--text-muted); }
        .tl-body { flex:1; }
        .tl-summary { color:var(--text); line-height:1.45; }
        .tl-meta { font-size:11px; color:var(--text-muted); margin-top:3px; }
        .tl-outcome-failed { color:var(--error); font-weight:600; }
        .tl-outcome-worked { color:var(--success); font-weight:600; }

        /* ── Auto-capture badge (Timeline) ── */
        .tl-auto-badge {
            display:inline-block; font-size:9px; font-weight:700;
            padding:1px 6px; border-radius:3px; margin-left:6px;
            background:rgba(99,102,241,0.12); color:#818cf8;
            text-transform:uppercase; letter-spacing:0.3px; vertical-align:middle;
        }
        .tl-capture-source { color:var(--text-muted); font-size:10px; font-style:italic; }

        /* ── Auto-capture stats (ROI) ── */
        .roi-capture-stats {
            display:grid; grid-template-columns:repeat(4,1fr); gap:12px;
            margin-bottom:20px;
        }
        .roi-capture-stat {
            background:var(--surface); border:1px solid var(--border);
            border-radius:10px; padding:14px 16px; text-align:center;
            border-top:3px solid var(--accent);
        }
        .roi-capture-stat.green { border-top-color:var(--success); }
        .roi-capture-stat.amber { border-top-color:var(--warning); }
        .roi-capture-stat.purple { border-top-color:var(--accent); }
        .roi-capture-stat.blue { border-top-color:var(--primary); }
        .roi-capture-stat-value { font-size:24px; font-weight:800; color:var(--text); }
        .roi-capture-stat-label { font-size:11px; color:var(--text-dim); margin-top:4px; font-weight:500; }

        /* ── Churn heatmap (ROI) ── */
        .churn-row {
            display:flex; align-items:center; gap:10px; padding:6px 0;
            border-bottom:1px solid rgba(255,255,255,0.03);
        }
        .churn-row:last-child { border-bottom:none; }
        .churn-file { flex:0 0 180px; font-size:11px; color:var(--text-dim); font-family:monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .churn-bar-track { flex:1; height:16px; background:var(--surface2); border-radius:3px; overflow:hidden; }
        .churn-bar-fill { height:100%; border-radius:3px; transition:width 0.6s ease; }
        .churn-bar-fill.high { background:var(--error); }
        .churn-bar-fill.medium { background:var(--warning); }
        .churn-bar-fill.low { background:var(--success); }
        .churn-count { flex:0 0 60px; font-size:11px; color:var(--text-dim); text-align:right; font-weight:600; }
        .churn-severity {
            flex:0 0 50px; font-size:9px; font-weight:700; text-align:center;
            padding:2px 6px; border-radius:3px; text-transform:uppercase;
        }
        .churn-severity.high { background:rgba(239,68,68,0.12); color:var(--error); }
        .churn-severity.medium { background:rgba(245,158,11,0.12); color:var(--warning); }
        .churn-severity.low { background:rgba(16,185,129,0.12); color:var(--success); }
        .churn-empty { color:var(--text-muted); font-size:12px; padding:16px; text-align:center; }

        /* ── Animated counter ── */
        @keyframes fadeInUp { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
        .animate-in { animation: fadeInUp 0.4s ease forwards; }
    </style>
</head>
<body>

    <!-- Header -->
    <div class="header">
        <div class="header-brand">
            <div class="pulse-dot"></div>
            projectmem
        </div>
        <div class="header-stats">
            <span>Tokens Saved: <span class="val" id="hdr-tokens">0</span></span>
            <span>Issues Resolved: <span class="val" id="hdr-issues">0</span></span>
            <span>Events: <span class="val" id="hdr-events">0</span></span>
            <span>Auto-captured: <span class="val" id="hdr-auto">0</span></span>
        </div>
    </div>

    <!-- Tabs -->
    <div class="tabs">
        <div class="tab active" data-panel="story">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg>
            Story Map
        </div>
        <div class="tab" data-panel="roi">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            ROI Dashboard
        </div>
        <div class="tab" data-panel="map">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="5" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><line x1="12" y1="8" x2="5" y2="16"/><line x1="12" y1="8" x2="19" y2="16"/></svg>
            Project Map
        </div>
        <div class="tab" data-panel="timeline">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
            Timeline
        </div>
    </div>

    <!-- Panels -->
    <div class="panels">

        <!-- Story Map -->
        <div class="panel active" id="panel-story">
            <svg id="canvas"></svg>
            <div class="map-legend">
                <div class="map-legend-item"><div class="dot" style="background:var(--primary)"></div> Issue / Event</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--success)"></div> Fix</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--error)"></div> Failed Attempt</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--warning)"></div> File (warm = more failures)</div>
                <div class="map-legend-item"><div class="dot" style="background:var(--accent)"></div> Decision</div>
                <div class="map-legend-item" style="margin-top:4px;font-size:10px;color:var(--text-muted)">Scroll to zoom &middot; Drag to pan</div>
            </div>
            <div class="map-tooltip" id="tooltip"></div>
        </div>

        <!-- ROI Dashboard -->
        <div class="panel" id="panel-roi">
            <div class="roi-scroll">
                <div class="roi-container">
                    <div class="roi-top" id="roi-top"></div>
                    <!-- Auto-Capture Stats -->
                    <div class="roi-capture-stats" id="roi-capture-stats"></div>
                    <div class="roi-charts">
                        <div class="roi-card">
                            <div class="roi-card-title">Token Savings Breakdown</div>
                            <div id="roi-bars"></div>
                        </div>
                        <div class="roi-card">
                            <div class="roi-card-title">Distribution</div>
                            <div class="roi-donut-wrap">
                                <svg id="roi-donut" width="160" height="160"></svg>
                                <div class="roi-donut-legend" id="roi-donut-legend"></div>
                            </div>
                        </div>
                    </div>
                    <!-- Capture Source Donut -->
                    <div class="roi-charts">
                        <div class="roi-card">
                            <div class="roi-card-title">Capture Sources</div>
                            <div class="roi-donut-wrap">
                                <svg id="roi-source-donut" width="160" height="160"></svg>
                                <div class="roi-donut-legend" id="roi-source-legend"></div>
                            </div>
                        </div>
                        <div class="roi-card">
                            <div class="roi-card-title">File Churn (Top 10)</div>
                            <div id="roi-churn"></div>
                        </div>
                    </div>
                    <div class="roi-card roi-area-card">
                        <div class="roi-card-title">Cumulative Savings Over Time</div>
                        <svg class="roi-area-chart" id="roi-area"></svg>
                    </div>
                </div>
            </div>
        </div>

        <!-- Project Map -->
        <div class="panel" id="panel-map">
            <div class="map-split">
                <div class="map-graph-pane">
                    <div class="map-view-toggle">
                        <button class="map-view-btn active" data-view="tree">Tree</button>
                        <button class="map-view-btn" data-view="graph">Graph</button>
                    </div>
                    <svg id="map-canvas"></svg>
                    <svg id="map-tree"></svg>
                    <div class="map-legend">
                        <div class="map-legend-item"><div class="dot" style="background:var(--accent)"></div> Folder</div>
                        <div class="map-legend-item"><div class="dot" style="background:var(--primary)"></div> File / Module</div>
                        <div class="map-legend-item" style="color:var(--text-muted);margin-top:4px;font-size:10px;">Hover for full paths</div>
                    </div>
                </div>
                <div class="map-text-pane" id="map-content"></div>
            </div>
        </div>

        <!-- Timeline -->
        <div class="panel" id="panel-timeline">
            <div class="timeline-view">
                <div class="tl-header">
                    <div class="tl-activity" id="tl-activity"></div>
                    <div class="tl-filters" id="tl-filters"></div>
                </div>
                <div id="tl-list"></div>
            </div>
        </div>
    </div>

    <script>
    // ── Data Injection ──
    const data = {{GRAPH_DATA}};
    const projectMap = {{PROJECT_MAP}};
    const projectMapGraph = {{PROJECT_MAP_GRAPH}};
    const timelineData = {{TIMELINE_DATA}};

    // ── Animated Counter ──
    function animateValue(el, end, prefix='', suffix='', duration=800) {
        let start = 0;
        const startTime = performance.now();
        function step(now) {
            const progress = Math.min((now - startTime) / duration, 1);
            const ease = 1 - Math.pow(1 - progress, 3);
            const current = Math.floor(ease * end);
            el.textContent = prefix + current.toLocaleString() + suffix;
            if (progress < 1) requestAnimationFrame(step);
            else el.textContent = prefix + end.toLocaleString() + suffix;
        }
        requestAnimationFrame(step);
    }

    // ── Tab Navigation ──
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById('panel-' + tab.dataset.panel).classList.add('active');
        });
    });

    // ── Header Stats ──
    const resolvedCount = timelineData.filter(e => e.type === 'fix').length;
    const autoCount = timelineData.filter(e => e.auto_captured).length;
    const manualCount = timelineData.length - autoCount;
    animateValue(document.getElementById('hdr-tokens'), data.stats.total_tokens);
    animateValue(document.getElementById('hdr-issues'), resolvedCount);
    animateValue(document.getElementById('hdr-events'), timelineData.length);
    animateValue(document.getElementById('hdr-auto'), autoCount);

    // ══════════════════════════════════════════
    // TAB 1: Story Map — Filter noise, add glow
    // ══════════════════════════════════════════
    const NOISE = /__pycache__|\\.pyc$|\\.DS_Store|\\.egg-info/;
    const cleanNodes = data.nodes.filter(n => !NOISE.test(n.id));
    const cleanNodeIds = new Set(cleanNodes.map(n => n.id));
    const cleanLinks = data.links.filter(l => {
        const s = typeof l.source === 'object' ? l.source.id : l.source;
        const t = typeof l.target === 'object' ? l.target.id : l.target;
        return cleanNodeIds.has(s) && cleanNodeIds.has(t);
    });

    const svg = d3.select("#canvas");
    const width = window.innerWidth;
    const height = window.innerHeight - 94;
    const g = svg.append("g");

    // Glow filter
    const defs = svg.append("defs");
    const glow = defs.append("filter").attr("id","glow");
    glow.append("feGaussianBlur").attr("stdDeviation","3").attr("result","blur");
    glow.append("feMerge").selectAll("feMergeNode")
        .data(["blur","SourceGraphic"]).enter()
        .append("feMergeNode").attr("in", d=>d);

    svg.call(d3.zoom().scaleExtent([0.3,5]).on("zoom", e => g.attr("transform", e.transform)));

    const sim = d3.forceSimulation(cleanNodes)
        .force("link", d3.forceLink(cleanLinks).id(d=>d.id).distance(d => {
            const s = typeof d.source === 'object' ? d.source : cleanNodes.find(n=>n.id===d.source);
            return (s && s.type === 'event') ? 60 : 90;
        }))
        .force("charge", d3.forceManyBody().strength(-250))
        .force("center", d3.forceCenter(width/2, height/2))
        .force("collision", d3.forceCollide(12));

    const link = g.append("g").selectAll("line")
        .data(cleanLinks).enter().append("line")
        .attr("class","story-link")
        .attr("stroke", d => {
            const s = typeof d.source === 'object' ? d.source : cleanNodes.find(n=>n.id===d.source);
            if (!s) return '#334155';
            if (s.outcome === 'failed') return '#ef444480';
            if (s.event_type === 'fix') return '#10b98160';
            return '#33415580';
        });

    function nodeColor(d) {
        if (d.type === 'file') {
            const heat = Math.min((d.failures||0)/5, 1);
            return d3.interpolate("#475569","#f87171")(heat);
        }
        if (d.event_type==='fix') return '#10b981';
        if (d.outcome==='failed') return '#ef4444';
        if (d.event_type==='decision') return '#818cf8';
        return '#3b82f6';
    }
    function nodeRadius(d) {
        if (d.type==='event') {
            if (d.event_type==='fix' || d.outcome==='failed') return 8;
            return 6;
        }
        return 5 + Math.min((d.failures||0), 4);
    }

    const node = g.append("g").selectAll("circle")
        .data(cleanNodes).enter().append("circle")
        .attr("class","story-node")
        .attr("r", nodeRadius)
        .attr("fill", d => d.auto_captured ? nodeColor(d)+'99' : nodeColor(d))
        .attr("stroke", d => d.type==='event' ? nodeColor(d) : '#0f172a')
        .attr("stroke-width", d => d.type==='event' ? 2 : 1)
        .attr("stroke-opacity", d => d.type==='event' ? 0.3 : 1)
        .attr("stroke-dasharray", d => d.auto_captured ? "3,2" : null)
        .attr("filter", d => (d.type==='event' && (d.event_type==='fix' || d.outcome==='failed')) ? "url(#glow)" : null)
        .call(d3.drag()
            .on("start", e => { if(!e.active) sim.alphaTarget(0.3).restart(); e.subject.fx=e.subject.x; e.subject.fy=e.subject.y; })
            .on("drag", e => { e.subject.fx=e.x; e.subject.fy=e.y; })
            .on("end", e => { if(!e.active) sim.alphaTarget(0); e.subject.fx=null; e.subject.fy=null; }));

    const labels = g.append("g").selectAll("text")
        .data(cleanNodes.filter(d => d.type==='event' || (d.failures||0)>0))
        .enter().append("text")
        .attr("font-size","10px")
        .attr("fill", d => d.type==='event' ? '#94a3b8' : '#64748b')
        .attr("dx",12).attr("dy",".35em")
        .text(d => d.label);

    node.on("mouseover", (event,d) => {
        const tt = document.getElementById("tooltip");
        tt.style.opacity=1;
        const typeLabel = d.event_type ? d.event_type.toUpperCase() : (d.type||'').toUpperCase();
        const details = d.summary || d.full_path || '';
        const outcome = d.outcome ? '<br/><span style="color:'+(d.outcome==='failed'?'var(--error)':'var(--success)')+'">Outcome: '+d.outcome+'</span>' : '';
        const loc = d.location ? '<br/><span style="color:var(--accent)">@ '+d.location+'</span>' : '';
        tt.innerHTML = '<strong>'+typeLabel+': '+d.label+'</strong><br/>'+details+outcome+loc;
        tt.style.left=(event.pageX+14)+"px";
        tt.style.top=(event.pageY-14)+"px";
    }).on("mouseout", () => { document.getElementById("tooltip").style.opacity=0; });

    sim.on("tick", () => {
        link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
        node.attr("cx",d=>d.x).attr("cy",d=>d.y);
        labels.attr("x",d=>d.x).attr("y",d=>d.y);
    });

    // ══════════════════════════════════════════
    // TAB 2: ROI Dashboard — Full overhaul
    // ══════════════════════════════════════════
    const stats = data.stats;
    const evtCount = timelineData.length;
    const sessionsEstimate = Math.max(Math.ceil(evtCount / 4), 1);
    const avgPerSession = evtCount > 0 ? Math.round(stats.total_tokens / sessionsEstimate) : 0;
    const monthlyProjected = stats.total_tokens * 4;

    // Top stat cards
    const topCards = [
        { label:'Total Tokens Saved', value:stats.total_tokens, prefix:'', suffix:'', color:'green' },
        { label:'Estimated USD Saved', value:stats.usd_saved, prefix:'$', suffix:'', color:'purple', decimal:true },
        { label:'Memory Events', value:evtCount, prefix:'', suffix:'', color:'blue' },
        { label:'Monthly Projection', value:monthlyProjected, prefix:'', suffix:'', color:'amber' },
    ];
    const topEl = document.getElementById('roi-top');
    topCards.forEach((c,i) => {
        const card = document.createElement('div');
        card.className = 'roi-stat ' + c.color + ' animate-in';
        card.style.animationDelay = (i*0.08)+'s';
        card.innerHTML = '<div class="roi-stat-label">'+c.label+'</div><div class="roi-stat-value" id="roi-v-'+i+'">0</div><div class="roi-stat-sub">'+(i===3?'Projected at current pace':'Since project start')+'</div>';
        topEl.appendChild(card);
        setTimeout(() => {
            const el = document.getElementById('roi-v-'+i);
            if (c.decimal) animateValue(el, Math.round(c.value*100)/100, c.prefix, '', 1000);
            else animateValue(el, c.value, c.prefix, c.suffix, 1000);
        }, 200 + i*100);
    });
    // Fix USD display
    setTimeout(() => {
        const usdEl = document.getElementById('roi-v-1');
        if (usdEl) usdEl.textContent = '$' + stats.usd_saved.toFixed(2);
    }, 1400);

    // ── Auto-Capture Stats Row ──
    const captureRate = timelineData.length > 0 ? Math.round(autoCount / timelineData.length * 100) : 0;
    const captureCards = [
        { label:'Manual Events', value:manualCount, color:'blue' },
        { label:'Auto-captured', value:autoCount, color:'purple' },
        { label:'Would Be Lost', value:autoCount, color:'amber' },
        { label:'Auto-capture Rate', value:captureRate, color:'green', suffix:'%' },
    ];
    const captureEl = document.getElementById('roi-capture-stats');
    captureCards.forEach((c,i) => {
        const card = document.createElement('div');
        card.className = 'roi-capture-stat ' + c.color + ' animate-in';
        card.style.animationDelay = (i*0.08)+'s';
        card.innerHTML = '<div class="roi-capture-stat-value" id="cap-v-'+i+'">0</div><div class="roi-capture-stat-label">'+c.label+'</div>';
        captureEl.appendChild(card);
        setTimeout(() => {
            animateValue(document.getElementById('cap-v-'+i), c.value, '', c.suffix||'', 800);
        }, 300+i*100);
    });

    // ── Capture Source Donut ──
    const sourceMap = {};
    timelineData.forEach(e => {
        const src = e.auto_captured ? (e.capture_source || 'auto_unknown') : 'manual';
        sourceMap[src] = (sourceMap[src]||0) + 1;
    });
    const srcData = Object.entries(sourceMap).filter(([,v])=>v>0);
    const srcColors = { manual:'#3b82f6', git_post_commit:'#818cf8', git_post_revert:'#ef4444', git_post_merge:'#10b981', churn_detector:'#f59e0b', ci_parser:'#ec4899', auto_unknown:'#64748b' };
    const srcTotal = srcData.reduce((s,[,v])=>s+v, 0);
    if (srcData.length > 0) {
        const srcPie = d3.pie().value(d=>d[1]).sort(null).padAngle(0.03);
        const srcArc = d3.arc().innerRadius(50).outerRadius(72);
        const srcSvg = d3.select("#roi-source-donut")
            .attr("viewBox","0 0 160 160")
            .append("g").attr("transform","translate(80,80)");
        srcSvg.selectAll("path")
            .data(srcPie(srcData)).enter().append("path")
            .attr("d", srcArc)
            .attr("fill",(d)=>srcColors[d.data[0]]||'#64748b')
            .attr("stroke","var(--surface)").attr("stroke-width",2)
            .style("opacity",0)
            .transition().duration(600).delay((d,i)=>i*100)
            .style("opacity",1)
            .attrTween("d", function(d) {
                const interp = d3.interpolate({startAngle:d.startAngle,endAngle:d.startAngle}, d);
                return t => srcArc(interp(t));
            });
        srcSvg.append("text").attr("text-anchor","middle").attr("dy","-0.2em")
            .attr("fill","var(--text)").attr("font-size","18px").attr("font-weight","800")
            .text(srcTotal);
        srcSvg.append("text").attr("text-anchor","middle").attr("dy","1.2em")
            .attr("fill","var(--text-dim)").attr("font-size","10px").text("events");
        const srcLegend = document.getElementById('roi-source-legend');
        const srcLabels = { manual:'Manual', git_post_commit:'Git Commits', git_post_revert:'Git Reverts', git_post_merge:'Git Merges', churn_detector:'Churn Alerts', ci_parser:'CI Results', auto_unknown:'Auto (other)' };
        srcData.forEach(([key,val]) => {
            const pct = ((val/srcTotal)*100).toFixed(0);
            srcLegend.innerHTML += '<div class="roi-donut-item"><div class="roi-donut-dot" style="background:'+(srcColors[key]||'#64748b')+'"></div><div class="roi-donut-name">'+(srcLabels[key]||key)+'</div><div class="roi-donut-val">'+pct+'%</div></div>';
        });
    }

    // ── File Churn Heatmap ──
    const fileChanges = {};
    timelineData.forEach(e => {
        if (e.location) {
            const f = e.location.split(':')[0];
            fileChanges[f] = (fileChanges[f]||0) + 1;
        }
    });
    // Also count from graph data file nodes
    data.nodes.forEach(n => {
        if (n.type === 'file' && n.failures > 0) {
            fileChanges[n.id] = (fileChanges[n.id]||0) + n.failures;
        }
    });
    const churnEl = document.getElementById('roi-churn');
    const churnEntries = Object.entries(fileChanges).sort((a,b)=>b[1]-a[1]).slice(0,10);
    if (churnEntries.length === 0) {
        churnEl.innerHTML = '<div class="churn-empty">No file activity tracked yet</div>';
    } else {
        const maxChurn = Math.max(...churnEntries.map(d=>d[1]), 1);
        churnEntries.forEach(([file,count],i) => {
            const severity = count >= 8 ? 'high' : count >= 4 ? 'medium' : 'low';
            const pct = (count/maxChurn*100).toFixed(0);
            const shortFile = file.split('/').slice(-2).join('/');
            const row = document.createElement('div');
            row.className = 'churn-row animate-in';
            row.style.animationDelay = (0.2+i*0.05)+'s';
            row.innerHTML = '<div class="churn-file" title="'+file+'">'+shortFile+'</div><div class="churn-bar-track"><div class="churn-bar-fill '+severity+'" style="width:0%"></div></div><div class="churn-count">'+count+'</div><div class="churn-severity '+severity+'">'+severity+'</div>';
            churnEl.appendChild(row);
            setTimeout(() => { row.querySelector('.churn-bar-fill').style.width = pct+'%'; }, 400+i*60);
        });
    }

    // Bar chart
    const barColors = {
        issue:'#3b82f6', attempt_failed:'#ef4444', attempt_worked:'#10b981',
        fix:'#10b981', decision:'#818cf8', note:'#64748b', backfill:'#475569'
    };
    const barsEl = document.getElementById('roi-bars');
    const maxTokens = Math.max(...Object.values(stats.breakdown), 1);
    Object.entries(stats.breakdown).forEach(([key,val],i) => {
        if (val===0) return;
        const pct = (val/maxTokens*100).toFixed(1);
        const row = document.createElement('div');
        row.className = 'roi-bar-row animate-in';
        row.style.animationDelay = (0.3+i*0.06)+'s';
        row.innerHTML = '<div class="roi-bar-label">'+key.replace(/_/g,' ')+'</div><div class="roi-bar-track"><div class="roi-bar-fill" style="width:0%;background:'+(barColors[key]||'#3b82f6')+'"></div></div><div class="roi-bar-val">'+val.toLocaleString()+'</div>';
        barsEl.appendChild(row);
        setTimeout(() => { row.querySelector('.roi-bar-fill').style.width = pct+'%'; }, 400+i*80);
    });

    // Donut chart
    const donutData = Object.entries(stats.breakdown).filter(([,v])=>v>0);
    const donutColors = donutData.map(([k])=>barColors[k]||'#3b82f6');
    const total = donutData.reduce((s,[,v])=>s+v, 0);
    const pie = d3.pie().value(d=>d[1]).sort(null).padAngle(0.03);
    const arc = d3.arc().innerRadius(50).outerRadius(72);
    const donutSvg = d3.select("#roi-donut")
        .attr("viewBox","0 0 160 160")
        .append("g").attr("transform","translate(80,80)");
    donutSvg.selectAll("path")
        .data(pie(donutData)).enter().append("path")
        .attr("d", arc)
        .attr("fill", (d,i)=>donutColors[i])
        .attr("stroke","var(--surface)").attr("stroke-width",2)
        .style("opacity",0)
        .transition().duration(600).delay((d,i)=>i*100)
        .style("opacity",1)
        .attrTween("d", function(d) {
            const interp = d3.interpolate({startAngle:d.startAngle,endAngle:d.startAngle}, d);
            return t => arc(interp(t));
        });
    // Center label
    donutSvg.append("text").attr("text-anchor","middle").attr("dy","-0.2em")
        .attr("fill","var(--text)").attr("font-size","18px").attr("font-weight","800")
        .text(total.toLocaleString());
    donutSvg.append("text").attr("text-anchor","middle").attr("dy","1.2em")
        .attr("fill","var(--text-dim)").attr("font-size","10px")
        .text("tokens");

    // Donut legend
    const legendEl = document.getElementById('roi-donut-legend');
    donutData.forEach(([key,val],i) => {
        const pct = ((val/total)*100).toFixed(0);
        legendEl.innerHTML += '<div class="roi-donut-item"><div class="roi-donut-dot" style="background:'+donutColors[i]+'"></div><div class="roi-donut-name">'+key.replace(/_/g,' ')+'</div><div class="roi-donut-val">'+pct+'%</div></div>';
    });

    // Area chart — cumulative savings over time
    const areaSvg = d3.select("#roi-area");
    const aRect = document.getElementById('roi-area').getBoundingClientRect();
    const aW = aRect.width || 900, aH = 140;
    areaSvg.attr("viewBox", "0 0 "+aW+" "+aH);

    const sortedEvents = [...timelineData].sort((a,b) => new Date(a.timestamp)-new Date(b.timestamp));
    const tokenMap = { issue:2000, attempt:2000, fix:4000, decision:3000, note:1000, backfill:500 };
    let cumulative = 0;
    const areaData = sortedEvents.map((e,i) => {
        cumulative += (tokenMap[e.type]||1000);
        return { x:i, y:cumulative, type:e.type };
    });
    if (areaData.length > 0) {
        const xScale = d3.scaleLinear().domain([0,areaData.length-1]).range([40,aW-16]);
        const yScale = d3.scaleLinear().domain([0,d3.max(areaData,d=>d.y)*1.1]).range([aH-24,8]);

        // Grid lines
        const yTicks = yScale.ticks(4);
        yTicks.forEach(t => {
            areaSvg.append("line").attr("x1",40).attr("x2",aW-16).attr("y1",yScale(t)).attr("y2",yScale(t))
                .attr("stroke","var(--border)").attr("stroke-dasharray","2,4");
            areaSvg.append("text").attr("x",4).attr("y",yScale(t)+4)
                .attr("fill","var(--text-muted)").attr("font-size","9px").text((t/1000).toFixed(0)+'K');
        });

        // Area
        const area = d3.area().x(d=>xScale(d.x)).y0(aH-24).y1(d=>yScale(d.y)).curve(d3.curveMonotoneX);
        const grad = areaSvg.append("defs").append("linearGradient").attr("id","aGrad").attr("x1",0).attr("y1",0).attr("x2",0).attr("y2",1);
        grad.append("stop").attr("offset","0%").attr("stop-color","var(--primary)").attr("stop-opacity",0.3);
        grad.append("stop").attr("offset","100%").attr("stop-color","var(--primary)").attr("stop-opacity",0.02);
        areaSvg.append("path").datum(areaData).attr("fill","url(#aGrad)").attr("d",area);

        // Line
        const line = d3.line().x(d=>xScale(d.x)).y(d=>yScale(d.y)).curve(d3.curveMonotoneX);
        const path = areaSvg.append("path").datum(areaData)
            .attr("fill","none").attr("stroke","var(--primary)").attr("stroke-width",2).attr("d",line);
        const pathLen = path.node().getTotalLength();
        path.attr("stroke-dasharray",pathLen).attr("stroke-dashoffset",pathLen)
            .transition().duration(1200).ease(d3.easeCubicOut).attr("stroke-dashoffset",0);

        // Dots for key events
        areaSvg.selectAll(".area-dot")
            .data(areaData.filter(d=>d.type==='fix'||d.type==='issue'))
            .enter().append("circle")
            .attr("cx",d=>xScale(d.x)).attr("cy",d=>yScale(d.y))
            .attr("r",3)
            .attr("fill",d=>d.type==='fix'?'var(--success)':'var(--primary)')
            .style("opacity",0)
            .transition().delay(1200).duration(300).style("opacity",1);
    }

    // ══════════════════════════════════════════
    // TAB 3: Project Map
    // ══════════════════════════════════════════
    function renderMarkdown(md) {
        return md
            .replace(/^### (.+)$/gm, '<h3>$1</h3>')
            .replace(/^## (.+)$/gm, '<h2>$1</h2>')
            .replace(/^# (.+)$/gm, '<h1>$1</h1>')
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
            .replace(/^- (.+)$/gm, '<li>$1</li>')
            .replace(/(<li>.*<\\/li>)/s, '<ul>$1</ul>')
            .replace(/^(?!<[hul])(\\S.*)$/gm, '<p>$1</p>')
            .replace(/\\n\\n/g, '')
            .replace(/((?:<li>[^]*?<\\/li>\\s*)+)/g, '<ul>$1</ul>');
    }
    document.getElementById('map-content').innerHTML = renderMarkdown(projectMap);

    if (projectMapGraph.nodes.length > 0) {
        const mSvg = d3.select("#map-canvas");
        const mW = window.innerWidth * 0.6;
        const mH = window.innerHeight - 56;
        const mG = mSvg.append("g");

        mSvg.call(d3.zoom().scaleExtent([0.5,4]).on("zoom", e => mG.attr("transform",e.transform)));

        // Arrow marker
        const mDefs = mSvg.append("defs");
        mDefs.append("marker").attr("id","arrow").attr("viewBox","0 -5 10 10")
            .attr("refX",18).attr("refY",0).attr("markerWidth",6).attr("markerHeight",6).attr("orient","auto")
            .append("path").attr("fill","#475569").attr("d","M0,-5L10,0L0,5");
        // Glow
        const mGlow = mDefs.append("filter").attr("id","mglow");
        mGlow.append("feGaussianBlur").attr("stdDeviation","2.5").attr("result","blur");
        mGlow.append("feMerge").selectAll("feMergeNode").data(["blur","SourceGraphic"]).enter().append("feMergeNode").attr("in",d=>d);

        const mSim = d3.forceSimulation(projectMapGraph.nodes)
            .force("link", d3.forceLink(projectMapGraph.links).id(d=>d.id).distance(130))
            .force("charge", d3.forceManyBody().strength(-500))
            .force("center", d3.forceCenter(mW/2, mH/2));

        const mLink = mG.append("g").selectAll("line")
            .data(projectMapGraph.links).enter().append("line")
            .attr("class","arch-link").attr("marker-end","url(#arrow)");

        const mNode = mG.append("g").selectAll("circle")
            .data(projectMapGraph.nodes).enter().append("circle")
            .attr("class","arch-node")
            .attr("r", d=>d.type==='folder'?12:7)
            .attr("fill", d=>d.type==='folder'?'var(--accent)':'var(--primary)')
            .attr("stroke", d=>d.type==='folder'?'var(--accent)':'var(--primary)')
            .attr("stroke-width", d=>d.type==='folder'?3:1.5)
            .attr("stroke-opacity",0.25)
            .attr("filter","url(#mglow)")
            .call(d3.drag()
                .on("start", e => { if(!e.active) mSim.alphaTarget(0.3).restart(); e.subject.fx=e.subject.x; e.subject.fy=e.subject.y; })
                .on("drag", e => { e.subject.fx=e.x; e.subject.fy=e.y; })
                .on("end", e => { if(!e.active) mSim.alphaTarget(0); e.subject.fx=null; e.subject.fy=null; }));

        const mLabels = mG.append("g").selectAll("text")
            .data(projectMapGraph.nodes).enter().append("text")
            .attr("font-size", d=>d.type==='folder'?'12px':'11px')
            .attr("font-weight", d=>d.type==='folder'?'600':'400')
            .attr("fill", d=>d.type==='folder'?'#c4b5fd':'#e2e8f0')
            .attr("dx",16).attr("dy",".35em")
            .text(d=>d.label);

        mNode.on("mouseover", (event,d) => {
            const tt = document.getElementById("tooltip");
            tt.style.opacity=1;
            tt.innerHTML = '<strong>'+d.type.toUpperCase()+'</strong><br/>'+d.full_path;
            tt.style.left=(event.pageX+14)+"px";
            tt.style.top=(event.pageY-14)+"px";
        }).on("mouseout", () => { document.getElementById("tooltip").style.opacity=0; });

        mSim.on("tick", () => {
            mLink.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
            mNode.attr("cx",d=>d.x).attr("cy",d=>d.y);
            mLabels.attr("x",d=>d.x).attr("y",d=>d.y);
        });
    }

    // ══════════════════════════════════════════
    // TAB 3b: Project Map — Tree / Dendrogram view
    // ══════════════════════════════════════════
    function buildTreeData() {
        if (!projectMapGraph.nodes.length) return null;
        // Build a 3-level hierarchy: root → top-folder → leaf
        const root = { id: '__root__', label: 'project', children: [] };
        const groups = {};
        const palette = ['#3b82f6','#8b5cf6','#10b981','#f59e0b','#ec4899','#06b6d4','#84cc16','#ef4444','#a78bfa','#22d3ee','#fb7185','#4ade80'];
        let colorIdx = 0;

        projectMapGraph.nodes.forEach(n => {
            const id = n.id;
            const parts = id.split('/').filter(Boolean);
            const top = parts[0] || '(root)';
            if (!groups[top]) {
                groups[top] = { id: 'g_'+top, label: top, color: palette[(colorIdx++) % palette.length], children: [] };
                root.children.push(groups[top]);
            }
            // Only add leaf (file or last segment) — skip if it's the folder itself
            if (n.type !== 'folder' || parts.length > 1) {
                const leafLabel = n.label || parts[parts.length-1];
                groups[top].children.push({ id, label: leafLabel, color: groups[top].color, full: id });
            }
        });
        // Sort & cap each group at 25 leaves to avoid runaway trees
        Object.values(groups).forEach(g => {
            g.children.sort((a,b) => a.label.localeCompare(b.label));
            if (g.children.length > 25) {
                const more = g.children.length - 24;
                g.children = g.children.slice(0, 24);
                g.children.push({ id: 'more_'+g.label, label: `+${more} more`, color: g.color, more: true });
            }
        });
        root.children.sort((a,b) => a.label.localeCompare(b.label));
        return root;
    }

    function renderTree() {
        const treeData = buildTreeData();
        const tSvg = d3.select("#map-tree");
        tSvg.selectAll("*").remove();
        if (!treeData) {
            tSvg.append("text").attr("x","50%").attr("y","50%").attr("text-anchor","middle")
                .attr("fill","var(--text-muted)").attr("font-size","13px")
                .text("PROJECT_MAP.md is empty — no tree to render.");
            return;
        }
        const pane = document.querySelector('.map-graph-pane');
        const W = pane.clientWidth || 800;
        const H = pane.clientHeight || 600;
        const root = d3.hierarchy(treeData);
        const leafCount = root.leaves().length;
        // Vertical space per leaf, fixed min/max
        const innerH = Math.max(H - 40, leafCount * 22);
        const cluster = d3.cluster().size([innerH, W - 260]);
        cluster(root);

        tSvg.attr("viewBox", `0 0 ${W} ${innerH + 40}`)
            .style("cursor","grab");
        const zoomG = tSvg.append("g");
        const g = zoomG.append("g").attr("transform","translate(120,20)");

        // Zoom + pan
        const zoom = d3.zoom()
            .scaleExtent([0.3, 5])
            .on("zoom", (e) => zoomG.attr("transform", e.transform))
            .on("start", () => tSvg.style("cursor","grabbing"))
            .on("end", () => tSvg.style("cursor","grab"));
        tSvg.call(zoom).on("dblclick.zoom", () => tSvg.transition().duration(400).call(zoom.transform, d3.zoomIdentity));

        // Links — bezier curves
        const linkGen = d3.linkHorizontal().x(d=>d.y).y(d=>d.x);
        g.append("g").selectAll("path")
            .data(root.links()).enter().append("path")
            .attr("class","tree-link")
            .attr("d", linkGen);

        // Nodes
        const node = g.append("g").selectAll("g")
            .data(root.descendants()).enter().append("g")
            .attr("transform", d => `translate(${d.y},${d.x})`);

        node.append("circle")
            .attr("class","tree-node-circle")
            .attr("r", d => d.depth === 0 ? 5 : d.depth === 1 ? 6 : 4)
            .attr("fill", d => d.data.color || (d.depth === 0 ? '#64748b' : 'var(--primary)'));

        node.append("text")
            .attr("class","tree-node-label")
            .attr("dy","0.32em")
            .attr("x", d => d.children ? -10 : 10)
            .attr("text-anchor", d => d.children ? "end" : "start")
            .attr("fill", d => d.data.more ? "var(--text-muted)" : "var(--text)")
            .style("font-style", d => d.data.more ? "italic" : "normal")
            .text(d => d.depth === 0 ? "" : d.data.label);

        // Tooltip on full paths
        node.on("mouseover", (e,d) => {
            if (!d.data.full) return;
            const tt = document.getElementById("tooltip");
            tt.style.opacity = 1;
            tt.innerHTML = '<strong>'+d.data.label+'</strong><br/>'+d.data.full;
            tt.style.left = (e.pageX + 14) + "px";
            tt.style.top = (e.pageY - 14) + "px";
        }).on("mouseout", () => { document.getElementById("tooltip").style.opacity = 0; });
    }

    // View toggle
    const mapPane = document.querySelector('.map-graph-pane');
    let treeRendered = false;
    mapPane.classList.add('tree-mode');
    if (!treeRendered) { renderTree(); treeRendered = true; }
    document.querySelectorAll('.map-view-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.map-view-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            if (btn.dataset.view === 'tree') {
                mapPane.classList.add('tree-mode');
                if (!treeRendered) { renderTree(); treeRendered = true; }
            } else {
                mapPane.classList.remove('tree-mode');
            }
        });
    });
    window.addEventListener('resize', () => {
        if (mapPane.classList.contains('tree-mode')) renderTree();
    });

    // ══════════════════════════════════════════
    // TAB 4: Timeline — Grouped + Activity Chart
    // ══════════════════════════════════════════
    const eventTypes = [...new Set(timelineData.map(e=>e.type))];
    const activeFilters = new Set([...eventTypes, '_all']);
    let captureFilter = 'all'; // 'all', 'manual', 'auto'
    const filtersEl = document.getElementById('tl-filters');
    const listEl = document.getElementById('tl-list');

    // Count per type
    const typeCounts = {};
    timelineData.forEach(e => { typeCounts[e.type] = (typeCounts[e.type]||0)+1; });

    // Source filter pills (Manual / Auto)
    if (autoCount > 0) {
        const sourceFilters = [
            { key:'all', label:'All', count:timelineData.length },
            { key:'manual', label:'Manual', count:manualCount },
            { key:'auto', label:'Auto-captured', count:autoCount },
        ];
        sourceFilters.forEach(sf => {
            const btn = document.createElement('div');
            btn.className = 'tl-filter' + (sf.key==='all'?' active':'');
            btn.style.borderColor = sf.key==='auto'?'#818cf8':sf.key==='manual'?'#3b82f6':'';
            btn.innerHTML = sf.label + ' <span class="count">' + sf.count + '</span>';
            btn.addEventListener('click', () => {
                captureFilter = sf.key;
                filtersEl.querySelectorAll('.tl-filter-source').forEach(b=>b.classList.remove('active'));
                btn.classList.add('active');
                renderTimeline();
            });
            btn.classList.add('tl-filter-source');
            if (sf.key==='all') btn.classList.add('active');
            filtersEl.appendChild(btn);
        });
        // Separator
        const sep = document.createElement('div');
        sep.style.cssText = 'width:1px;height:20px;background:var(--border);margin:0 4px;';
        filtersEl.appendChild(sep);
    }

    eventTypes.forEach(type => {
        const btn = document.createElement('div');
        btn.className = 'tl-filter active';
        btn.innerHTML = type + ' <span class="count">' + (typeCounts[type]||0) + '</span>';
        btn.addEventListener('click', () => {
            if (activeFilters.has(type)) { activeFilters.delete(type); btn.classList.remove('active'); }
            else { activeFilters.add(type); btn.classList.add('active'); }
            renderTimeline();
        });
        filtersEl.appendChild(btn);
    });

    // Activity mini-chart
    function buildActivityChart() {
        const actEl = document.getElementById('tl-activity');
        const sorted = [...timelineData].sort((a,b)=>new Date(a.timestamp)-new Date(b.timestamp));
        const dayMap = {};
        sorted.forEach(e => {
            const day = new Date(e.timestamp).toDateString();
            dayMap[day] = (dayMap[day]||0)+1;
        });
        const days = Object.entries(dayMap);
        const maxCount = Math.max(...days.map(d=>d[1]), 1);
        days.forEach(([day,count]) => {
            const bar = document.createElement('div');
            bar.className = 'tl-activity-bar';
            bar.style.height = Math.max((count/maxCount)*100, 8)+'%';
            bar.title = day+': '+count+' events';
            actEl.appendChild(bar);
        });
    }
    buildActivityChart();

    function renderTimeline() {
        let filtered = [...timelineData].filter(e=>activeFilters.has(e.type));
        // Apply capture source filter
        if (captureFilter === 'manual') filtered = filtered.filter(e => !e.auto_captured);
        else if (captureFilter === 'auto') filtered = filtered.filter(e => e.auto_captured);
        const sorted = filtered.sort((a,b)=>new Date(b.timestamp)-new Date(a.timestamp));

        // Group by date
        const groups = {};
        sorted.forEach(e => {
            const d = new Date(e.timestamp);
            const key = d.toLocaleDateString('en-US', {weekday:'short', month:'short', day:'numeric', year:'numeric'});
            if (!groups[key]) groups[key]=[];
            groups[key].push(e);
        });

        const srcLabels = { git_post_commit:'git commit', git_post_revert:'git revert', git_post_merge:'git merge', churn_detector:'churn detected', ci_parser:'CI result' };
        let html = '';
        for (const [date, events] of Object.entries(groups)) {
            html += '<div class="tl-date-group"><div class="tl-date-label">'+date+' &middot; '+events.length+' events</div>';
            events.forEach(e => {
                const outcomeClass = e.outcome==='failed'?'tl-outcome-failed':e.outcome==='worked'?'tl-outcome-worked':'';
                const outcomeLabel = e.outcome?' <span class="'+outcomeClass+'">['+e.outcome+']</span>':'';
                const loc = e.location?'<span style="color:var(--accent)"> @ '+e.location+'</span>':'';
                const iid = e.issue_id?'<span style="color:var(--text-muted)"> #'+e.issue_id+'</span>':'';
                const autoBadge = e.auto_captured?'<span class="tl-auto-badge">AUTO</span>':'';
                const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '';
                const capSrc = e.auto_captured && e.capture_source ? '<span class="tl-capture-source"> &middot; '+(srcLabels[e.capture_source]||e.capture_source)+'</span>' : '';
                html += '<div class="tl-item"><div class="tl-badge '+e.type+'">'+e.type+'</div><div class="tl-body"><div class="tl-summary">'+e.summary+outcomeLabel+iid+autoBadge+'</div><div class="tl-meta">'+ts+loc+capSrc+'</div></div></div>';
            });
            html += '</div>';
        }
        if (sorted.length === 0) html = '<div style="text-align:center;color:var(--text-muted);padding:40px">No events match current filters</div>';
        listEl.innerHTML = html;
    }
    renderTimeline();
    </script>
</body>
</html>
"""
