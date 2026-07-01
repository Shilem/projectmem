"""Tests for the Showoff tab (animated story scenes + recorder) in `pjm visualize`.

The Showoff feature is template-only: it adds a sidebar tab, a canvas stage,
three animated scenes (Story Replay / Orbit / Universe) and a MediaRecorder
based REC button to the generated dashboard. These tests pin the template
contract so a refactor cannot silently drop the tab or its controls.
"""

from __future__ import annotations

import json

from projectmem.commands.visualize import VIZ_TEMPLATE, build_graph_data
from projectmem.models import Event


def test_template_has_showoff_nav_and_panel() -> None:
    assert 'data-panel="showoff"' in VIZ_TEMPLATE
    assert 'id="panel-showoff"' in VIZ_TEMPLATE
    assert 'id="so-canvas"' in VIZ_TEMPLATE


def test_template_has_all_three_scenes() -> None:
    for scene in ("replay", "orbit", "universe"):
        assert f'data-scene="{scene}"' in VIZ_TEMPLATE


def test_template_has_recorder_controls() -> None:
    assert 'id="so-rec"' in VIZ_TEMPLATE
    assert 'id="so-reclen"' in VIZ_TEMPLATE
    # custom duration is hard-capped at one minute
    assert 'value="60"' in VIZ_TEMPLATE
    assert "Math.min(60," in VIZ_TEMPLATE
    assert "MediaRecorder" in VIZ_TEMPLATE
    assert "captureStream" in VIZ_TEMPLATE
    # honest platform note ships with the recorder
    assert ".webm" in VIZ_TEMPLATE


def test_template_has_watermark_toggle() -> None:
    assert 'id="so-wm"' in VIZ_TEMPLATE
    assert "made with projectmem" in VIZ_TEMPLATE


def test_showoff_uses_existing_data_only() -> None:
    """Showoff must reuse the Story Map's injected data - no new placeholders."""
    placeholders = {
        token
        for token in (
            "{{GRAPH_DATA}}",
            "{{PROJECT_MAP}}",
            "{{PROJECT_MAP_GRAPH}}",
            "{{TIMELINE_DATA}}",
            "{{SCORE_DATA}}",
            "{{PROJECT_NAME}}",
        )
        if token in VIZ_TEMPLATE
    }
    assert placeholders == {
        "{{GRAPH_DATA}}",
        "{{PROJECT_MAP}}",
        "{{PROJECT_MAP_GRAPH}}",
        "{{TIMELINE_DATA}}",
        "{{SCORE_DATA}}",
        "{{PROJECT_NAME}}",
    }


def test_graph_data_feeds_showoff_scenes() -> None:
    """The fields Showoff reads (type / event_type / outcome / timestamp /
    event_count / failure_count) must survive JSON round-tripping."""
    events = [
        Event(type="issue", summary="crash on empty input", issue_id="0001",
              location="src/run.py:42"),
        Event(type="attempt", summary="guarded with if-not-x", issue_id="0001",
              outcome="failed", location="src/run.py:42"),
        Event(type="fix", summary="validate before parse", issue_id="0001",
              location="src/run.py:42"),
    ]
    graph = json.loads(json.dumps(build_graph_data(events)))
    kinds = {node["type"] for node in graph["nodes"]}
    assert kinds == {"file", "event"}
    event_nodes = [n for n in graph["nodes"] if n["type"] == "event"]
    assert {n["event_type"] for n in event_nodes} == {"issue", "attempt", "fix"}
    assert any(n.get("outcome") == "failed" for n in event_nodes)
    file_nodes = [n for n in graph["nodes"] if n["type"] == "file"]
    assert file_nodes and all("event_count" in n and "failure_count" in n
                              for n in file_nodes)
