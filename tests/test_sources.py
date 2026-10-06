import os
from pathlib import Path

import pytest
import yaml

from conda_visualizer import sources
from conda_visualizer.sources import (
    CondaLockSource,
    DotSource,
    EnvironmentFileSource,
    ExplicitSource,
    PixiLockSource,
    PrefixSource,
    SourceError,
    channel_name,
    load,
    open_source,
    parse_filename,
    parse_requires_dist,
)
from tests.conftest import CF, DOT, EXPLICIT, _pixi_lock


@pytest.mark.parametrize(
    ("url", "name"),
    [
        (f"{CF}/linux-64", "conda-forge"),
        ("https://repo.anaconda.com/pkgs/main/osx-arm64", "pkgs/main"),
        ("https://prefix.dev/my-channel/noarch/", "my-channel"),
        ("conda-forge", "conda-forge"),
        ("", ""),
    ],
)
def test_channel_name(url, name):
    assert channel_name(url) == name


def test_parse_filename():
    info = parse_filename(f"{CF}/linux-64/python_abi-3.14-8_cp314.conda")
    assert info["name"] == "python_abi"
    assert (info["version"], info["build"], info["subdir"]) == ("3.14", "8_cp314", "linux-64")
    assert info["channel"] == "conda-forge"
    assert parse_filename(f"{CF}/noarch/six-1.17.0-pyhe01879c_1.tar.bz2")["name"] == "six"


def test_parse_requires_dist_markers():
    assert parse_requires_dist("Foo_Bar>=1") == "foo-bar"
    assert parse_requires_dist("sphinx; extra == 'docs'") is None
    assert parse_requires_dist("argparse; python_version < '3'") is None


def test_prefix(prefix):
    source = open_source(prefix)
    assert isinstance(source, PrefixSource)
    g = source.graph()
    assert g.platform == "linux-64"
    assert g.node("lib")["channel"] == "conda-forge"
    assert g.node("__glibc")["kind"] == "virtual"
    assert g.node("ghost")["kind"] == "missing"
    assert g.leaves() == ["Extra_Pkg", "app", "tool"]


def test_prefix_requested_from_history(prefix):
    g = load(prefix)
    requested = {n for n, d in g.graph.nodes(data=True) if d.get("requested")}
    # lib was added then removed from the history specs; pip REQUESTED counts too.
    assert requested == {"app", "tool", "Extra_Pkg"}


def test_prefix_pip_packages_link_to_conda(prefix):
    g = load(prefix)
    assert g.node("Extra_Pkg")["kind"] == "pypi"
    assert g.depends("Extra_Pkg") == ["lib", "python"]


def test_prefix_without_pip(prefix):
    g = PrefixSource(label="x", prefix=prefix, include_pip=False).graph()
    assert "Extra_Pkg" not in g


def test_directory_without_env_errors(tmp_path):
    with pytest.raises(SourceError):
        open_source(tmp_path)


@pytest.mark.parametrize("version", [6, 7])
def test_pixi_lock(tmp_path, version):
    path = tmp_path / "pixi.lock"
    path.write_text(_pixi_lock(version))
    source = open_source(path)
    assert isinstance(source, PixiLockSource)
    assert source.platforms == ["linux-64", "osx-arm64"]
    assert source.environments == ["default"]
    g = source.graph("osx-arm64")
    assert g.platform == "osx-arm64"
    assert g.node("lib")["subdir"] == "osx-arm64"
    assert g.node("app")["version"] == "1.0"
    assert g.depends("app") == ["__glibc", "lib", "python"]
    # requires_dist "pylib" resolves through the conda record's purl.
    assert g.depends("extra-pkg") == ["lib"]


def test_pixi_lock_unknown_environment(pixi_lock):
    with pytest.raises(SourceError):
        open_source(pixi_lock).graph(environment="nope")


def test_pixi_lock_v4_kind_url(tmp_path):
    data = {
        "version": 4,
        "environments": {
            "default": {"packages": {"linux-64": [{"conda": f"{CF}/linux-64/app-1.0-h0.conda"}]}}
        },
        "packages": [
            {
                "kind": "conda",
                "name": "app",
                "version": "1.0",
                "build": "h0",
                "subdir": "linux-64",
                "url": f"{CF}/linux-64/app-1.0-h0.conda",
                "depends": ["lib >=1"],
            }
        ],
    }
    path = tmp_path / "pixi.lock"
    path.write_text(yaml.safe_dump(data))
    g = load(path)
    assert g.depends("app") == ["lib"]


def test_conda_lock(conda_lock):
    source = open_source(conda_lock)
    assert isinstance(source, CondaLockSource)
    assert source.platforms == ["linux-64", "osx-arm64"]
    g = source.graph("linux-64")
    assert sorted(g.graph) == ["app", "lib", "python", "rich"]
    assert g.graph.edges["app", "lib"]["spec"] == "lib >=1"
    assert g.node("rich")["kind"] == "pypi"
    # pip names are matched case-insensitively against conda packages.
    assert g.depends("rich") == ["lib"]
    assert len(source.graph("osx-arm64")) == 1


def test_explicit():
    source = open_source(EXPLICIT.encode(), filename="explicit.txt")
    assert isinstance(source, ExplicitSource)
    assert source.platforms == ["linux-64"]
    g = source.graph()
    assert sorted(g.graph) == ["app", "lib"]
    assert g.node("app")["subdir"] == "noarch"
    assert g.graph.number_of_edges() == 0
    assert g.warnings


def test_dot():
    source = open_source(DOT.encode(), filename="graph.txt")
    assert isinstance(source, DotSource)
    g = source.graph()
    assert g.depends("app") == ["__glibc", "lib"]
    assert g.depends("lib", recursive=True) == ["base", "core"]
    assert g.node("__glibc")["kind"] == "virtual"
    assert g.node("tool") == {"kind": "pypi", "requested": False, "version": "2.0"}
    assert g.graph.edges["tool", "lib"]["spec"] == "lib >=1"
    assert g.depends("x") == ["y"]  # edges inside subgraphs are kept


def test_dot_roundtrip(prefix):
    g = load(prefix)
    again = load(g.to_dot().encode(), filename="env.dot")
    assert set(again.graph.edges) == set(g.graph.edges)
    assert again.node("lib")["version"] == g.node("lib")["version"]
    assert again.node("app")["requested"] is True


def test_conda_tree_dot_detected_without_suffix(tmp_path):
    path = tmp_path / "deps"
    path.write_text('digraph {\n  "a" -> "b"\n}\n')
    assert isinstance(open_source(path), DotSource)


def test_unrecognized_yaml():
    with pytest.raises(SourceError):
        open_source(b"foo: bar\n", filename="x.yml")


def test_environment_file_specs():
    text = (
        "channels: [conda-forge, nodefaults]\ndependencies:\n  - numpy>=2\n  - pip:\n    - rich\n"
    )
    source = open_source(text.encode(), filename="environment.yml")
    assert isinstance(source, EnvironmentFileSource)
    assert source.channels == ["conda-forge"]
    assert source.specs == (["numpy>=2"], ["rich"])
    assert source.default_platform == sources.current_platform()


def test_environment_file_solved(monkeypatch):
    class Record:
        def __init__(self, name, depends):
            self.name = type("N", (), {"normalized": name})()
            self.version, self.build, self.channel, self.subdir = "1", "h0", f"{CF}/", "noarch"
            self.size, self.license, self.url, self.depends = 1, "MIT", f"{CF}/noarch/x", depends

    calls = {}

    def fake_solve(specs, channels, platform):
        calls.update(specs=specs, channels=channels, platform=platform)
        return [Record("numpy", ["python >=3.10"]), Record("python", [])]

    monkeypatch.setattr(sources, "solve", fake_solve)
    source = EnvironmentFileSource(label="env.yml", data={"dependencies": ["numpy"]})
    g = source.graph("linux-64")
    assert calls == {"specs": ["numpy"], "channels": ["conda-forge"], "platform": "linux-64"}
    assert g.depends("numpy") == ["python"]
    assert g.node("numpy")["requested"] and not g.node("python").get("requested")


@pytest.mark.skipif(not os.environ.get("CONDA_VISUALIZER_NETWORK"), reason="needs network")
def test_environment_file_rattler_solve():
    g = EnvironmentFileSource(label="x", data={"dependencies": ["python=3.12"]}).graph("linux-64")
    assert "python" in g and "__glibc" in g


def test_named_environment(monkeypatch, prefix):
    monkeypatch.setattr(sources, "list_environments", lambda: {"myenv": str(prefix)})
    source = open_source("myenv")
    assert isinstance(source, PrefixSource) and source.prefix == Path(prefix)
    with pytest.raises(SourceError):
        open_source("does-not-exist")
