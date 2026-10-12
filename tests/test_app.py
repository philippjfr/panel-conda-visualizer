import json

import pytest

from conda_visualizer import app as app_module
from conda_visualizer.app import Explorer
from conda_visualizer.cli import main
from conda_visualizer.component import CytoscapeGraph
from conda_visualizer.export import export_html
from conda_visualizer.sources import load


@pytest.fixture(autouse=True)
def no_conda(monkeypatch):
    monkeypatch.setattr(app_module, "discovered_environments", lambda: {})


def test_component_from_graph(prefix):
    view = CytoscapeGraph(load(prefix))
    ids = {n["id"] for n in view.elements["nodes"]}
    assert {"app", "lib", "__glibc"} <= ids


def test_component_dblclick_callback():
    view = CytoscapeGraph()
    clicked = []
    view.on_dblclick(clicked.append)
    view._handle_msg({"type": "dblclick", "id": "lib"})
    assert clicked == ["lib"]


def test_explorer_loads_prefix(prefix):
    explorer = Explorer(source=str(prefix))
    nodes = {n["id"] for n in explorer._graph.elements["nodes"]}
    assert "__glibc" not in nodes  # virtual packages hidden by default
    assert {"app", "lib", "ghost"} <= nodes
    assert len(explorer._table.value) == len(nodes)
    assert "x → y → x" in explorer._cycles.object


def test_explorer_focus_direction_depth(prefix):
    explorer = Explorer(source=str(prefix))
    explorer.focus = "lib"
    assert set(explorer.view.graph) == {"lib", "base", "app", "tool", "Extra_Pkg"}
    explorer.direction = "depends"
    assert set(explorer.view.graph) == {"lib", "base"}
    explorer.param.update(focus="app", depth=1)
    assert set(explorer.view.graph) == {"app", "lib", "python"}
    explorer.small = True
    assert "python" not in explorer.view


def test_explorer_selection_updates_details(prefix):
    explorer = Explorer(source=str(prefix))
    explorer._graph.selected = "lib"
    assert "### lib" in explorer._details.object
    assert "`app` lib >=1" in explorer._details.object
    assert "lib==1.5" in explorer._tree.object


def test_explorer_dblclick_focuses(prefix):
    explorer = Explorer(source=str(prefix))
    explorer._graph._handle_msg({"type": "dblclick", "id": "tool"})
    assert explorer.focus == "tool"


def test_explorer_platform_switch(pixi_lock):
    explorer = Explorer(source=str(pixi_lock))
    assert explorer._platform_select.visible
    assert explorer.full.platform in ("linux-64", "osx-arm64")
    explorer.platform = "linux-64"
    assert explorer.full.node("lib")["subdir"] == "linux-64"


def test_explorer_upload(prefix):
    explorer = Explorer(source=str(prefix))
    explorer._upload.filename = "deps.dot"
    explorer._upload.value = b'digraph { "a" -> "b" }'
    assert sorted(explorer.full.graph) == ["a", "b"]


def test_explorer_bad_source_shows_error(prefix, tmp_path):
    explorer = Explorer(source=str(prefix))
    explorer.source = str(tmp_path / "missing")
    assert explorer._message.visible and explorer._message.severity == "error"
    assert "app" in explorer.full  # previous graph is kept


def test_explorer_downloads(prefix):
    explorer = Explorer(source=str(prefix))
    explorer.focus = "lib"
    assert '"lib" -> "base"' in explorer._dot_file().read()
    assert json.loads(explorer._json_file().read())["nodes"]
    html = explorer._html_file().read()
    assert "<html" in html.lower() and "cytoscape" in html
    assert explorer._html.filename == "env-lib.html"


def test_export_html(prefix, tmp_path):
    text = export_html(load(prefix), tmp_path / "graph.html").read_text()
    assert "esm.sh/cytoscape" in text
    assert "Extra_Pkg" in text


def test_cli_queries(prefix, capsys):
    main(["leaves", "-p", str(prefix)])
    assert capsys.readouterr().out.split() == ["Extra_Pkg", "app", "tool"]
    main(["leaves", "-p", str(prefix), "--with-cycles", "--json"])
    assert json.loads(capsys.readouterr().out) == ["Extra_Pkg", "app", "tool", "x"]
    main(["cycles", "-p", str(prefix)])
    assert capsys.readouterr().out.strip() == "x -> y -> x"
    main(["depends", "app", "-p", str(prefix), "-r", "--exclude", "lib,python"])
    assert capsys.readouterr().out.split() == ["__glibc", "lib", "python"]
    main(["whoneeds", "base", "-p", str(prefix), "-t"])
    assert capsys.readouterr().out.startswith("base==0.1\n")


def test_cli_missing_package(prefix, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["depends", "nope", "-p", str(prefix)])
    assert exc.value.code == 1


def test_cli_convert(prefix, tmp_path, capsys):
    main(["convert", str(prefix), "--focus", "lib", "--direction", "depends"])
    out = capsys.readouterr().out
    assert '"lib" -> "base"' in out and '"app"' not in out
    main(["convert", str(prefix), "-o", str(tmp_path / "g.json")])
    data = json.loads((tmp_path / "g.json").read_text())
    assert "__glibc" not in {n["id"] for n in data["nodes"]}
    main(["convert", str(prefix), "-o", str(tmp_path / "g.html")])
    assert (tmp_path / "g.html").stat().st_size > 1000


@pytest.fixture
def browser_mode(monkeypatch):
    from conda_visualizer import sources

    monkeypatch.setattr(sources, "BROWSER", True)
    monkeypatch.setattr(app_module, "BROWSER", True)


def test_browser_mode_sources(browser_mode):
    from conda_visualizer import sources

    assert sources.default_source_path() == ""
    assert sources.list_environments() == {}


def test_browser_mode_is_upload_first(browser_mode):
    import panel as pn

    explorer = Explorer()
    assert explorer.full is None and not explorer._message.visible
    assert not explorer._env_select.visible and not explorer._path_input.visible
    assert explorer._intro.visible
    # pmui's chunked binary upload does not survive Panel's pyodide worker.
    assert type(explorer._upload) is pn.widgets.FileInput


def test_upload_value_before_filename(browser_mode):
    explorer = Explorer()
    loads = []
    original = explorer._open
    explorer._open = lambda factory: (loads.append(1), original(factory))
    dot = b'digraph { "a" -> "b" }'
    # Pyodide delivers the two properties as separate updates.
    explorer._upload.value = dot
    explorer._upload.filename = "deps.dot"
    assert explorer.full.source == "deps.dot"
    explorer._upload.param.update(value=dot, filename="deps.dot")
    assert len(loads) == 2  # value alone, then with its name; the repeat is skipped
    explorer.focus = "a"
    assert explorer._export_name == "deps-a"


def test_version_tools_hidden_without_rattler(monkeypatch, prefix):
    monkeypatch.setattr(app_module, "rattler", None)
    explorer = Explorer(source=str(prefix))
    assert explorer._versions_section() == []
    assert explorer._page.main[0][2]._names == ["Package", "Packages", "Leaves", "Cycles", "Tree"]
