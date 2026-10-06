# conda-visualizer

Interactive dependency graphs for conda environments, built with [Panel](https://panel.holoviz.org) and [Cytoscape.js](https://js.cytoscape.org).

It reads an installed prefix, a `pixi.lock`, a `conda-lock.yml`, an explicit lockfile, an `environment.yml` (solved with py-rattler) or a DOT file, and renders a graph you can search, highlight, isolate and export.

## Usage

```bash
conda-visualizer serve [SOURCE]                    # interactive app
conda-visualizer convert SOURCE -o graph.html      # standalone interactive HTML
conda-visualizer convert SOURCE -o graph.dot       # DOT (or .json)
conda-visualizer leaves|cycles|depends|whoneeds -p PREFIX   # conda-tree style queries
```

The exported HTML needs no Python server. Search, highlighting, isolation, layout switching and PNG/SVG export run in the browser. Panel and Cytoscape are loaded from CDNs unless `--inline` is passed, which embeds Bokeh/Panel and leaves only Cytoscape on esm.sh.

The graph is also available as a component:

```python
from conda_visualizer.component import CytoscapeGraph
from conda_visualizer.sources import load

CytoscapeGraph(load("pixi.lock")).servable()
```

## Development

```bash
pixi install
pixi run install-browser
pixi run test
pixi run test-ui
```
