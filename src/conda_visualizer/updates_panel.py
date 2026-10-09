"""Sidebar controls for looking up newer versions and previewing version changes."""

from __future__ import annotations

import asyncio

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param

from conda_visualizer.graph import DependencyGraph
from conda_visualizer.updates import (
    Availability,
    Plan,
    UpdateError,
    apply_plan,
    fetch_availability,
    outdated,
    plan_change,
    requested_specs,
    with_availability,
)

HINT_CHECK = "Press *Check latest versions* to see which packages have newer versions."
HINT_SELECT = (
    "To see what a different version would change, click a package in the graph or type its "
    "name below, choose a version and press *Preview change*."
)

PLAN_KINDS = (
    ("upgrade", "upgraded"),
    ("downgrade", "downgraded"),
    ("add", "added"),
    ("remove", "removed"),
    ("rebuild", "rebuilt"),
)


def describe_counts(plan: Plan) -> str:
    counts = plan.counts()
    parts = [f"{counts[kind]} {label}" for kind, label in PLAN_KINDS if counts[kind]]
    return ", ".join(parts) or "no changes"


class UpdatesPanel(pn.viewable.Viewer):
    """Check the channels for newer versions and preview switching a package's version.

    ``graph`` is the installed environment. ``current`` is what to display: that
    graph with newest versions attached and, while a preview is active, the
    changes the preview makes.
    """

    graph = param.Parameter(default=None, doc="The environment as installed.")

    current = param.Parameter(default=None, doc="Graph to display.")

    plan = param.Parameter(default=None, doc="The active preview, if any.")

    availability = param.Parameter(default=None, doc="Versions offered by the channels.")

    package = param.Selector(default=None, objects=[], allow_None=True, label="Package")

    version = param.Selector(default=None, objects=[], allow_None=True, label="Version")

    selected = param.String(default=None, allow_None=True, doc="Package selected in the graph.")

    scope = param.Selector(
        default="requested",
        objects={"Requested packages": "requested", "All packages": "all"},
        label="Mark outdated",
        doc="Which packages get the newer-version marker; dependencies follow from requested ones.",
    )

    channels = param.List(default=None, allow_None=True, doc="Override the graph's channels.")

    def __init__(self, **params):
        super().__init__(**params)
        self.table = pn.widgets.Tabulator(
            pd.DataFrame(columns=["name", "change", "from", "to"]),
            sizing_mode="stretch_width",
            height=420,
            theme="materialize",
            show_index=False,
            disabled=True,
            pagination=None,
            header_filters=True,
        )
        self._installed: DependencyGraph | None = None  # as loaded, without lookups
        self._base: DependencyGraph | None = None  # with newest versions attached
        self._status = pmui.Alert(
            visible=False, severity="info", margin=(5, 10), sizing_mode="stretch_width"
        )
        self._summary = pn.pane.Markdown(margin=(0, 10), sizing_mode="stretch_width")
        self._check = pmui.Button(
            label="Check latest versions",
            icon="update",
            variant="outlined",
            size="small",
            sizing_mode="stretch_width",
        )
        self._scope = pmui.Select.from_param(self.param.scope, sizing_mode="stretch_width")
        self._package = pmui.AutocompleteInput.from_param(
            self.param.package,
            placeholder="Type to search…",
            search_strategy="includes",
            min_characters=0,
            restrict=True,
            sizing_mode="stretch_width",
        )
        self._version = pmui.Select.from_param(self.param.version, sizing_mode="stretch_width")
        self._preview = pmui.Button(
            label="Preview change",
            icon="visibility",
            variant="contained",
            size="small",
            sizing_mode="stretch_width",
            disabled=True,
        )
        self._update_all = pmui.Button(
            label="Preview updating everything",
            icon="upgrade",
            variant="outlined",
            size="small",
            sizing_mode="stretch_width",
            disabled=True,
        )
        self._clear = pmui.Button(
            label="Clear preview",
            icon="close",
            variant="text",
            size="small",
            sizing_mode="stretch_width",
            disabled=True,
        )
        self._check.on_click(self._on_check)
        self._preview.on_click(self._on_preview)
        self._update_all.on_click(self._on_update_all)
        self._clear.on_click(lambda event: self.clear())

    def __panel__(self):
        return pmui.Column(
            self._summary,
            self._check,
            self._scope,
            self._package,
            self._version,
            self._preview,
            self._status,
            self._update_all,
            self._clear,
            sizing_mode="stretch_width",
        )

    # State ------------------------------------------------------------------

    def reset(self, graph: DependencyGraph | None) -> None:
        """Start over with a freshly loaded environment."""
        self._installed = self._base = graph
        names = (
            sorted(n for n, d in graph.graph.nodes(data=True) if d.get("kind") == "conda")
            if graph
            else []
        )
        with param.parameterized.batch_call_watchers(self):
            self.param.package.objects = names
            self.param.update(availability=None, plan=None, package=None, current=graph)
        self._set_versions()
        self.table.value = pd.DataFrame(columns=["name", "change", "from", "to"])
        self._status.visible = False
        self._summary.object = HINT_CHECK if graph is not None else ""
        self._sync_buttons()

    def _installed_version(self) -> str | None:
        node = self._base.graph.nodes.get(self.package) if self._base and self.package else None
        return node["version"] if node else None

    def _sync_buttons(self) -> None:
        ready = self._base is not None and self.availability is not None
        different = self.version is not None and self.version != self._installed_version()
        self._preview.disabled = not (ready and self.package and different)
        self._update_all.disabled = not ready
        self._clear.disabled = self.plan is None

    def _say(self, text: str, severity: str = "info") -> None:
        self._status.param.update(object=text, severity=severity, visible=bool(text))

    def _compose(self) -> None:
        if self._base is None:
            return
        self.current = apply_plan(self._base, self.plan) if self.plan else self._base

    def _set_versions(self) -> None:
        """Offer the package's versions, newest first, preselecting the newest."""
        node = self._base.graph.nodes.get(self.package) if self._base and self.package else None
        if node is None:
            self.param.version.objects = []
            self.version = None
            return
        installed = node["version"]
        if self.availability is None:
            self.param.version.objects = {f"{installed} (installed)": installed}
            self.version = installed
            return
        found = self.availability.choices(self.package, installed)
        options = {}
        for i, version in enumerate(found):
            tags = [
                t for t, hit in (("installed", version == installed), ("latest", i == 0)) if hit
            ]
            options[f"{version} ({', '.join(tags)})" if tags else version] = version
        self.param.version.objects = options
        self.version = found[0]

    @param.depends("package", watch=True)
    def _on_package(self):
        self._set_versions()
        self._sync_buttons()

    @param.depends("version", watch=True)
    def _on_version(self):
        self._sync_buttons()

    @param.depends("selected", watch=True)
    def _on_selected(self):
        if self.selected and self.selected in self.param.package.objects:
            self.package = self.selected

    # Lookups ----------------------------------------------------------------

    def check_latest(self) -> None:
        self._apply_availability(self._fetch())

    def _fetch(self) -> Availability:
        return fetch_availability(self._base, channels=self.channels)

    def _apply_availability(self, availability: Availability) -> None:
        # Annotating copies the graph, so keep the plan (if any) on top of the new copy.
        self.availability = availability
        self._annotate()
        self._set_versions()
        self._sync_buttons()
        self._say("")
        self._summary.object = self._availability_text() + self._plan_text() + self._next_step()

    def _annotate(self) -> None:
        self._base = with_availability(
            self._installed, self.availability, requested_only=self.scope == "requested"
        )
        self._compose()

    @param.depends("scope", watch=True)
    def _on_scope(self):
        if self.availability is not None:
            self._annotate()
            self._summary.object = self._availability_text() + self._plan_text() + self._next_step()

    def _next_step(self) -> str:
        if self.plan is not None or self.package:
            return ""
        return "\n\n" + HINT_SELECT

    # Previews ---------------------------------------------------------------

    def preview(self, package: str | None = None, version: str | None = None) -> None:
        package, version = package or self.package, version or self.version
        self._apply_plan(self._solve({package: version}, False), f"{package} {version}")

    def preview_update_all(self) -> None:
        self._apply_plan(self._solve({}, True), "everything")

    def _solve(self, targets: dict[str, str], update_all: bool) -> Plan:
        return plan_change(self._base, targets, update_all=update_all, channels=self.channels)

    def _apply_plan(self, plan: Plan, label: str) -> None:
        if not plan:
            self.plan = None
            self.table.value = pd.DataFrame(columns=["name", "change", "from", "to"])
            self._compose()
            self._sync_buttons()
            self._say(f"Switching {label} changes nothing.", "info")
            self._summary.object = ""
            return
        self.plan = plan
        self.table.value = pd.DataFrame(plan.rows())
        self._compose()
        self._sync_buttons()
        self._say("")
        self._summary.object = (
            f"**Preview: {label}** changes {len(plan.changes)} packages: {describe_counts(plan)}."
            "\n\nThe full list is in the *Changes* tab."
        )

    def _availability_text(self) -> str:
        if self.availability is None:
            return ""
        late = outdated(self._base)
        hint = " (double border in the graph)" if late else ""
        if self.scope == "requested":
            total = len(requested_specs(self._base))
            return f"**{len(late)} of {total} requested packages** have a newer version{hint}."
        total = len(self.availability.versions)
        return f"**{len(late)} of {total} packages** have a newer version{hint}."

    def _plan_text(self) -> str:
        return f"\n\nPreview active: {describe_counts(self.plan)}." if self.plan else ""

    def clear(self) -> None:
        self.plan = None
        self.table.value = pd.DataFrame(columns=["name", "change", "from", "to"])
        self._compose()
        self._sync_buttons()
        self._say("")
        self._summary.object = self._availability_text()

    # Handlers ---------------------------------------------------------------
    # The channel and solver calls block, so they run in a worker thread and
    # the button shows a spinner meanwhile.

    async def _run(self, button, work, apply):
        if self._base is None:
            return
        button.loading = True
        try:
            result = await asyncio.to_thread(work)
        except UpdateError as exc:
            self._say(str(exc), "error")
            return
        except Exception as exc:
            self._say(f"{type(exc).__name__}: {exc}", "error")
            return
        finally:
            button.loading = False
        apply(result)

    async def _on_check(self, event):
        await self._run(self._check, self._fetch, self._apply_availability)

    async def _on_preview(self, event):
        package, version = self.package, self.version
        await self._run(
            self._preview,
            lambda: self._solve({package: version}, False),
            lambda plan: self._apply_plan(plan, f"{package} {version}"),
        )

    async def _on_update_all(self, event):
        await self._run(
            self._update_all,
            lambda: self._solve({}, True),
            lambda plan: self._apply_plan(plan, "everything"),
        )
