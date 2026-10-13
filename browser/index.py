"""Entry point for the Pyodide build; see scripts/build_browser.py."""

import os

# Offline builds (panel convert --offline) serve Panel from static/panel/, but
# panel-material-ui still loads its bundle from its own CDN root, so point that at
# the copy build_browser.py writes to static/panel-material-ui/ before importing it.
_panel_root = os.environ.get("PANEL_CDN_ROOT", "")
if _panel_root.endswith("/static/panel/"):
    os.environ.setdefault(
        "PANEL_MATERIAL_UI_CDN_ROOT", _panel_root.removesuffix("panel/") + "panel-material-ui/v"
    )

from conda_visualizer.app import create_app  # noqa: E402

create_app().servable(title="Conda Visualizer")
