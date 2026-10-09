import json

import pytest

pytest.importorskip("rattler")

from conda_visualizer.cli import main
from conda_visualizer.graph import DependencyGraph
from conda_visualizer.updates import (
    UpdateError,
    _is_prerelease,
    _version,
    apply_plan,
    fetch_availability,
    graph_channels,
    graph_platform,
    outdated,
    plan_change,
    relax_spec,
    with_availability,
)
from conda_visualizer.updates_panel import UpdatesPanel
from tests.conftest import make_channel

#: name -> {version: depends}. ``lib`` 2.0 needs base >=1 and a new ``helper`` and no
#: longer needs ``shim``; ``app`` 2.0 needs ``lib >=2``.
CHANNEL = {
    "base": {"0.1": [], "0.2": [], "1.0": [], "2.0rc1": []},
    "shim": {"1.0": []},
    "helper": {"1.0": []},
    "lib": {
        "1.0": ["base >=0.1,<1", "shim"],
        "1.5": ["base >=0.1,<1", "shim"],
        "2.0": ["base >=1,<1.5", "helper"],
    },
    "app": {"1.0": ["lib"], "2.0": ["lib >=2"]},
    "pinned": {"1.0": ["lib <2"]},
}


@pytest.fixture
def channel(tmp_path):
    return make_channel(
        tmp_path,
        {
            name: {version: ("0", depends) for version, depends in versions.items()}
            for name, versions in CHANNEL.items()
        },
    )


def installed(channel, packages, requested=("app",)):
    """The environment ``packages`` (name -> version) as a graph."""
    g = DependencyGraph(source="test", platform="linux-64")
    for name, version in packages.items():
        g.add_package(
            name,
            version=version,
            build="0",
            subdir="linux-64",
            url=f"{channel}/linux-64/{name}-{version}-0.conda",
            requested=name in requested,
        )
        for dep in CHANNEL[name][version]:
            g.add_dependency(name, dep.split()[0], dep)
    return g.finalize()


@pytest.fixture
def old(channel):
    return installed(channel, {"app": "1.0", "lib": "1.5", "base": "0.2", "shim": "1.0"})


def test_prerelease_detection():
    assert _is_prerelease(_version("3.15.0rc2"))
    assert _is_prerelease(_version("1.0b2"))
    assert _is_prerelease(_version("1.0.dev3"))
    assert not _is_prerelease(_version("1.1.1w"))
    assert not _is_prerelease(_version("2.0.post1"))


def test_channels_and_platform_come_from_the_graph(channel, old):
    assert graph_channels(old) == [channel]
    assert graph_platform(old) == "linux-64"


def test_availability_lists_stable_versions_newest_first(channel, old):
    found = fetch_availability(old)
    assert found.versions["base"] == ["1.0", "0.2", "0.1"]
    assert found.latest("lib") == "2.0"
    assert found.choices("base", "0.2") == ["1.0", "0.2", "0.1"]
    assert found.choices("base", "0.0.9")[-1] == "0.0.9"


def test_requested_only_marks_requested_packages(channel, old):
    found = fetch_availability(old)
    annotated = with_availability(old, found, requested_only=True)
    assert [row["name"] for row in outdated(annotated)] == ["app"]
    assert annotated.node("base")["latest"] == "1.0"


def test_versions_come_from_the_installed_channel(tmp_path, channel):
    other = make_channel(tmp_path / "other", {"lib": {"9.0": ("0", [])}})
    g = installed(channel, {"app": "1.0", "lib": "1.5", "base": "0.2", "shim": "1.0"})
    found = fetch_availability(g, channels=[channel, other])
    # lib 9.0 exists, but only in a channel lib was not installed from.
    assert found.latest("lib") == "2.0"


def test_with_availability_marks_outdated(channel, old):
    annotated = with_availability(old, fetch_availability(old))
    assert annotated.node("lib")["latest"] == "2.0"
    assert annotated.node("lib")["outdated"] is True
    assert annotated.node("shim")["outdated"] is False
    assert [row["name"] for row in outdated(annotated)] == ["app", "base", "lib"]
    assert "latest" not in old.node("lib")


def test_plan_moves_only_what_the_new_version_needs(channel, old):
    plan = plan_change(old, {"lib": "2.0"})
    kinds = {name: change.kind for name, change in plan.changes.items()}
    assert kinds == {"lib": "upgrade", "base": "upgrade", "helper": "add", "shim": "remove"}
    assert plan.changes["base"].describe() == "base 0.2 -> 1.0"
    assert "app" not in plan.changes
    assert plan.counts()["upgrade"] == 2
    assert [row["name"] for row in plan.rows()][:2] == ["base", "lib"]


def test_plan_downgrade(channel, old):
    plan = plan_change(old, {"base": "0.1"})
    assert {n: c.kind for n, c in plan.changes.items()} == {"base": "downgrade"}


def test_update_all_ignores_the_installed_versions(channel, old):
    plan = plan_change(old, update_all=True)
    assert plan.changes["app"].new_version == "2.0"
    assert plan.changes["lib"].new_version == "2.0"
    assert plan.changes["base"].new_version == "1.0"


def test_unsolvable_change_is_reported(channel):
    g = installed(
        channel,
        {"app": "1.0", "pinned": "1.0", "lib": "1.5", "base": "0.2", "shim": "1.0"},
        requested=("app", "pinned"),
    )
    with pytest.raises(UpdateError, match=r"No solution for lib 2\.0"):
        plan_change(g, {"lib": "2.0"})


def test_unknown_target(channel, old):
    with pytest.raises(UpdateError, match="nope"):
        plan_change(old, {"nope": "1"})


def test_apply_plan_marks_nodes_and_rewires_edges(channel, old):
    annotated = with_availability(old, fetch_availability(old))
    merged = apply_plan(annotated, plan_change(annotated, {"lib": "2.0"}))
    nodes = dict(merged.graph.nodes(data=True))
    assert nodes["lib"]["change"] == "upgrade"
    assert nodes["lib"]["old_version"] == "1.5"
    assert nodes["lib"]["target"] is True
    assert nodes["lib"]["outdated"] is False
    assert nodes["base"]["version"] == "1.0"
    assert nodes["helper"]["change"] == "add"
    assert nodes["shim"]["change"] == "remove"
    assert "change" not in nodes["app"]
    assert set(merged.graph.successors("lib")) == {"base", "helper"}
    # The graph the plan was applied to is untouched.
    assert annotated.node("lib")["version"] == "1.5"
    assert "change" not in annotated.node("lib")
    elements = {n["id"]: n for n in merged.to_elements()["nodes"]}
    assert elements["lib"]["change"] == "upgrade"
    assert elements["lib"]["old_version"] == "1.5"


def test_requested_spec_is_kept_for_unchanged_packages(channel):
    g = installed(channel, {"app": "1.0", "lib": "1.5", "base": "0.2", "shim": "1.0"})
    g.graph.nodes["app"]["requested_spec"] = "app <2"
    plan = plan_change(g, {"base": "0.1"})
    assert "app" not in plan.changes
    plan = plan_change(g, update_all=True)
    assert "app" not in plan.changes


def test_panel_flow(channel, old):
    panel = UpdatesPanel(channels=[channel])
    panel.reset(old)
    assert [n for n in panel.param.package.objects if n] == ["app", "base", "lib", "shim"]
    assert panel.current is old

    panel.selected = "lib"
    assert panel.package == "lib"
    assert panel.param.version.objects == {"1.5 (installed)": "1.5"}

    panel.check_latest()
    assert panel.current.node("lib")["latest"] == "2.0"
    assert panel.version == "2.0"
    assert list(panel.param.version.objects.items())[:2] == [
        ("2.0 (latest)", "2.0"),
        ("1.5 (installed)", "1.5"),
    ]
    # Only requested packages are marked by default; dependencies follow from them.
    assert "1 of 1 requested packages" in panel._summary.object
    assert panel.current.node("app")["outdated"] is True
    assert panel.current.node("base")["outdated"] is False
    assert panel.current.node("base")["latest"] == "1.0"
    panel.scope = "all"
    assert "3 of 4 packages" in panel._summary.object
    assert panel.current.node("base")["outdated"] is True
    panel.scope = "requested"

    panel.preview()
    assert panel.plan is not None
    assert panel.current.node("helper")["change"] == "add"
    assert list(panel.table.value["name"]) == ["base", "lib", "helper", "shim"]
    assert "2 upgraded, 1 added, 1 removed" in panel._summary.object

    panel.clear()
    assert panel.plan is None
    assert "change" not in panel.current.node("lib")
    assert panel.current.node("lib")["latest"] == "2.0"
    assert panel.table.value.empty

    panel.package = "base"
    panel.version = "0.2"
    panel.preview()
    assert panel.plan is None
    assert "changes nothing" in panel._status.object


def test_panel_reports_unreachable_channels(old):
    panel = UpdatesPanel(channels=["file:///does/not/exist"])
    panel.reset(old)
    with pytest.raises(UpdateError, match="Could not read the channels"):
        panel.check_latest()


@pytest.fixture
def prefix(tmp_path, channel):
    meta = tmp_path / "env" / "conda-meta"
    meta.mkdir(parents=True)
    for name, version in {"app": "1.0", "lib": "1.5", "base": "0.2", "shim": "1.0"}.items():
        record = {
            "name": name,
            "version": version,
            "build": "0",
            "depends": CHANNEL[name][version],
            "channel": f"{channel}/linux-64",
            "subdir": "linux-64",
            "url": f"{channel}/linux-64/{name}-{version}-0.conda",
        }
        (meta / f"{name}-{version}-0.json").write_text(json.dumps(record))
    (meta / "history").write_text("# update specs: ['app']\n")
    return tmp_path / "env"


def test_cli_outdated(prefix, capsys):
    main(["outdated", "-p", str(prefix), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {"name": "app", "version": "1.0", "latest": "2.0"},
        {"name": "base", "version": "0.2", "latest": "1.0"},
        {"name": "lib", "version": "1.5", "latest": "2.0"},
    ]


def test_cli_plan(prefix, capsys):
    main(["plan", "-p", str(prefix), "lib==2.0"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["upgrade", "base", "0.2", "->", "1.0"]
    assert any(line.startswith("add") and "helper" in line for line in out)
    assert any(line.startswith("remove") and "shim" in line for line in out)


def test_cli_plan_rejects_malformed_pins(prefix):
    with pytest.raises(SystemExit):
        main(["plan", "-p", str(prefix), "lib"])


def test_cli_convert_marks_changes(prefix, tmp_path):
    out = tmp_path / "plan.json"
    main(["convert", str(prefix), "--set", "lib=2.0", "--latest", "-o", str(out)])
    nodes = {n["id"]: n for n in json.loads(out.read_text())["nodes"]}
    assert nodes["lib"]["change"] == "upgrade"
    assert nodes["lib"]["old_version"] == "1.5"
    assert nodes["helper"]["change"] == "add"
    assert nodes["app"]["latest"] == "2.0"


def test_build_pins_in_requested_specs_do_not_block_changes(channel):
    g = installed(channel, {"app": "1.0", "lib": "1.5", "base": "0.2", "shim": "1.0"})
    # conda records exact installs like these; the double == form even parses to a build of "=0".
    g.graph.nodes["app"]["requested_spec"] = "app==1.0==0"
    g.graph.nodes["lib"].update(requested=True, requested_spec="lib==1.5=0")
    plan = plan_change(g, {"lib": "2.0"})
    assert plan.changes["lib"].new_version == "2.0"
    assert "app" not in plan.changes
    assert plan_change(g, {"base": "0.1"}).changes["base"].kind == "downgrade"


def test_relax_spec():
    assert relax_spec("numpy") == "numpy"
    assert relax_spec("numpy >=1.2,<2") == "numpy >=1.2,<2"
    assert relax_spec("numpy==1.2=py313_0") == "numpy"
    assert relax_spec("numpy==1.2==py313_0") == "numpy"
    assert relax_spec("python=3.13=*_cp313") == "python"
    assert relax_spec("pkg[version='>=2']") == "pkg[version='>=2']"
    assert relax_spec("not a [ spec") == "not"
