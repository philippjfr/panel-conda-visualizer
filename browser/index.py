"""Entry point for the Pyodide build; see scripts/build_browser.py."""

from conda_visualizer.app import create_app

create_app().servable(title="Conda Visualizer")
