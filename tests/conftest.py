import json
from pathlib import Path

import pytest

CF = "https://conda.anaconda.org/conda-forge"

#: name -> (version, build, depends); "app" and "tool" are leaves, x/y form an
#: orphaned cycle and "ghost" is depended on but not installed.
RECORDS = {
    "app": ("1.0", "py_0", ["lib >=1", "python >=3.10", "__glibc >=2.17"]),
    "tool": ("2.0", "h0", ["lib", "ghost"]),
    "lib": ("1.5", "h1", ["base"]),
    "base": ("0.1", "h2", []),
    "python": ("3.12.0", "h3", ["base"]),
    "x": ("1", "h4", ["y"]),
    "y": ("1", "h5", ["x"]),
}


@pytest.fixture
def prefix(tmp_path) -> Path:
    meta = tmp_path / "env" / "conda-meta"
    meta.mkdir(parents=True)
    for name, (version, build, depends) in RECORDS.items():
        record = {
            "name": name,
            "version": version,
            "build": build,
            "depends": depends,
            "channel": f"{CF}/linux-64",
            "subdir": "linux-64",
            "size": 1000,
            "url": f"{CF}/linux-64/{name}-{version}-{build}.conda",
        }
        (meta / f"{name}-{version}-{build}.json").write_text(json.dumps(record))
    (meta / "history").write_text(
        "==> 2026-01-01 <==\n# update specs: ['app', 'lib>=1']\n"
        "==> 2026-01-02 <==\n# remove specs: ['lib']\n# update specs: ['tool']\n"
    )
    dist = tmp_path / "env" / "lib" / "python3.12" / "site-packages" / "extra_pkg-0.3.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: Extra_Pkg\nVersion: 0.3\n"
        "Requires-Dist: python\nRequires-Dist: lib\nRequires-Dist: docs-only; extra == 'docs'\n"
    )
    (dist / "INSTALLER").write_text("pip\n")
    (dist / "REQUESTED").write_text("")
    return tmp_path / "env"


def _pixi_lock(version: int) -> str:
    def url(plat, name):
        version_, build, _ = RECORDS[name]
        return f"{CF}/{plat}/{name}-{version_}-{build}.conda"

    lines = [
        f"version: {version}",
        "environments:",
        "  default:",
        "    channels:",
        f"    - url: {CF}/",
        "    packages:",
    ]
    for plat in ("linux-64", "osx-arm64"):
        lines.append(f"      {plat}:")
        lines += [f"      - conda: {url(plat, n)}" for n in RECORDS]
        lines.append("      - pypi: https://files.example/extra_pkg-0.3-py3-none-any.whl")
    lines.append("packages:")
    for plat in ("linux-64", "osx-arm64"):
        for name, (_, _, depends) in RECORDS.items():
            lines.append(f"- conda: {url(plat, name)}")
            if depends:
                lines.append("  depends:")
                lines += [f"  - {d}" for d in depends]
            if name == "lib":
                lines += ["  purls:", "  - pkg:pypi/pylib@1.5"]
    lines += [
        "- pypi: https://files.example/extra_pkg-0.3-py3-none-any.whl",
        "  name: extra-pkg",
        "  version: '0.3'",
        "  requires_dist:",
        "  - pylib>=1",
        "  - sphinx ; extra == 'docs'",
    ]
    return "\n".join(lines) + "\n"


@pytest.fixture
def pixi_lock(tmp_path) -> Path:
    path = tmp_path / "pixi.lock"
    path.write_text(_pixi_lock(6))
    return path


CONDA_LOCK = f"""\
version: 1
metadata:
  content_hash: {{linux-64: abc}}
  channels: [{{url: conda-forge, used_env_vars: []}}]
  platforms: [linux-64, osx-arm64]
  sources: [environment.yml]
package:
- name: app
  version: '1.0'
  manager: conda
  platform: linux-64
  dependencies: {{lib: '>=1', python: '>=3.10'}}
  url: {CF}/linux-64/app-1.0-py_0.conda
  hash: {{md5: x}}
  category: main
  optional: false
- name: lib
  version: '1.5'
  manager: conda
  platform: linux-64
  dependencies: {{}}
  url: {CF}/linux-64/lib-1.5-h1.conda
  hash: {{md5: x}}
- name: python
  version: 3.12.0
  manager: conda
  platform: linux-64
  dependencies: {{}}
  url: {CF}/linux-64/python-3.12.0-h3.conda
  hash: {{md5: x}}
- name: rich
  version: 13.0.0
  manager: pip
  platform: linux-64
  dependencies: {{Lib: '*'}}
  url: https://files.example/rich-13.0.0-py3-none-any.whl
  hash: {{sha256: x}}
- name: lib
  version: '1.4'
  manager: conda
  platform: osx-arm64
  dependencies: {{}}
  url: {CF}/osx-arm64/lib-1.4-h1.conda
  hash: {{md5: x}}
"""

EXPLICIT = f"""\
# This file may be used to create an environment using:
# platform: linux-64
@EXPLICIT
{CF}/linux-64/lib-1.5-h1.conda#abc
{CF}/noarch/app-1.0-py_0.tar.bz2
"""

DOT = """\
// conda tree deptree --dot
digraph {
  "app" -> "lib"
  "app" -> "__glibc"
  lib -> base -> core
  "tool" [version="2.0", kind="pypi"]
  "tool" -> "lib" [spec="lib >=1"]
  subgraph cluster_x { x -> y }
}
"""


@pytest.fixture
def conda_lock(tmp_path) -> Path:
    path = tmp_path / "conda-lock.yml"
    path.write_text(CONDA_LOCK)
    return path
