import pydot
import pytest

from conda_visualizer.graph import DependencyGraph, spec_name


def make_graph() -> DependencyGraph:
    g = DependencyGraph(source="test")
    for name in ("app", "tool", "lib", "base", "python", "x", "y"):
        g.add_package(name, version="1.0")
    for pkg, dep, spec in [
        ("app", "lib", "lib >=1"),
        ("app", "python", "python >=3.10"),
        ("app", "__glibc", "__glibc >=2.17"),
        ("tool", "lib", "lib"),
        ("tool", "ghost", "ghost"),
        ("lib", "base", "base"),
        ("python", "base", "base"),
        ("x", "y", "y"),
        ("y", "x", "x"),
    ]:
        g.add_dependency(pkg, dep, spec)
    return g.finalize()


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("python >=3.10", "python"),
        ("python_abi 3.14.* *_cp314", "python_abi"),
        ("conda-forge::numpy >=2", "numpy"),
        ("requests[socks]>=2; python_version>'3'", "requests"),
        ("__glibc >=2.17,<3.0.a0", "__glibc"),
    ],
)
def test_spec_name(spec, name):
    assert spec_name(spec) == name


def test_implicit_nodes_are_classified():
    g = make_graph()
    assert g.node("__glibc")["kind"] == "virtual"
    assert g.node("ghost")["kind"] == "missing"
    assert g.node("lib")["kind"] == "conda"
    assert any("ghost" in w for w in g.warnings)


def test_duplicate_specs_are_merged():
    g = DependencyGraph()
    g.add_package("pkg")
    g.add_dependency("pkg", "python", "python >=3.10")
    g.add_dependency("pkg", "python", "python >=3.10")
    g.add_dependency("pkg", "python", "python <4")
    assert g.graph.edges["pkg", "python"]["spec"] == "python >=3.10, python <4"


def test_leaves_cycles_roots():
    g = make_graph()
    assert g.leaves() == ["app", "tool"]
    assert g.cycles() == [["x", "y"]]
    # The orphaned x <-> y cycle is reported like `leaves --with-cycles`.
    assert g.roots() == ["app", "tool", "x"]


def test_depends_and_whoneeds():
    g = make_graph()
    assert g.depends("app") == ["__glibc", "lib", "python"]
    assert g.depends("app", recursive=True) == ["__glibc", "base", "lib", "python"]
    assert g.whoneeds("base") == ["lib", "python"]
    assert g.whoneeds("base", recursive=True) == ["app", "lib", "python", "tool"]
    with pytest.raises(KeyError):
        g.depends("nope")


def test_exclude_keeps_node_but_stops_traversal():
    g = make_graph()
    assert g.depends("app", recursive=True, exclude=["lib", "python"]) == [
        "__glibc",
        "lib",
        "python",
    ]


def test_neighborhood_direction_depth_hide():
    g = make_graph()
    assert g.neighborhood(["lib"], "depends") == {"lib", "base"}
    assert g.neighborhood(["lib"], "whoneeds") == {"lib", "app", "tool"}
    assert g.neighborhood(["app"], "depends", depth=1) == {"app", "lib", "python", "__glibc"}
    assert "python" not in g.neighborhood(["app"], hide=["python"])
    # A hidden package can't be used as a bridge to its own dependencies.
    assert g.neighborhood(["python"], "whoneeds", hide=["python"]) == {"python", "app"}
    assert g.neighborhood([], hide=["x"]) == set(g.graph) - {"x"}


def test_tree_marks_repeated_subtrees():
    g = make_graph()
    g.add_dependency("base", "core", "core")
    g.graph.nodes["core"]["kind"] = "conda"
    tree = g.tree("app")
    assert tree.splitlines()[0] == "app==1.0"
    assert "├─ lib 1.0 [required: lib >=1]" in tree
    assert "dependencies of base displayed above" in tree
    reverse = g.tree("base", reverse=True)
    assert "dependent packages of lib displayed above" not in reverse
    assert "└─ python 1.0 [required: base]" in reverse


def test_tree_stops_at_cycles():
    assert make_graph().tree("x").splitlines() == [
        "x==1.0",
        "└─ y 1.0 [required: y]",
        "   └─ x 1.0 [required: x]",
    ]


def test_details_and_records():
    g = make_graph()
    info = g.details("lib")
    assert info["depends"] == [("base", "base")]
    assert info["whoneeds"] == [("app", "lib >=1"), ("tool", "lib")]
    rows = {r["name"]: r for r in g.records()}
    assert rows["lib"]["dependents"] == 2
    assert rows["base"]["depends"] == 0


def test_to_elements():
    els = make_graph().to_elements()
    nodes = {n["id"]: n for n in els["nodes"]}
    assert nodes["lib"]["in"] == 2 and nodes["lib"]["out"] == 1
    assert {"id": "app->lib", "source": "app", "target": "lib", "spec": "lib >=1"} in els["edges"]


def test_dot_is_parseable_and_conda_tree_compatible():
    g = make_graph()
    plain = g.to_dot(attributes=False)
    assert '  "app" -> "lib"' in plain.splitlines()
    assert "[" not in plain
    parsed = pydot.graph_from_dot_data(g.to_dot())
    assert parsed is not None
    assert len(parsed[0].get_edges()) == g.graph.number_of_edges()


def test_dot_escapes_quotes():
    g = DependencyGraph()
    g.add_package('we"ird', version='1"0')
    assert '"we\\"ird" [version="1\\"0"' in g.to_dot()
