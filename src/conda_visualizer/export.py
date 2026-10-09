"""Standalone HTML export that stays interactive without a Python server."""

from __future__ import annotations

import os
from pathlib import Path

import panel as pn

from .component import CytoscapeGraph
from .graph import DependencyGraph
from .theme import FONTS_CSS, STANDALONE_CSS


def summary(graph: DependencyGraph) -> str:
    where = graph.source + (f" · {graph.platform}" if graph.platform else "")
    return (
        f"**{where}** · {len(graph)} packages · {graph.graph.number_of_edges()} edges · "
        f"{len(graph.leaves())} leaves · {len(graph.cycles())} cycles"
    )


def standalone(graph: DependencyGraph, title: str | None = None, **params) -> pn.Column:
    """Layout used for static export: header line plus a full-page graph."""
    params.setdefault("show_details", True)
    params.setdefault("filename", Path(graph.source).name or "graph")
    view = CytoscapeGraph(graph, sizing_mode="stretch_both", min_height=500, **params)
    header = pn.pane.Markdown(
        f"### {title or 'Dependency graph'}\n{summary(graph)}",
        margin=(0, 12),
        sizing_mode="stretch_width",
    )
    return pn.Column(header, view, sizing_mode="stretch_both")


def export_html(
    graph: DependencyGraph,
    path: str | os.PathLike,
    title: str | None = None,
    inline: bool = False,
    **params,
) -> Path:
    """Write ``graph`` to a self-contained HTML file.

    Search, highlighting, isolation, layout switching and PNG/SVG export all
    run client-side. Cytoscape is embedded from the compiled bundle shipped in
    the wheel; ``inline=True`` also embeds the Bokeh/Panel JS. The brand fonts
    come from Google Fonts and fall back to Arial offline. An uncompiled source
    checkout falls back to esm.sh.
    """
    path = Path(path)
    title = title or f"Dependencies of {Path(graph.source).name or 'environment'}"
    with pn.config.set(css_files=[FONTS_CSS], raw_css=[STANDALONE_CSS]):
        standalone(graph, title, **params).save(
            path, title=title, resources="inline" if inline else "cdn", embed=False
        )
    return path
