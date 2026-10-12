"""Panel app for exploring conda dependency graphs.

Serve with ``panel serve src/conda_visualizer/app.py`` or ``conda-visualizer serve``;
pass ``?source=<prefix|env name|file>`` to open a specific input.
"""

from __future__ import annotations

import functools
import io
from pathlib import Path

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param

# Absolute imports so `panel serve app.py` can run this file as a script.
from conda_visualizer.component import CytoscapeGraph
from conda_visualizer.export import standalone
from conda_visualizer.graph import DependencyGraph
from conda_visualizer.sources import (
    BROWSER,
    Source,
    SourceError,
    default_source_path,
    list_environments,
    open_source,
    rattler,
)
from conda_visualizer.theme import (
    FAVICON,
    FONTS_CSS,
    LOGO,
    RAW_CSS,
    STANDALONE_CSS,
    THEME_CONFIG,
)
from conda_visualizer.updates_panel import UpdatesPanel

pn.extension(
    "tabulator",
    notifications=True,
    throttled=True,
    css_files=[FONTS_CSS],
    raw_css=[RAW_CSS],
)

#: conda-tree's ``--small`` hides these; nearly every package depends on them.
COMMON = ("python", "conda")

#: Above this many nodes the full view is replaced by the requested packages.
MAX_NODES = 1500


@functools.cache
def discovered_environments() -> dict[str, str]:
    return list_environments()


class Explorer(pn.viewable.Viewer):
    """Load an environment definition and browse its dependency graph."""

    source = param.String(default="", doc="Prefix, environment name or file path.")

    environment_name = param.Selector(
        default=None, objects=[], allow_None=True, label="Conda environment"
    )

    platform = param.Selector(default=None, objects=[], allow_None=True)

    environment = param.Selector(
        default=None, objects=[], allow_None=True, label="Lockfile environment"
    )

    focus = param.Selector(default=None, objects=[], allow_None=True, label="Focus package")

    direction = param.Selector(default="both", objects=["both", "depends", "whoneeds"])

    depth = param.Integer(default=0, bounds=(0, 10), label="Depth (0 = unlimited)")

    exclude = param.ListSelector(default=[], objects=[], label="Stop at (keep, don't expand)")

    hide = param.ListSelector(default=[], objects=[], label="Hide packages")

    hide_virtual = param.Boolean(default=True, label="Hide virtual packages")

    small = param.Boolean(default=False, label="Hide python and conda")

    changes_only = param.Boolean(
        default=True,
        label="Show only changed packages",
        doc="While a version change is previewed, hide packages it does not touch.",
    )

    full = param.Parameter(default=None, doc="The complete DependencyGraph of the source.")

    view = param.Parameter(default=None, doc="The displayed subgraph.")

    def __init__(self, **params):
        params.setdefault("source", default_source_path())
        self._graph = CytoscapeGraph(
            sizing_mode="stretch_width", height=680, min_height=400, show_details=True
        )
        self._details = pn.pane.Markdown(sizing_mode="stretch_width", margin=(0, 10))
        self._tree = pn.pane.Markdown(sizing_mode="stretch_width", margin=(0, 10))
        self._stats = pn.pane.Markdown(margin=(0, 10))
        self._message = pmui.Alert(
            visible=False, severity="warning", margin=(5, 10), sizing_mode="stretch_width"
        )
        self._table = pn.widgets.Tabulator(
            pd.DataFrame(),
            sizing_mode="stretch_width",
            height=420,
            theme="materialize",
            show_index=False,
            disabled=True,
            pagination=None,
            header_filters=True,
        )
        self._leaves = pn.widgets.Tabulator(
            pd.DataFrame(),
            sizing_mode="stretch_width",
            height=420,
            theme="materialize",
            show_index=False,
            disabled=True,
            header_filters=True,
        )
        self._cycles = pn.pane.Markdown(sizing_mode="stretch_width")
        self._updates = UpdatesPanel()
        self._keep_view = False
        self._source_obj: Source | None = None
        self._uploaded: tuple | None = None
        super().__init__(**params)

        envs = discovered_environments()
        self.param.environment_name.objects = list(envs)
        self._envs = envs
        self._build_widgets()
        self._build_layout()
        self._wire()
        self._load_source()

    # Construction ---------------------------------------------------------

    def _build_widgets(self):
        self._env_select = pmui.AutocompleteInput.from_param(
            self.param.environment_name,
            placeholder="Pick an installed environment",
            search_strategy="includes",
            min_characters=0,
            restrict=True,
            sizing_mode="stretch_width",
        )
        self._path_input = pmui.TextInput.from_param(
            self.param.source,
            label="Path",
            placeholder="prefix, pixi.lock, conda-lock.yml, environment.yml or .dot",
            sizing_mode="stretch_width",
        )
        # The browser build has no filesystem or conda install to browse.
        self._env_select.visible = self._path_input.visible = not BROWSER
        accept = ".lock,.yml,.yaml,.dot,.gv,.txt"
        if BROWSER:
            # Panel's pyodide worker (<= 1.10.0b2) lets back-to-back messages overwrite
            # each other, losing pmui's upload chunk, and fails to decode binary data;
            # the core widget syncs a single string property. Drop once fixed upstream.
            self._upload = pn.widgets.FileInput(
                accept=accept, sizing_mode="stretch_width", margin=(5, 10)
            )
        else:
            self._upload = pmui.FileInput(
                label="Upload file", accept=accept, sizing_mode="stretch_width"
            )
        self._intro = pmui.Alert(
            object=(
                "Running in your browser. Upload a DOT file (e.g. from "
                "`conda tree deptree --dot` or `conda-visualizer convert`), a pixi.lock, "
                "conda-lock.yml or explicit lockfile."
            ),
            severity="info",
            visible=BROWSER,
            margin=(5, 10),
            sizing_mode="stretch_width",
        )
        self._platform_select = pmui.Select.from_param(
            self.param.platform, sizing_mode="stretch_width", visible=False
        )
        self._env_lock_select = pmui.Select.from_param(
            self.param.environment, sizing_mode="stretch_width", visible=False
        )
        self._focus_input = pmui.AutocompleteInput.from_param(
            self.param.focus,
            placeholder="Type to search…",
            search_strategy="includes",
            min_characters=1,
            restrict=True,
            sizing_mode="stretch_width",
        )
        self._direction = pmui.RadioButtonGroup.from_param(
            self.param.direction,
            size="small",
            sizing_mode="stretch_width",
        )
        self._depth = pmui.IntSlider.from_param(
            self.param.depth, margin=(10, 20), sizing_mode="stretch_width"
        )
        self._exclude = pmui.MultiChoice.from_param(self.param.exclude, sizing_mode="stretch_width")
        self._hide = pmui.MultiChoice.from_param(self.param.hide, sizing_mode="stretch_width")
        self._hide_virtual = pmui.Switch.from_param(self.param.hide_virtual)
        self._small = pmui.Switch.from_param(self.param.small)
        self._clear_focus = pmui.Button(
            label="Clear focus",
            icon="close",
            variant="outlined",
            size="small",
            sizing_mode="stretch_width",
        )
        self._versions = pmui.Switch.from_param(
            self._graph.param.show_versions, label="Show versions"
        )
        self._changes_only_switch = pmui.Switch.from_param(self.param.changes_only, visible=False)
        self._latest = pmui.Switch.from_param(
            self._graph.param.show_latest, label="Show newest versions"
        )
        self._html = pmui.FileDownload(
            label="HTML",
            icon="public",
            variant="text",
            color="light",
            callback=self._html_file,
            filename="dependencies.html",
            description="Standalone interactive page; works without a server",
        )
        self._dot = pmui.FileDownload(
            label="DOT",
            icon="download",
            variant="text",
            color="light",
            callback=self._dot_file,
            filename="dependencies.dot",
        )
        self._json = pmui.FileDownload(
            label="JSON",
            icon="data_object",
            variant="text",
            color="light",
            callback=self._json_file,
            filename="dependencies.json",
        )

    def _section(self, title):
        return pmui.Typography(title, variant="overline", margin=(15, 10, 0, 10))

    def _versions_section(self) -> list:
        # Looking up and previewing versions solves with py-rattler, which has
        # no Pyodide build and is an optional extra elsewhere.
        if rattler is None:
            return []
        return [
            self._latest,
            pmui.Divider(),
            self._section("Versions"),
            self._updates,
            self._changes_only_switch,
        ]

    def _build_layout(self):
        sidebar = pmui.Column(
            self._section("Source"),
            self._intro,
            self._env_select,
            self._path_input,
            self._upload,
            self._platform_select,
            self._env_lock_select,
            self._message,
            pmui.Divider(),
            self._section("View"),
            self._focus_input,
            self._direction,
            self._depth,
            self._clear_focus,
            self._exclude,
            self._hide,
            self._hide_virtual,
            self._small,
            self._versions,
            *self._versions_section(),
            sizing_mode="stretch_width",
        )
        tabs = pmui.Tabs(
            ("Package", self._details),
            ("Packages", self._table),
            ("Leaves", self._leaves),
            *([("Changes", self._updates.table)] if rattler else []),
            ("Cycles", self._cycles),
            ("Tree", self._tree),
            dynamic=True,
            sizing_mode="stretch_width",
        )
        main = pmui.Column(
            self._stats,
            pmui.Paper(self._graph, sizing_mode="stretch_width"),
            tabs,
            sizing_mode="stretch_width",
            margin=10,
        )
        self._page = pmui.Page(
            title="Conda Visualizer",
            logo=LOGO,
            favicon=FAVICON,
            theme_config=THEME_CONFIG,
            sidebar=[sidebar],
            main=[main],
            header=[
                pmui.Row(
                    pn.layout.HSpacer(),
                    self._html,
                    self._dot,
                    self._json,
                    sizing_mode="stretch_width",
                )
            ],
            sidebar_width=330,
        )

    def _wire(self):
        self._upload.param.watch(self._on_upload, ["value", "filename"])
        self._clear_focus.on_click(self._on_clear_focus)
        self._graph.on_dblclick(self._on_dblclick)
        self._graph.param.watch(self._update_details, "selected")
        self._table.on_click(self._on_row_click)
        self._updates.table.on_click(self._on_row_click)
        self._updates.param.watch(self._on_current, "current")
        self._updates.param.watch(self._on_plan, "plan")
        self._graph.param.watch(lambda e: setattr(self._updates, "selected", e.new), "selected")
        self._leaves.on_click(self._on_row_click)
        self._page.param.watch(self._sync_theme, "dark_theme")
        self._graph.dark_theme = self._page.dark_theme

    def __panel__(self):
        return self._page

    # Loading --------------------------------------------------------------

    @param.depends("environment_name", watch=True)
    def _on_environment_name(self):
        if self.environment_name:
            self.source = self._envs[self.environment_name]

    @param.depends("source", watch=True)
    def _load_source(self):
        if not self.source:
            return
        self._open(lambda: open_source(self.source))

    def _on_upload(self, *events):
        # In Pyodide value and filename arrive as separate updates; reload when the
        # filename catches up so detection and labels use the right name.
        value, filename = self._upload.value, self._upload.filename
        if not value or (value, filename) == self._uploaded:
            return
        self._uploaded = (value, filename)
        self._open(lambda: open_source(value, filename=filename))

    def _open(self, factory):
        self._graph.loading = True
        try:
            source = factory()
        except Exception as exc:
            # Unexpected parser failures must reach the user too, not just the log.
            expected = isinstance(exc, (SourceError, OSError))
            self._show_message(str(exc) if expected else f"{type(exc).__name__}: {exc}", "error")
            self._graph.loading = False
            return
        self._source_obj = source
        values = {"platform": source.default_platform, "environment": source.default_environment}
        unchanged = values == {"platform": self.platform, "environment": self.environment}
        with pn.io.hold():
            self.param.platform.objects = source.platforms
            self.param.environment.objects = source.environments
            # update() fires _build_graph once for both values; same values fire nothing.
            self.param.update(values)
            self._platform_select.visible = len(source.platforms) > 1
            self._env_lock_select.visible = len(source.environments) > 1
            if unchanged:
                self._build_graph()

    @param.depends("platform", "environment", watch=True)
    def _build_graph(self):
        if self._source_obj is None:
            return
        self._graph.loading = True
        try:
            full = self._source_obj.graph(self.platform, self.environment)
        except Exception as exc:
            self._show_message(f"{type(exc).__name__}: {exc}", "error")
            self._graph.loading = False
            return
        names = full.packages
        with pn.io.hold():
            self.param.focus.objects = names
            self.param.exclude.objects = names
            self.param.hide.objects = names
            self.param.update(
                focus=self.focus if self.focus in full else None,
                exclude=[n for n in self.exclude if n in full],
                hide=[n for n in self.hide if n in full],
                full=full,
            )
            self._updates.reset(full)
            if full.warnings:
                self._show_message("\n".join(full.warnings), "warning")
            else:
                self._message.visible = False

    def _on_plan(self, event):
        self._changes_only_switch.visible = event.new is not None

    def _on_current(self, event):
        """Show the annotated graph, with the active preview applied."""
        if event.new is not None and event.new is not self.full:
            # Looking up versions or previewing a change must not move the selected package.
            self._keep_view = True
            try:
                self.full = event.new
            finally:
                self._keep_view = False

    def _show_message(self, text, severity):
        self._message.param.update(object=text, severity=severity, visible=True)

    # View -----------------------------------------------------------------

    @param.depends(
        "full",
        "focus",
        "direction",
        "depth",
        "exclude",
        "hide",
        "hide_virtual",
        "small",
        "changes_only",
        watch=True,
    )
    def _update_view(self):
        full: DependencyGraph | None = self.full
        if full is None:
            return
        hide = set(self.hide)
        if self.hide_virtual:
            hide |= set(full.without_kinds(["virtual"]))
        if self.small:
            hide |= set(COMMON)
        focus = [self.focus] if self.focus else []
        nodes = full.neighborhood(focus, self.direction, self.depth or None, self.exclude, hide)
        truncated = False
        if not focus and len(nodes) > MAX_NODES:
            requested = [n for n in nodes if full.graph.nodes[n].get("requested")]
            nodes = full.neighborhood(requested or full.leaves(), "depends", 1, hide=hide)
            truncated = True
        nodes = self._only_changes(full, nodes)
        view = full.subgraph(nodes)
        with pn.io.hold():
            self.view = view
            elements = view.to_elements()
            if self._keep_view:
                elements["keep_view"] = True
            self._graph.param.update(elements=elements, filename=self._export_name)
            if not self._graph.selected or self._graph.selected not in view:
                self._graph.selected = self.focus
            self._graph.loading = False
            self._update_tables(full, view)
            self._update_stats(full, view, truncated)
            self._update_details()

    def _only_changes(self, full: DependencyGraph, nodes: set[str]) -> set[str]:
        """While a preview is active, keep the changed packages and what the pinned ones need."""
        data = dict(full.graph.nodes(data=True))
        changed = {n for n in nodes if data[n].get("change")}
        if not self.changes_only or not any(d.get("change") for d in data.values()):
            return nodes
        pinned = [n for n in changed if data[n].get("target")]
        context = {dep for n in pinned for dep in full.graph.successors(n)}
        return changed | (context & nodes)

    def _update_stats(self, full, view, truncated):
        where = full.source + (f" · {full.platform}" if full.platform else "")
        shown = f"showing {len(view)} of {len(full)} packages, {view.graph.number_of_edges()} edges"
        note = (
            f" (limited to direct dependencies of requested packages above {MAX_NODES})"
            if truncated
            else ""
        )
        late = sum(1 for _, d in full.graph.nodes(data=True) if d.get("outdated"))
        newer = (
            f"  ·  {late} outdated"
            if any("latest" in d for _, d in full.graph.nodes(data=True))
            else ""
        )
        self._stats.object = (
            f"**{where}**  ·  {shown}  ·  {len(full.leaves())} leaves  ·  "
            f"{len(full.cycles())} cycles{newer}{note}"
        )

    def _update_tables(self, full, view):
        self._table.value = pd.DataFrame(view.records())
        self._leaves.value = pd.DataFrame([r for r in full.records() if r["dependents"] == 0])
        cycles = full.cycles()
        self._cycles.object = (
            "\n".join(f"- {' → '.join([*c, c[0]])}" for c in cycles)
            if cycles
            else "No dependency cycles."
        )

    def _update_details(self, *_):
        full = self.full
        name = self._graph.selected or self.focus
        if full is None or not name or name not in full:
            self._details.object = (
                "Click a node to inspect it. Double-click a node to focus the view on it."
            )
            self._tree.object = "Select a package to see its dependency tree."
            return
        info = full.details(name)
        fields = [(k, info.get(k)) for k in ("version", "build", "channel", "kind", "license")]
        lines = [f"### {name}"]
        if info.get("requested"):
            lines.append("*explicitly requested*")
        lines += [f"**{k}**: {v}  " for k, v in fields if v]
        if info.get("change"):
            was = f" (was {info['old_version']})" if info.get("old_version") else ""
            lines.append(f"**preview**: {info['change']}{was}  ")
        if info.get("latest"):
            lines.append(f"**newest available**: {info['latest']}  ")
        if info.get("size"):
            lines.append(f"**size**: {info['size'] / 1e6:.2f} MB  ")
        for key, title in (("depends", "Depends on"), ("whoneeds", "Needed by")):
            items = info[key]
            lines.append(f"\n**{title}** ({len(items)})\n")
            lines += [f"- `{dep}` {spec}" if spec else f"- `{dep}`" for dep, spec in items[:200]]
        self._details.object = "\n".join(lines)
        reverse = self.direction == "whoneeds"
        tree = full.tree(name, reverse=reverse, exclude=self.exclude)
        self._tree.object = f"```\n{tree}\n```"

    # Handlers -------------------------------------------------------------

    def _on_dblclick(self, name):
        self.focus = name

    def _on_clear_focus(self, event):
        self.focus = None

    def _on_row_click(self, event):
        if event.column != "name" or not event.value:
            return
        name = event.value
        if self.view is not None and name in self.view:
            self._graph.selected = name
        else:
            self.focus = name

    def _sync_theme(self, event):
        self._graph.dark_theme = event.new

    # Export ---------------------------------------------------------------

    @property
    def _export_name(self):
        name = Path(self.full.source).name if self.full is not None else "graph"
        # Only strip input-file suffixes; prefix names like "dev3.14" contain dots too.
        for suffix in (".dot", ".gv", ".lock", ".yml", ".yaml", ".txt"):
            name = name.removesuffix(suffix)
        return f"{name}-{self.focus}" if self.focus else name

    def _dot_file(self):
        self._dot.filename = f"{self._export_name}.dot"
        return io.StringIO(self.view.to_dot() if self.view is not None else "digraph {}\n")

    def _html_file(self):
        self._html.filename = f"{self._export_name}.html"
        buffer = io.StringIO()
        if self.view is not None:
            page = standalone(
                self.view,
                title=f"Dependencies of {self._export_name}",
                layout=self._graph.layout,
                rank_dir=self._graph.rank_dir,
                highlight=self._graph.highlight,
                show_versions=self._graph.show_versions,
                selected=self._graph.selected,
            )
            with pn.config.set(raw_css=[STANDALONE_CSS]):
                page.save(buffer, title=self._export_name, resources="cdn")
        buffer.seek(0)
        return buffer

    def _json_file(self):
        self._json.filename = f"{self._export_name}.json"
        return io.StringIO(self.view.to_json() if self.view is not None else "{}")


def create_app(source: str | None = None) -> Explorer:
    args = pn.state.session_args or {}
    if source is None and "source" in args:
        source = args["source"][0].decode()
    return Explorer(source=source) if source else Explorer()


# Only when this file is the served script; since Panel 1.10 pn.state.served is also
# true when a converted (Pyodide) app imports this module, which would add a second app.
if __name__.startswith("bokeh_app"):
    create_app().servable(title="Conda Visualizer")
