"""Cytoscape.js graph component for Panel."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import param
from panel.custom import JSComponent

if TYPE_CHECKING:
    # Kept out of the runtime import so the isolated wheel build, which only has
    # Panel installed, can import this module to compile the bundle.
    from .graph import DependencyGraph

DIST = Path(__file__).parent / "dist"

LAYOUTS = ["dagre", "elk", "fcose", "breadthfirst", "cose", "concentric", "circle", "grid"]


class CytoscapeGraph(JSComponent):
    """Interactive directed graph rendered with Cytoscape.js.

    ``elements`` holds ``{"nodes": [...], "edges": [...]}`` where each node
    needs an ``id`` and each edge a ``source`` and ``target``; all other keys
    are available to the stylesheet and tooltip. Clicking a node sets
    ``selected`` and highlights what it depends on and what depends on it.
    """

    elements = param.Dict(
        default={"nodes": [], "edges": []},
        doc="""Nodes and edges to render. Setting ``"keep_view": True`` in the dictionary
        keeps the zoom and re-centers the selected node instead of fitting the whole graph;
        it travels with the data so it cannot arrive after the update it applies to.""",
    )

    layout = param.Selector(default="dagre", objects=LAYOUTS, doc="Cytoscape layout algorithm.")

    rank_dir = param.Selector(
        default="TB", objects=["TB", "LR", "BT", "RL"], doc="Direction of hierarchical layouts."
    )

    selected = param.String(default=None, allow_None=True, doc="Id of the selected node.")

    highlight = param.Selector(
        default="both",
        objects=["both", "depends", "whoneeds", "neighbors"],
        doc="Which related nodes to highlight around the selection.",
    )

    show_versions = param.Boolean(default=True, doc="Show the version below node labels.")

    show_latest = param.Boolean(
        default=True,
        doc="Show the newest available version of outdated packages (nodes with ``latest``).",
    )

    dark_theme = param.Boolean(default=False, doc="Use the dark color palette.")

    search = param.String(default="", doc="Highlight packages whose name contains this text.")

    toolbar = param.Boolean(
        default=True, doc="Show the built-in search, layout and image export toolbar."
    )

    show_details = param.Boolean(
        default=False, doc="Show a card listing the selected package's direct relations."
    )

    filename = param.String(default="graph", doc="Base name for PNG/SVG downloads.")

    # Built by ``panel compile conda_visualizer``; without it the raw ESM and the
    # importmap below are used, which needs network access to esm.sh.
    _bundle = DIST / "conda-visualizer.bundle.js"

    _esm = Path(__file__).parent / "graph.js"

    # Raw CSS rather than a Path: inside a server session Panel turns a Path into
    # a server-relative URL, which is dead in a page saved from that session.
    _stylesheets = [(Path(__file__).parent / "graph.css").read_text(encoding="utf-8")]  # noqa: RUF012

    _importmap = {  # noqa: RUF012
        "imports": {
            "cytoscape": "https://esm.sh/cytoscape@3.34.3",
            "cytoscape-dagre": "https://esm.sh/cytoscape-dagre@4.0.1",
            "cytoscape-elk": "https://esm.sh/cytoscape-elk@2.3.0",
            "cytoscape-fcose": "https://esm.sh/cytoscape-fcose@2.2.0",
            "cytoscape-svg": "https://esm.sh/cytoscape-svg@0.4.0",
        }
    }

    def __init__(self, graph: DependencyGraph | None = None, **params):
        if graph is not None:
            params["elements"] = graph.to_elements()
        params.setdefault("min_height", 400)
        super().__init__(**params)
        self._dblclick_callbacks: list[Callable[[str], None]] = []

    def on_dblclick(self, callback: Callable[[str], None]) -> None:
        """Register ``callback(node_id)`` for node double clicks."""
        self._dblclick_callbacks.append(callback)

    def fit(self) -> None:
        self._send_msg({"type": "fit"})

    def relayout(self) -> None:
        self._send_msg({"type": "layout"})

    def export(self, fmt: str = "png", filename: str | None = None) -> None:
        """Trigger a browser download of the full graph as PNG or SVG."""
        self._send_msg({"type": "export", "format": fmt, "filename": filename or self.filename})

    def _handle_msg(self, data) -> None:
        if data.get("type") == "dblclick":
            for callback in self._dblclick_callbacks:
                callback(data["id"])
