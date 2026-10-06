import pytest

pytest.importorskip("playwright")

import panel as pn
from panel.tests.util import serve_component, wait_until
from playwright.sync_api import expect

from conda_visualizer import app as app_module
from conda_visualizer.app import Explorer
from conda_visualizer.component import CytoscapeGraph
from conda_visualizer.export import export_html
from conda_visualizer.sources import load

pytestmark = pytest.mark.ui

ELEMENTS = {
    "nodes": [
        {"id": "app", "label": "app", "version": "1.0", "in": 0, "out": 2, "requested": True},
        {"id": "lib", "label": "lib", "version": "1.5", "in": 1, "out": 1},
        {"id": "libextra", "label": "libextra", "in": 1, "out": 0},
        {"id": "base", "label": "base", "in": 1, "out": 0},
        {"id": "other", "label": "other", "in": 0, "out": 0},
    ],
    "edges": [
        {"source": "app", "target": "lib", "spec": "lib >=1"},
        {"source": "lib", "target": "base", "spec": "base"},
        {"source": "app", "target": "libextra", "spec": "libextra"},
    ],
}


@pytest.fixture(autouse=True)
def cleanup():
    yield
    pn.state.kill_all_servers()
    pn.state._indicators.clear()


def cy(page, expr):
    """Evaluate ``expr`` with ``cy`` bound to the Cytoscape instance."""
    script = f"(el) => {{ const cy = el._cy; return {expr}; }}"
    return page.locator(".cy-container").evaluate(script)


def emit(page, event, node=None):
    """Emit a Cytoscape event on ``node`` (or the core).

    The return value is discarded on purpose: ``emit`` returns the Cytoscape
    object, and Playwright serializing that object graph exhausts the page heap.
    """
    target = f"cy.$id('{node}')" if node else "cy"
    cy(page, f"({target}.emit('{event}'), null)")


def wait_for_nodes(page, n):
    wait_until(lambda: cy(page, "cy.nodes().length") == n, page)


def test_renders_elements_and_updates(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=800)
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    assert cy(page, "cy.edges().length") == 3
    graph.elements = {"nodes": ELEMENTS["nodes"][:2], "edges": ELEMENTS["edges"][:1]}
    wait_for_nodes(page, 2)


def test_tap_selects_and_highlights(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=800, show_details=True)
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    emit(page, "tap", "lib")
    wait_until(lambda: graph.selected == "lib", page)
    assert cy(page, "cy.$('.depends').nodes().map(n => n.id())") == ["base"]
    assert cy(page, "cy.$('.whoneeds').nodes().map(n => n.id())") == ["app"]
    assert sorted(cy(page, "cy.$('node.faded').map(n => n.id())")) == ["libextra", "other"]
    expect(page.locator(".cy-card")).to_contain_text("Depends on (1)")

    graph.highlight = "depends"
    wait_until(lambda: cy(page, "cy.$('.whoneeds').length") == 0, page)

    emit(page, "tap")
    wait_until(lambda: graph.selected is None, page)
    assert cy(page, "cy.$('.faded').length") == 0


def test_python_selection_reaches_js(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=800)
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    graph.selected = "base"
    wait_until(lambda: cy(page, "cy.$('.focus').map(n => n.id())") == ["base"], page)


def test_toolbar_search_cycles_matches(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=800)
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    search = page.locator(".cy-search")
    search.fill("lib")
    wait_until(lambda: graph.search == "lib", page)
    expect(page.locator(".cy-count")).to_have_text("2 matches")
    # Exact and prefix matches rank first; Enter steps through them.
    search.press("Enter")
    wait_until(lambda: graph.selected == "lib", page)
    search.press("Enter")
    wait_until(lambda: graph.selected == "libextra", page)
    search.press("Shift+Enter")
    wait_until(lambda: graph.selected == "lib", page)
    search.press("Escape")
    wait_until(lambda: graph.search == "" and graph.selected is None, page)


def test_python_search_reaches_js(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=800)
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    graph.search = "bas"
    expect(page.locator(".cy-search")).to_have_value("bas")
    wait_until(lambda: cy(page, "cy.$('.match').map(n => n.id())") == ["base"], page)


def test_toolbar_controls_sync(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=900)
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    page.locator(".cy-toolbar select").nth(0).select_option("grid")
    wait_until(lambda: graph.layout == "grid", page)
    page.locator(".cy-toolbar select").nth(1).select_option("LR")
    wait_until(lambda: graph.rank_dir == "LR", page)
    graph.toolbar = False
    expect(page.locator(".cy-toolbar")).to_be_hidden()


def test_isolate(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=900, selected="lib")
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    page.get_by_role("button", name="Isolate").click()
    wait_until(lambda: cy(page, "cy.nodes(':visible').length") == 3, page)
    page.get_by_role("button", name="Show all").click()
    wait_until(lambda: cy(page, "cy.nodes(':visible').length") == 5, page)


def test_dblclick_and_export(page):
    graph = CytoscapeGraph(elements=ELEMENTS, height=500, width=900, filename="deps")
    clicked = []
    graph.on_dblclick(clicked.append)
    serve_component(page, graph)
    wait_for_nodes(page, 5)
    emit(page, "dbltap", "base")
    wait_until(lambda: clicked == ["base"], page)
    with page.expect_download() as download:
        page.get_by_role("button", name="SVG").click()
    assert download.value.suggested_filename == "deps.svg"
    with page.expect_download() as download:
        graph.export("png")
    assert download.value.suggested_filename == "deps.png"


def test_explorer_app(page, prefix, monkeypatch):
    monkeypatch.setattr(app_module, "discovered_environments", lambda: {})
    explorer = Explorer(source=str(prefix))
    serve_component(page, explorer)
    n = len(explorer.view)
    wait_for_nodes(page, n)
    explorer.focus = "base"
    wait_until(lambda: cy(page, "cy.nodes().length") == len(explorer.view) < n, page)
    emit(page, "tap", "lib")
    wait_until(lambda: "### lib" in explorer._details.object, page)
    emit(page, "dbltap", "app")
    wait_until(lambda: explorer.focus == "app", page)


def test_static_export_works_without_server(page, prefix, tmp_path):
    path = export_html(load(prefix), tmp_path / "graph.html")
    page.goto(path.as_uri())
    wait_until(lambda: page.locator(".cy-container").count() == 1, page, timeout=15000)
    wait_for_nodes(page, len(load(prefix)))
    search = page.locator(".cy-search")
    search.fill("lib")
    search.press("Enter")
    wait_until(lambda: cy(page, "cy.$(':selected').map(n => n.id())") == ["lib"], page)
    expect(page.locator(".cy-card")).to_contain_text("Needed by (3)")
    page.get_by_role("button", name="Isolate").click()
    wait_until(lambda: cy(page, "cy.nodes(':visible').length") < len(load(prefix)), page)


def test_explorer_html_download_is_self_contained(page, prefix, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "discovered_environments", lambda: {})
    explorer = Explorer(source=str(prefix))
    serve_component(page, explorer)
    wait_for_nodes(page, len(explorer.view))
    with page.expect_download() as download:
        page.locator("button", has_text="HTML").first.click()
    saved = tmp_path / "saved.html"
    download.value.save_as(saved)

    # Saved from a server session, the page must not reference server-relative assets.
    assert "components/conda_visualizer" not in saved.read_text()
    exported = page.context.new_page()
    failed = []
    exported.on("requestfailed", lambda request: failed.append(request.url))
    exported.goto(saved.as_uri())
    wait_until(lambda: exported.locator(".cy-container").count() == 1, exported, timeout=15000)
    expect(exported.locator(".cy-toolbar")).to_have_css("display", "flex")
    assert failed == []
