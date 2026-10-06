"""Package dependency graph with conda-tree style queries.

Edges point from a package to its dependency, matching conda-tree, so
``depends`` follows outgoing edges and ``whoneeds`` follows incoming ones.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import networkx as nx

Direction = Literal["depends", "whoneeds", "both"]

#: Attributes a package node may carry; anything else is dropped on export.
NODE_FIELDS = (
    "version",
    "build",
    "channel",
    "subdir",
    "kind",
    "requested",
    "size",
    "license",
    "url",
)

#: Node kinds. ``virtual`` and ``missing`` are created implicitly for
#: dependency names that have no record of their own.
KINDS = ("conda", "pypi", "virtual", "missing", "node")

_SPEC_NAME = re.compile(r"^\s*(?:[^\s:]+::)?([A-Za-z0-9_.\-]+)")


def spec_name(spec: str) -> str:
    """Return the package name of a conda MatchSpec or PEP 508 string."""
    match = _SPEC_NAME.match(spec)
    return match.group(1) if match else spec.split(" ")[0]


def normalize_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class DependencyGraph:
    """A directed package graph plus metadata about where it came from."""

    graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    source: str = ""
    platform: str = ""
    warnings: list[str] = field(default_factory=list)

    # Construction ---------------------------------------------------------

    def add_package(self, name: str, **attrs) -> None:
        attrs = {k: v for k, v in attrs.items() if v not in (None, "")}
        attrs.setdefault("kind", "conda")
        self.graph.add_node(name, record=True, **attrs)

    def add_dependency(self, package: str, dependency: str, spec: str = "") -> None:
        if package == dependency:
            return
        if not self.graph.has_node(package):
            self.graph.add_node(package)
        if self.graph.has_edge(package, dependency):
            # noarch records often repeat a dependency (e.g. python twice);
            # conda-tree keeps only the last spec, we keep both.
            previous = self.graph.edges[package, dependency].get("spec", "")
            if spec and spec not in previous.split(", "):
                spec = f"{previous}, {spec}" if previous else spec
            else:
                spec = previous
        self.graph.add_edge(package, dependency, spec=spec)

    def finalize(self) -> DependencyGraph:
        """Classify nodes that only appeared as dependency targets."""
        for name, data in self.graph.nodes(data=True):
            if not data.pop("record", False) and "kind" not in data:
                data["kind"] = "virtual" if name.startswith("__") else "missing"
        missing = sorted(n for n, d in self.graph.nodes(data=True) if d["kind"] == "missing")
        if missing:
            preview = ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
            self.warnings.append(f"{len(missing)} dependencies have no record: {preview}")
        return self

    # Basic accessors ------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self.graph

    def __len__(self) -> int:
        return self.graph.number_of_nodes()

    @property
    def packages(self) -> list[str]:
        return sorted(self.graph.nodes)

    def node(self, name: str) -> dict:
        return dict(self.graph.nodes[name])

    # conda-tree queries ---------------------------------------------------

    def leaves(self) -> list[str]:
        """Packages nothing else depends on (conda-tree ``leaves``)."""
        return sorted(n for n, deg in self.graph.in_degree() if deg == 0)

    def cycles(self) -> list[list[str]]:
        """Simple dependency cycles, each rotated to start at its smallest name."""
        cycles = []
        for cycle in nx.simple_cycles(self.graph):
            start = cycle.index(min(cycle))
            cycles.append(cycle[start:] + cycle[:start])
        return sorted(cycles)

    def roots(self) -> list[str]:
        """Leaves plus one member of every cycle unreachable from a leaf.

        Matches ``conda tree leaves --with-cycles``: a cycle nobody else
        depends on would otherwise be invisible as a root.
        """
        leaves = self.leaves()
        reachable = set(leaves)
        for leaf in leaves:
            reachable |= nx.descendants(self.graph, leaf)
        roots = list(leaves)
        for cycle in self.cycles():
            if not reachable.intersection(cycle):
                roots.append(cycle[0])
                reachable |= nx.descendants(self.graph, cycle[0]) | {cycle[0]}
        return sorted(roots)

    def depends(
        self, package: str, recursive: bool = False, exclude: Iterable[str] = ()
    ) -> list[str]:
        """Dependencies of ``package`` (conda-tree ``depends``)."""
        return self._reach(package, "depends", None if recursive else 1, set(exclude))

    def whoneeds(
        self, package: str, recursive: bool = False, exclude: Iterable[str] = ()
    ) -> list[str]:
        """Packages depending on ``package`` (conda-tree ``whoneeds``)."""
        return self._reach(package, "whoneeds", None if recursive else 1, set(exclude))

    def _reach(
        self, package: str, direction: Direction, depth: int | None, exclude: set[str]
    ) -> list[str]:
        if package not in self.graph:
            raise KeyError(f"package {package!r} not found")
        # Unlike conda-tree's shortest-path check, excluded packages are
        # reported but never traversed, so nothing reachable another way is lost.
        found = self._bfs([package], direction, depth, exclude)
        found.discard(package)
        return sorted(found)

    def _bfs(
        self, start: Iterable[str], direction: Direction, depth: int | None, exclude: set[str]
    ) -> set[str]:
        steps = []
        if direction in ("depends", "both"):
            steps.append(self.graph.successors)
        if direction in ("whoneeds", "both"):
            steps.append(self.graph.predecessors)
        seen = set(start)
        for step in steps:
            queue = deque((node, 0) for node in start)
            visited = set(start)
            while queue:
                node, level = queue.popleft()
                if (depth is not None and level >= depth) or (
                    node in exclude and node not in start
                ):
                    continue
                for nxt in step(node):
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append((nxt, level + 1))
            seen |= visited
        return seen

    def neighborhood(
        self,
        focus: Iterable[str] = (),
        direction: Direction = "both",
        depth: int | None = None,
        exclude: Iterable[str] = (),
        hide: Iterable[str] = (),
    ) -> set[str]:
        """Nodes to display around ``focus``.

        ``exclude`` nodes stay visible but are not traversed through (the
        conda-tree meaning); ``hide`` nodes are removed entirely.
        """
        focus = [f for f in focus if f in self.graph]
        hide = set(hide) - set(focus)
        if focus:
            nodes = self._bfs(focus, direction, depth, set(exclude) | hide)
        else:
            nodes = set(self.graph.nodes)
        return nodes - hide

    def subgraph(self, nodes: Iterable[str]) -> DependencyGraph:
        return DependencyGraph(
            graph=self.graph.subgraph(nodes).copy(),
            source=self.source,
            platform=self.platform,
            warnings=list(self.warnings),
        )

    def without_kinds(self, kinds: Iterable[str]) -> list[str]:
        kinds = set(kinds)
        return [n for n, d in self.graph.nodes(data=True) if d.get("kind") in kinds]

    # Package details ------------------------------------------------------

    def details(self, name: str) -> dict:
        data = self.node(name)
        return {
            "name": name,
            **data,
            "depends": [
                (dep, self.graph.edges[name, dep].get("spec", ""))
                for dep in sorted(self.graph.successors(name))
            ],
            "whoneeds": [
                (dep, self.graph.edges[dep, name].get("spec", ""))
                for dep in sorted(self.graph.predecessors(name))
            ],
        }

    def tree(
        self,
        root: str,
        reverse: bool = False,
        exclude: Iterable[str] = (),
        full: bool = False,
        max_lines: int = 2000,
    ) -> str:
        """Render a conda-tree style text tree rooted at ``root``."""
        exclude = set(exclude)
        version = self.graph.nodes[root].get("version", "")
        lines = [f"{root}=={version}" if version else root]
        expanded: set[str] = set()
        cycle_nodes = {n for c in self.cycles() for n in c} if full else set()
        label = "dependent packages" if reverse else "dependencies"

        def children(node):
            if reverse:
                return [
                    (p, self.graph.edges[p, node].get("spec", ""))
                    for p in sorted(self.graph.predecessors(node))
                ]
            return [
                (s, self.graph.edges[node, s].get("spec", ""))
                for s in sorted(self.graph.successors(node))
            ]

        def walk(node, prefix, path):
            items = children(node)
            for i, (child, spec) in enumerate(items):
                if len(lines) >= max_lines:
                    return
                last = i == len(items) - 1
                branch = "└─ " if last else "├─ "
                ver = self.graph.nodes[child].get("version", "")
                name = f"{child} {ver}" if ver else child
                lines.append(f"{prefix}{branch}{name} [required: {spec or 'any'}]")
                sub = prefix + ("   " if last else "│  ")
                if child in path or child in exclude:
                    continue
                if not children(child):
                    continue
                if child in expanded and (not full or child in cycle_nodes):
                    lines.append(f"{sub}└─ {label} of {child} displayed above")
                    continue
                expanded.add(child)
                walk(child, sub, path | {child})

        expanded.add(root)
        walk(root, "", {root})
        if len(lines) >= max_lines:
            lines.append("... (truncated)")
        return "\n".join(lines)

    def records(self) -> list[dict]:
        """One row per package, for tabular display."""
        rows = []
        for name, data in sorted(self.graph.nodes(data=True)):
            rows.append(
                {
                    "name": name,
                    "version": data.get("version", ""),
                    "build": data.get("build", ""),
                    "channel": data.get("channel", ""),
                    "kind": data.get("kind", ""),
                    "requested": bool(data.get("requested", False)),
                    "depends": self.graph.out_degree(name),
                    "dependents": self.graph.in_degree(name),
                    "size": data.get("size"),
                }
            )
        return rows

    # Export ---------------------------------------------------------------

    def to_elements(self) -> dict:
        """Nodes and edges in the shape the Cytoscape component expects."""
        nodes = []
        for name, data in self.graph.nodes(data=True):
            item = {"id": name, "label": name}
            item.update({k: data[k] for k in NODE_FIELDS if k in data})
            item["in"] = self.graph.in_degree(name)
            item["out"] = self.graph.out_degree(name)
            nodes.append(item)
        edges = [
            {"id": f"{s}->{t}", "source": s, "target": t, "spec": d.get("spec", "")}
            for s, t, d in self.graph.edges(data=True)
        ]
        return {"nodes": nodes, "edges": edges}

    def to_dot(self, attributes: bool = True) -> str:
        """DOT output; with ``attributes=False`` it matches ``conda tree --dot``."""

        def q(value) -> str:
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

        lines = ["digraph {"]
        if attributes:
            for name, data in sorted(self.graph.nodes(data=True)):
                attrs = {k: data[k] for k in NODE_FIELDS if k in data and k != "url"}
                attrs = ", ".join(f"{k}={q(v)}" for k, v in attrs.items())
                lines.append(f"  {q(name)} [{attrs}]" if attrs else f"  {q(name)}")
        for s, t, data in sorted(self.graph.edges(data=True)):
            spec = data.get("spec")
            suffix = f" [spec={q(spec)}]" if attributes and spec else ""
            lines.append(f"  {q(s)} -> {q(t)}{suffix}")
        lines.append("}")
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        return json.dumps(
            {
                "source": self.source,
                "platform": self.platform,
                "warnings": self.warnings,
                **self.to_elements(),
            },
            indent=1,
        )
