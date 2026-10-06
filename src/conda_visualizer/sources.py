"""Converters from conda environment definitions to :class:`DependencyGraph`.

Supported inputs:

* an installed environment (prefix path or ``conda env`` name), read from
  ``conda-meta/*.json`` exactly like conda-tree's ``PrefixData`` records
* ``pixi.lock`` (v4 to v7)
* conda-lock unified lockfiles (``conda-lock.yml``)
* ``@EXPLICIT`` lockfiles, which carry no dependency edges
* ``environment.yml`` specs, solved with py-rattler
* DOT files, including ``conda tree deptree --dot`` output
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.parser import HeaderParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import pydot
import yaml
from packaging.markers import UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement

from .graph import DependencyGraph, normalize_pypi, spec_name

try:
    import rattler
except ImportError:  # optional: only needed to solve environment.yml
    rattler = None

ARCHIVE_SUFFIXES = (".conda", ".tar.bz2", ".whl", ".tar.gz", ".zip")


class SourceError(ValueError):
    """Raised when an input cannot be identified or parsed."""


def current_platform() -> str:
    machine = _platform.machine().lower()
    if sys.platform == "darwin":
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-64"
    if sys.platform.startswith("win"):
        return "win-arm64" if machine in ("arm64", "aarch64") else "win-64"
    arch = {"x86_64": "64", "amd64": "64", "aarch64": "aarch64", "arm64": "aarch64"}
    return f"linux-{arch.get(machine, machine)}"


def channel_name(url: str) -> str:
    """``https://conda.anaconda.org/conda-forge/osx-arm64`` -> ``conda-forge``."""
    if not url:
        return ""
    path = (urlparse(url).path if "://" in url else url).strip("/").split("/")
    if path and re.fullmatch(r"(noarch|(linux|osx|win|emscripten|wasi|zos)-\w+)", path[-1]):
        path = path[:-1]
    return "/".join(path)


def parse_filename(url: str) -> dict:
    """Recover name, version, build and subdir from a package URL or filename."""
    path = unquote(urlparse(url).path) if "://" in url else url
    parts = path.rstrip("/").split("/")
    filename = parts[-1]
    for suffix in ARCHIVE_SUFFIXES:
        if filename.endswith(suffix):
            filename = filename[: -len(suffix)]
            break
    info: dict = {"url": url}
    if url.endswith(".whl"):
        name, version, *_ = filename.split("-")
        return {**info, "name": name, "version": version, "kind": "pypi"}
    try:
        name, version, build = filename.rsplit("-", 2)
    except ValueError:
        return {**info, "name": filename}
    info.update(name=name, version=version, build=build)
    if len(parts) >= 2:
        info["subdir"] = parts[-2]
        info["channel"] = channel_name(url.rsplit("/", 1)[0])
    return info


def parse_requires_dist(requirement: str) -> str | None:
    """Name of a PEP 508 requirement, or None if its marker excludes it here."""
    try:
        req = Requirement(requirement)
    except InvalidRequirement:
        return normalize_pypi(spec_name(requirement))
    if req.marker is not None:
        try:
            if not req.marker.evaluate({"extra": ""}):
                return None
        except UndefinedEnvironmentName:
            pass
    return normalize_pypi(req.name)


def _link_pypi(graph: DependencyGraph, pypi_names: dict[str, str]) -> None:
    """Point pypi requirements at the conda or pypi package providing them."""
    pypi_nodes = [n for n, d in graph.graph.nodes(data=True) if d.get("kind") == "pypi"]
    pypi_names = {**pypi_names, **{normalize_pypi(n): n for n in pypi_nodes}}
    for name in pypi_nodes:
        data = graph.graph.nodes[name]
        for req in data.pop("requires_dist", []):
            target = parse_requires_dist(req)
            if target:
                graph.add_dependency(name, pypi_names.get(target, target), req)


@dataclass
class Source:
    """A parsed input that can produce one graph per platform/environment."""

    label: str
    kind: str = ""
    platforms: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)

    @property
    def default_platform(self) -> str | None:
        if not self.platforms:
            return None
        here = current_platform()
        return here if here in self.platforms else self.platforms[0]

    @property
    def default_environment(self) -> str | None:
        if not self.environments:
            return None
        return "default" if "default" in self.environments else self.environments[0]

    def graph(self, platform: str | None = None, environment: str | None = None):
        raise NotImplementedError


# Installed environments ---------------------------------------------------


@dataclass
class PrefixSource(Source):
    """An installed environment, read from its ``conda-meta`` directory."""

    prefix: Path = Path()
    include_pip: bool = True

    def __post_init__(self):
        self.kind = "prefix"

    def graph(self, platform=None, environment=None) -> DependencyGraph:
        meta = self.prefix / "conda-meta"
        g = DependencyGraph(source=str(self.prefix))
        requested = self.history_specs()
        pypi_names: dict[str, str] = {}
        for path in sorted(meta.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                g.warnings.append(f"Could not read {path.name}: {exc}")
                continue
            name = record["name"]
            g.platform = g.platform or record.get("subdir", "")
            explicit = record.get("requested_spec") or record.get("requested_specs")
            g.add_package(
                name,
                version=record.get("version"),
                build=record.get("build"),
                channel=channel_name(record.get("channel", "")),
                subdir=record.get("subdir"),
                size=record.get("size"),
                license=record.get("license"),
                url=record.get("url"),
                requested=name in requested or bool(explicit),
            )
            for dep in record.get("depends", []):
                g.add_dependency(name, spec_name(dep), dep)
            pypi_names[normalize_pypi(name)] = name
        if self.include_pip:
            for dist in self._pip_dists():
                name = dist["name"]
                if normalize_pypi(name) in pypi_names:
                    continue
                g.add_package(
                    name,
                    version=dist["version"],
                    kind="pypi",
                    requested=dist["requested"],
                    requires_dist=dist["requires"],
                )
            _link_pypi(g, pypi_names)
        if not len(g):
            raise SourceError(f"No packages found in {meta}")
        return g.finalize()

    def history_specs(self) -> set[str]:
        """Names the user explicitly asked for, replayed from ``conda-meta/history``."""
        history = self.prefix / "conda-meta" / "history"
        names: set[str] = set()
        if not history.exists():
            return names
        for line in history.read_text(errors="replace").splitlines():
            match = re.match(r"#\s*(update|remove|neutered) specs:\s*(\[.*\])", line)
            if not match:
                continue
            try:
                specs = {spec_name(s) for s in ast.literal_eval(match.group(2))}
            except (ValueError, SyntaxError):
                continue
            if match.group(1) == "remove":
                names -= specs
            else:
                names |= specs
        return names

    def _pip_dists(self) -> Iterable[dict]:
        patterns = ("lib/python*/site-packages/*.dist-info", "Lib/site-packages/*.dist-info")
        for pattern in patterns:
            for dist in self.prefix.glob(pattern):
                installer = dist / "INSTALLER"
                if installer.exists() and installer.read_text().strip() == "conda":
                    continue
                try:
                    meta = HeaderParser().parsestr((dist / "METADATA").read_text())
                except OSError:
                    continue
                yield {
                    "name": meta["Name"],
                    "version": meta["Version"],
                    "requires": meta.get_all("Requires-Dist") or [],
                    "requested": (dist / "REQUESTED").exists(),
                }


def default_source_path() -> str:
    """The environment to show when none is given: explicit override, active env, or ours."""
    return os.environ.get("CONDA_VISUALIZER_SOURCE") or os.environ.get("CONDA_PREFIX") or sys.prefix


def conda_executable() -> str | None:
    return os.environ.get("CONDA_EXE") or shutil.which("conda") or shutil.which("mamba")


def list_environments() -> dict[str, str]:
    """Map of display name to prefix for every environment conda knows about."""
    exe = conda_executable()
    envs: dict[str, str] = {}
    if exe:
        try:
            out = subprocess.run(
                [exe, "env", "list", "--json"], capture_output=True, text=True, timeout=30
            )
            prefixes = json.loads(out.stdout).get("envs", [])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            prefixes = []
        for prefix in prefixes:
            path = Path(prefix)
            name = "base" if (path / "condabin").exists() else path.name
            label = name if name not in envs else f"{name} ({path.parent})"
            envs[label] = str(path)
    if os.environ.get("CONDA_PREFIX") and os.environ["CONDA_PREFIX"] not in envs.values():
        envs[f"active ({Path(os.environ['CONDA_PREFIX']).name})"] = os.environ["CONDA_PREFIX"]
    return envs


# Lockfiles ----------------------------------------------------------------


@dataclass
class PixiLockSource(Source):
    """``pixi.lock``: environments reference packages by URL; records live in ``packages``."""

    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.kind = "pixi.lock"
        envs = self.data.get("environments") or {}
        self.environments = list(envs)
        platforms: list[str] = []
        for env in envs.values():
            for plat in env.get("packages", {}):
                if plat not in platforms:
                    platforms.append(plat)
        self.platforms = platforms

    def graph(self, platform=None, environment=None) -> DependencyGraph:
        environment = environment or self.default_environment
        platform = platform or self.default_platform or ""
        env = self.data["environments"].get(environment)
        if env is None:
            raise SourceError(f"Environment {environment!r} not in lockfile")
        records = {}
        for pkg in self.data.get("packages", []):
            key = pkg.get("conda") or pkg.get("pypi") or pkg.get("url")
            # v4/v5 list one record per platform under the same URL key.
            records.setdefault(key, []).append(pkg)
        g = DependencyGraph(source=self.label, platform=platform)
        pypi_names: dict[str, str] = {}
        for ref in env.get("packages", {}).get(platform, []):
            kind = "pypi" if ("pypi" in ref or ref.get("kind") == "pypi") else "conda"
            url = ref.get("conda") or ref.get("pypi") or ref.get("url")
            candidates = records.get(url, [{}])
            pkg = next(
                (c for c in candidates if c.get("platform", platform) == platform), candidates[0]
            )
            info = parse_filename(url) if kind == "conda" else {"url": url}
            name = pkg.get("name") or info.get("name")
            if not name:
                g.warnings.append(f"Skipping unnamed package {url}")
                continue
            attrs = {
                "version": pkg.get("version") or info.get("version"),
                "build": pkg.get("build") or info.get("build"),
                "channel": channel_name(pkg.get("channel", "")) or info.get("channel"),
                "subdir": pkg.get("subdir") or info.get("subdir"),
                "size": pkg.get("size"),
                "license": pkg.get("license"),
                "url": url,
                "kind": kind,
            }
            if kind == "pypi":
                g.add_package(name, requires_dist=pkg.get("requires_dist", []), **attrs)
                continue
            g.add_package(name, **attrs)
            pypi_names[normalize_pypi(name)] = name
            for purl in pkg.get("purls") or []:
                if purl.startswith("pkg:pypi/"):
                    pypi_names[normalize_pypi(purl[9:].split("@")[0])] = name
            for dep in pkg.get("depends", []):
                g.add_dependency(name, spec_name(dep), dep)
        _link_pypi(g, pypi_names)
        return g.finalize()


@dataclass
class CondaLockSource(Source):
    """conda-lock unified lockfile: a flat ``package`` list tagged by platform."""

    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.kind = "conda-lock"
        packages = self.data.get("package", [])
        platforms = self.data.get("metadata", {}).get("platforms") or []
        self.platforms = list(platforms) or sorted({p["platform"] for p in packages})

    def graph(self, platform=None, environment=None) -> DependencyGraph:
        platform = platform or self.default_platform or ""
        g = DependencyGraph(source=self.label, platform=platform)
        pypi_names: dict[str, str] = {}
        pip_packages = []
        for pkg in self.data.get("package", []):
            if pkg.get("platform") != platform:
                continue
            name = pkg["name"]
            info = parse_filename(pkg.get("url", ""))
            if pkg.get("manager") == "pip":
                pip_packages.append(pkg)
                continue
            g.add_package(
                name,
                version=pkg.get("version"),
                build=pkg.get("build") or info.get("build"),
                channel=info.get("channel"),
                subdir=info.get("subdir"),
                url=pkg.get("url"),
                kind="conda",
            )
            pypi_names[normalize_pypi(name)] = name
            for dep, spec in (pkg.get("dependencies") or {}).items():
                g.add_dependency(name, dep, f"{dep} {spec}".strip())
        for pkg in pip_packages:
            name = pkg["name"]
            g.add_package(name, version=pkg.get("version"), url=pkg.get("url"), kind="pypi")
            for dep, spec in (pkg.get("dependencies") or {}).items():
                target = normalize_pypi(dep)
                g.add_dependency(name, pypi_names.get(target, target), f"{dep} {spec}".strip())
        if not len(g):
            raise SourceError(f"No packages for platform {platform!r}")
        return g.finalize()


@dataclass
class ExplicitSource(Source):
    """``@EXPLICIT`` URL list: nodes only, since no dependency info is stored."""

    text: str = ""

    def __post_init__(self):
        self.kind = "explicit"
        match = re.search(r"^#\s*platform:\s*(\S+)", self.text, re.M)
        self.platforms = [match.group(1)] if match else []

    def graph(self, platform=None, environment=None) -> DependencyGraph:
        g = DependencyGraph(source=self.label, platform=self.default_platform or "")
        for line in self.text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "@")):
                continue
            info = parse_filename(line.split("#")[0])
            name = info.pop("name")
            g.add_package(name, **info)
        g.warnings.append("Explicit lockfiles do not record dependencies; only packages are shown.")
        return g.finalize()


# Environment specs (solved) ----------------------------------------------


@dataclass
class EnvironmentFileSource(Source):
    """``environment.yml`` solved with py-rattler for the chosen platform."""

    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.kind = "environment.yml"
        here = current_platform()
        base = ["linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64"]
        self.platforms = list(self.data.get("platforms") or [here, *(p for p in base if p != here)])

    @property
    def channels(self) -> list[str]:
        return [c for c in self.data.get("channels") or ["conda-forge"] if c != "nodefaults"]

    @property
    def specs(self) -> tuple[list[str], list[str]]:
        conda, pip = [], []
        for dep in self.data.get("dependencies") or []:
            if isinstance(dep, dict):
                pip.extend(dep.get("pip", []))
            else:
                conda.append(str(dep))
        return conda, pip

    def graph(self, platform=None, environment=None) -> DependencyGraph:
        platform = platform or self.default_platform or current_platform()
        conda_specs, pip_specs = self.specs
        records = solve(conda_specs, self.channels, platform)
        g = DependencyGraph(source=self.label, platform=platform)
        requested = {spec_name(s) for s in conda_specs}
        for rec in records:
            name = rec.name.normalized
            g.add_package(
                name,
                version=str(rec.version),
                build=rec.build,
                channel=channel_name(str(rec.channel or "")),
                subdir=rec.subdir,
                size=rec.size,
                license=rec.license,
                url=str(rec.url),
                requested=name in requested,
            )
            for dep in rec.depends:
                g.add_dependency(name, spec_name(dep), dep)
        for spec in pip_specs:
            g.add_package(normalize_pypi(spec_name(spec)), kind="pypi", requested=True)
        if pip_specs:
            g.warnings.append("pip dependencies are listed but not resolved.")
        return g.finalize()


def _virtual_packages(platform: str):
    if platform == current_platform():
        return rattler.VirtualPackage.detect()
    defaults = {
        "linux": [("__unix", "0"), ("__linux", "5.10"), ("__glibc", "2.28")],
        "osx": [("__unix", "0"), ("__osx", "13.0")],
        "win": [("__win", "0")],
    }
    return [
        rattler.GenericVirtualPackage(rattler.PackageName(n), rattler.Version(v), "0")
        for n, v in defaults.get(platform.split("-")[0], [])
    ]


def solve(specs: list[str], channels: list[str], platform: str):
    if rattler is None:
        raise SourceError("Solving environment.yml requires py-rattler")
    coro = rattler.solve(
        sources=channels,
        specs=specs,
        platforms=[platform, "noarch"],
        virtual_packages=_virtual_packages(platform),
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Panel callbacks may already run inside an event loop.
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


# DOT ----------------------------------------------------------------------


@dataclass
class DotSource(Source):
    """Any DOT digraph; ``version``/``kind``/``spec`` attributes are picked up."""

    text: str = ""

    def __post_init__(self):
        self.kind = "dot"

    def graph(self, platform=None, environment=None) -> DependencyGraph:
        parsed = pydot.graph_from_dot_data(self.text)
        if not parsed:
            raise SourceError("Could not parse DOT input")
        g = DependencyGraph(source=self.label)

        def unquote_id(value) -> str:
            value = str(value)
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            return value

        def attrs(obj) -> dict:
            return {k: unquote_id(v) for k, v in obj.get_attributes().items()}

        def walk(graph):
            for node in graph.get_nodes():
                name = unquote_id(node.get_name())
                if name in ("node", "edge", "graph", "\\n"):
                    continue
                a = attrs(node)
                kind = a.pop("kind", None) or ("virtual" if name.startswith("__") else "node")
                requested = a.pop("requested", "False") in ("True", "true", "1")
                g.add_package(
                    name,
                    kind=kind,
                    requested=requested,
                    **{k: a[k] for k in ("version", "build", "channel") if k in a},
                )
            for edge in graph.get_edges():
                src, dst = unquote_id(edge.get_source()), unquote_id(edge.get_destination())
                a = attrs(edge)
                for end in (src, dst):
                    if end not in g.graph or "kind" not in g.graph.nodes[end]:
                        g.add_package(end, kind="virtual" if end.startswith("__") else "node")
                g.add_dependency(src, dst, a.get("spec") or a.get("label", ""))
            for sub in graph.get_subgraphs():
                walk(sub)

        walk(parsed[0])
        return g.finalize()


# Detection ----------------------------------------------------------------


def open_source(source: str | os.PathLike | bytes, filename: str | None = None) -> Source:
    """Identify ``source`` and return a :class:`Source` for it.

    ``source`` may be a prefix path, an environment name, a file path, or
    the raw bytes/text of an uploaded file (pass ``filename`` as a hint).
    """
    if isinstance(source, bytes):
        return _from_text(source.decode("utf-8", errors="replace"), filename or "upload")
    source = str(source)
    path = Path(source).expanduser()
    if path.is_dir():
        if (path / "conda-meta").is_dir():
            return PrefixSource(label=str(path), prefix=path)
        for candidate in ("pixi.lock", "conda-lock.yml", "environment.yml"):
            if (path / candidate).exists():
                return open_source(path / candidate)
        pixi_env = path / ".pixi" / "envs" / "default"
        if (pixi_env / "conda-meta").is_dir():
            return PrefixSource(label=str(pixi_env), prefix=pixi_env)
        raise SourceError(f"{path} is not a conda environment")
    if path.is_file():
        return _from_text(path.read_text(errors="replace"), str(path))
    if "\n" in source or source.lstrip().startswith(("digraph", "strict", "graph")):
        return _from_text(source, filename or "text")
    envs = list_environments()
    for label, prefix in envs.items():
        if source in (label, Path(prefix).name):
            return PrefixSource(label=label, prefix=Path(prefix))
    raise SourceError(f"Could not find environment or file {source!r}")


def _from_text(text: str, label: str) -> Source:
    name = Path(label).name.lower()
    stripped = re.sub(r"^\s*(//|#).*$", "", text, flags=re.M).lstrip()
    if name.endswith((".dot", ".gv")) or re.match(r"(strict\s+)?(di)?graph\b", stripped, re.I):
        return DotSource(label=label, text=text)
    if re.search(r"^@EXPLICIT\s*$", text, re.M):
        return ExplicitSource(label=label, text=text)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SourceError(f"{label}: not valid YAML or DOT ({exc})") from exc
    if not isinstance(data, dict):
        raise SourceError(f"{label}: unrecognized format")
    if "environments" in data and "packages" in data:
        return PixiLockSource(label=label, data=data)
    if "package" in data and "metadata" in data:
        return CondaLockSource(label=label, data=data)
    if "dependencies" in data:
        return EnvironmentFileSource(label=label, data=data)
    raise SourceError(f"{label}: unrecognized format")


def load(
    source: str | os.PathLike | bytes,
    platform: str | None = None,
    environment: str | None = None,
    filename: str | None = None,
) -> DependencyGraph:
    """Shortcut for ``open_source(source).graph(platform, environment)``."""
    return open_source(source, filename).graph(platform, environment)
