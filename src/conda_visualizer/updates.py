"""Latest available versions and what-if version changes, solved with py-rattler.

``fetch_availability`` asks the graph's channels for every version of every
conda package in it. ``plan_change`` solves the environment again with some
packages pinned to other versions, keeping every other package where it is
unless the solver has to move it. ``apply_plan`` overlays the result on the
current graph so the differences can be drawn.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

from .graph import DependencyGraph, spec_name
from .sources import SourceError, _virtual_packages, current_platform, rattler, run_sync


class UpdateError(SourceError):
    """The channels could not be queried or no solution exists."""


#: How a package differs between the current environment and a plan.
CHANGES = ("upgrade", "downgrade", "rebuild", "add", "remove")

#: Packages the solver may not know about when the graph has no channel information.
DEFAULT_CHANNELS = ["conda-forge"]


def _require_rattler():
    if rattler is None:
        raise UpdateError("Version lookups and what-if changes require py-rattler")


@functools.cache
def _gateway():
    # One gateway keeps the repodata in memory between lookups and solves.
    return rattler.Gateway()


def _version(text: str):
    try:
        return rattler.Version(text)
    except Exception:
        return None


_PRE_TAGS = {"alpha", "beta", "pre", "preview", "rc", "c", "dev"}


def _is_prerelease(version) -> bool:
    """``3.15.0rc2`` and ``1.0b1``; not ``1.1.1w``, where a letter is a patch level."""
    if version.is_dev:
        return True
    for segment in version.segments():
        for i, part in enumerate(segment):
            if not isinstance(part, str):
                continue
            followed_by_number = i + 1 < len(segment) and isinstance(segment[i + 1], int)
            if part in _PRE_TAGS or (part in {"a", "b"} and followed_by_number):
                return True
    return False


def _conda_nodes(graph: DependencyGraph) -> dict[str, dict]:
    return {
        name: data
        for name, data in graph.graph.nodes(data=True)
        if data.get("kind") == "conda" and data.get("version")
    }


def _channel_base(data: dict) -> str:
    """Channel URL of an installed package; a package URL is <channel>/<subdir>/<filename>."""
    url = data.get("url") or ""
    return url.rsplit("/", 2)[0].rstrip("/") if "://" in url else ""


def graph_channels(graph: DependencyGraph) -> list[str]:
    """Channel URLs the installed packages came from, most used first."""
    counts: dict[str, int] = {}
    for data in _conda_nodes(graph).values():
        base = _channel_base(data) or data.get("channel") or ""
        if base:
            counts[base] = counts.get(base, 0) + 1
    return sorted(counts, key=lambda c: -counts[c]) or list(DEFAULT_CHANNELS)


def graph_platform(graph: DependencyGraph) -> str:
    if graph.platform and graph.platform != "noarch":
        return graph.platform
    subdirs = [d.get("subdir") for d in _conda_nodes(graph).values()]
    subdirs = [s for s in subdirs if s and s != "noarch"]
    return max(set(subdirs), key=subdirs.count) if subdirs else current_platform()


# Availability ---------------------------------------------------------------


@dataclass
class Availability:
    """Versions offered by the channels, newest first, per package."""

    versions: dict[str, list[str]] = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    platform: str = ""

    def latest(self, name: str) -> str | None:
        found = self.versions.get(name)
        return found[0] if found else None

    def choices(self, name: str, current: str | None = None) -> list[str]:
        """Versions to offer for ``name``, always including the installed one."""
        found = list(self.versions.get(name, []))
        if current and current not in found:
            found.append(current)
            found.sort(key=lambda v: _version(v) or _version("0"), reverse=True)
        return found


def fetch_availability(
    graph: DependencyGraph,
    channels: list[str] | None = None,
    platform: str | None = None,
) -> Availability:
    """Look up every available version of the graph's conda packages."""
    _require_rattler()
    channels = channels or graph_channels(graph)
    platform = platform or graph_platform(graph)
    names = sorted(_conda_nodes(graph))
    if not names:
        return Availability(channels=channels, platform=platform)

    async def query():
        # Not recursive: only the named packages, not everything they depend on.
        return await _gateway().query(
            sources=channels, platforms=[platform, "noarch"], specs=names, recursive=False
        )

    try:
        result = run_sync(query())
    except Exception as exc:
        raise UpdateError(f"Could not read the channels {', '.join(channels)}: {exc}") from exc
    # Strict channel priority means the solver only considers one channel per package,
    # so offer (and compare against) the versions of the channel it was installed from.
    home = {name: _channel_base(data) for name, data in _conda_nodes(graph).items()}
    found: dict[str, dict[str, dict[str, object]]] = {}
    for records in result:
        for rec in records:
            if not _is_prerelease(rec.version):
                name, channel = rec.name.normalized, str(rec.channel or "").rstrip("/")
                found.setdefault(name, {}).setdefault(channel, {})[str(rec.version)] = rec.version
    versions = {}
    for name, by_channel in found.items():
        seen = by_channel.get(home.get(name, ""))
        if seen is None:
            seen = {v: ver for chan in by_channel.values() for v, ver in chan.items()}
        versions[name] = [v for v, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]
    return Availability(versions=versions, channels=channels, platform=platform)


def with_availability(
    graph: DependencyGraph, availability: Availability, requested_only: bool = False
) -> DependencyGraph:
    """Copy of ``graph`` whose conda packages know their newest version.

    ``outdated`` marks the packages worth drawing attention to: all of them, or with
    ``requested_only`` just the ones that were asked for (the rest follow from those).
    """
    out = graph.copy()
    wanted = set(requested_specs(out)) if requested_only else None
    for name, data in _conda_nodes(out).items():
        for key in ("latest", "outdated"):
            data.pop(key, None)
        latest = availability.latest(name)
        if not latest:
            continue
        data["latest"] = latest
        current, newest = _version(data["version"]), _version(latest)
        newer = bool(current is not None and newest is not None and current < newest)
        data["outdated"] = newer and (wanted is None or name in wanted)
    return out


def outdated(graph: DependencyGraph) -> list[dict]:
    """Rows ``name, version, latest`` for packages with a newer version, from annotated graphs."""
    return [
        {"name": name, "version": data["version"], "latest": data["latest"]}
        for name, data in sorted(_conda_nodes(graph).items())
        if data.get("outdated")
    ]


# Plans ----------------------------------------------------------------------


@dataclass
class Change:
    name: str
    kind: str
    old_version: str = ""
    new_version: str = ""
    old_build: str = ""
    new_build: str = ""

    def describe(self) -> str:
        if self.kind == "add":
            return f"{self.name} {self.new_version} (new)"
        if self.kind == "remove":
            return f"{self.name} {self.old_version} (removed)"
        if self.kind == "rebuild":
            return f"{self.name} {self.old_version}: {self.old_build} -> {self.new_build}"
        return f"{self.name} {self.old_version} -> {self.new_version}"


@dataclass
class Plan:
    """The environment after changing some packages, relative to the current one."""

    targets: dict[str, str]
    update_all: bool
    changes: dict[str, Change]
    records: dict[str, dict]

    def __bool__(self) -> bool:
        return bool(self.changes)

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(CHANGES, 0)
        for change in self.changes.values():
            counts[change.kind] += 1
        return counts

    def rows(self) -> list[dict]:
        order = {kind: i for i, kind in enumerate(("upgrade", "downgrade", "add", "remove"))}
        rows = [
            {
                "name": c.name,
                "change": c.kind,
                "from": c.old_version + (f" ({c.old_build})" if c.kind == "rebuild" else ""),
                "to": c.new_version + (f" ({c.new_build})" if c.kind == "rebuild" else ""),
                "target": c.name in self.targets,
            }
            for c in self.changes.values()
        ]
        return sorted(rows, key=lambda r: (order.get(r["change"], 9), r["name"]))


def relax_spec(spec: str) -> str:
    """Reduce a spec that names a build to just the package name.

    Histories record ``pkg==1.2=py313_0`` (sometimes ``pkg==1.2==py313_0``) when a package was
    installed from an exact record. That pins one build of one version, which would make any
    change to the package or to what it depends on unsolvable, so only the name is kept.
    """
    try:
        parsed = rattler.MatchSpec(spec)
    except Exception:
        return spec_name(spec)
    return spec_name(spec) if parsed.build else spec


def requested_specs(graph: DependencyGraph) -> dict[str, str]:
    nodes = _conda_nodes(graph)
    names = [n for n, d in nodes.items() if d.get("requested")]
    if not names:
        names = [n for n in graph.roots() if n in nodes]
    return {n: relax_spec(nodes[n].get("requested_spec") or n) for n in names}


def _locked(graph: DependencyGraph, skip: set[str], channels, platform) -> list:
    """Records of the installed packages, so the solver prefers to keep them."""
    nodes = {n: d for n, d in _conda_nodes(graph).items() if n not in skip}
    if not nodes:
        return []

    async def query():
        specs = [f"{name}=={data['version']}" for name, data in nodes.items()]
        return await _gateway().query(
            sources=channels, platforms=[platform, "noarch"], specs=specs, recursive=False
        )

    result = run_sync(query())
    locked = {}
    for records in result:
        for rec in records:
            data = nodes.get(rec.name.normalized)
            if data and str(rec.version) == data["version"] and rec.build == data.get("build"):
                locked[rec.name.normalized] = rec
    return list(locked.values())


def _plain(rec) -> dict:
    return {
        "version": str(rec.version),
        "build": rec.build,
        "channel": str(rec.channel or ""),
        "subdir": rec.subdir,
        "size": rec.size,
        "license": rec.license,
        "url": str(rec.url),
        "depends": list(rec.depends),
    }


def _classify(old: dict, new: dict) -> str | None:
    old_v, new_v = _version(old["version"]), _version(new["version"])
    if old_v is not None and new_v is not None and old_v != new_v:
        return "upgrade" if new_v > old_v else "downgrade"
    if old["version"] != new["version"]:
        return "upgrade"
    if old.get("build") != new["build"]:
        return "rebuild"
    return None


def plan_change(
    graph: DependencyGraph,
    targets: dict[str, str] | None = None,
    *,
    update_all: bool = False,
    channels: list[str] | None = None,
    platform: str | None = None,
) -> Plan:
    """Solve ``graph`` again with ``targets`` (name -> version) pinned.

    Requested packages keep their original spec; everything else stays at its
    installed version unless the new pins force it to move. With
    ``update_all`` nothing is kept, which gives the newest consistent set.
    """
    _require_rattler()
    targets = dict(targets or {})
    nodes = _conda_nodes(graph)
    unknown = sorted(set(targets) - set(nodes))
    if unknown:
        raise UpdateError(f"Not conda packages of this environment: {', '.join(unknown)}")
    channels = channels or graph_channels(graph)
    platform = platform or graph_platform(graph)

    specs = requested_specs(graph)
    for name, version in targets.items():
        specs[name] = f"{name}=={version}"
    try:
        locked = [] if update_all else _locked(graph, set(targets), channels, platform)
        solved = run_sync(
            rattler.solve(
                sources=channels,
                specs=list(specs.values()),
                platforms=[platform, "noarch"],
                locked_packages=locked,
                virtual_packages=_virtual_packages(platform),
                gateway=_gateway(),
            )
        )
    except UpdateError:
        raise
    except Exception as exc:
        wanted = ", ".join(f"{n} {v}" for n, v in targets.items()) or "the requested packages"
        raise UpdateError(f"No solution for {wanted}: {exc}") from exc

    records = {rec.name.normalized: _plain(rec) for rec in solved}
    changes: dict[str, Change] = {}
    for name, new in records.items():
        old = nodes.get(name)
        if old is None:
            changes[name] = Change(name, "add", new_version=new["version"], new_build=new["build"])
        elif kind := _classify(old, new):
            changes[name] = Change(
                name, kind, old["version"], new["version"], old.get("build", ""), new["build"]
            )
    for name, old in nodes.items():
        if name not in records:
            changes[name] = Change(name, "remove", old["version"], old_build=old.get("build", ""))
    return Plan(targets=targets, update_all=update_all, changes=changes, records=records)


def apply_plan(graph: DependencyGraph, plan: Plan) -> DependencyGraph:
    """Overlay ``plan`` on ``graph``: new versions, new edges and a ``change`` per node.

    Nodes the plan removes stay in the graph, marked ``change="remove"``, so the
    removal is visible. Nothing is marked on packages the plan leaves alone.
    """
    out = graph.copy()
    g = out.graph
    for name, change in plan.changes.items():
        if change.kind == "remove":
            g.nodes[name]["change"] = "remove"
            continue
        new = plan.records[name]
        if name not in g:
            g.add_node(name)
        data = g.nodes[name]
        data.pop("outdated", None)
        old_version = data.get("version")
        data.update(
            kind="conda",
            version=new["version"],
            build=new["build"],
            subdir=new["subdir"],
            size=new["size"],
            license=new["license"],
            url=new["url"],
            change=change.kind,
        )
        if change.kind != "add":
            data["old_version"] = old_version
            data["old_build"] = change.old_build
        latest, current = data.get("latest"), _version(new["version"])
        if latest and current is not None and (newest := _version(latest)) is not None:
            data["outdated"] = current < newest
    for name in plan.targets:
        if name in g:
            g.nodes[name]["target"] = True
    for name, change in plan.changes.items():
        if change.kind == "remove":
            continue
        new = plan.records[name]
        g.remove_edges_from(list(g.out_edges(name)))
        for dep in new["depends"]:
            target = spec_name(dep)
            if target not in g:
                g.add_node(target, kind="virtual" if target.startswith("__") else "missing")
            out.add_dependency(name, target, dep)
    return out
