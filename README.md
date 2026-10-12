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

## Newer versions and version changes

The app's Versions section asks the environment's channels (read from the installed package URLs, so it works for prefixes and lockfiles) for every available version. Packages with a newer one get a double border and `1.2 ↑ 1.5` under their name; pre-releases are ignored.

Pick a package and a version and press Preview change to re-solve the environment with that package pinned. Every other package stays where it is unless the solver has to move it. The graph then fills upgraded packages green, downgraded ones lilac, new ones bright green, rebuilds grey and shows removed ones dashed, fading everything the change does not touch. Select a package to see its normal relations again. The Changes tab lists them. Preview updating everything solves without keeping the installed versions. Requested packages keep their original spec (for example `python=3.12`), except the one you pin.

The same is available from the command line:

```bash
conda-visualizer outdated -p PREFIX                  # newer versions
conda-visualizer plan -p PREFIX lib=2.0              # what switching lib to 2.0 changes
conda-visualizer plan -p PREFIX --update-all
conda-visualizer convert PREFIX --latest --set lib=2.0 -o graph.html
```

Lookups use py-rattler and need network access to the channels. "Newest" means the newest version in the channel for the platform; the solver decides whether it is compatible with the rest of the environment. PyPI packages are not checked.

The exported HTML needs no Python server. Search, highlighting, isolation, layout switching and PNG/SVG export run in the browser. Cytoscape is embedded from the compiled bundle in the wheel. Bokeh and Panel load from their CDNs unless `--inline` is passed, which leaves only the brand fonts (Inter, Space Grotesk, Space Mono) on Google Fonts, with Arial as the offline fallback.

## In the browser

A Pyodide build runs the whole app client-side: https://cdn.holoviz.org/panel-conda-visualizer/v0.1.0a0/app/index.html

There is no filesystem or conda install to read there, so it starts empty and takes uploads: DOT files (from `conda tree deptree --dot` or `conda-visualizer convert -o graph.dot`), `pixi.lock`, `conda-lock.yml` and explicit lockfiles. `environment.yml` and the Versions tools need py-rattler, which has no Pyodide build, so they are unavailable.

The build installs the wheel from the URL it will be served at, since browsers cannot fetch the `file:` URLs `panel convert` writes for local wheels:

```bash
pixi run -e build build-browser --base-url http://localhost:8123     # local testing
python -m http.server 8123 --directory build/browser
```

The graph is also available as a component:

```python
from conda_visualizer.component import CytoscapeGraph
from conda_visualizer.sources import load

CytoscapeGraph(load("pixi.lock")).servable()
```

## Development

The wheel ships a bundle compiled with `panel compile`. In a source checkout run `pixi run compile` first, otherwise Cytoscape is loaded from esm.sh.

```bash
pixi install
pixi run compile
pixi run install-browser
pixi run test
pixi run test-ui
```

Pushing a `vX.Y.Z` tag publishes to PyPI (trusted publishing, `pypi` environment) and syncs the bundle to `s3://cdn.holoviz.org/panel-conda-visualizer/vX.Y.Z/`, which needs the `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` repository secrets.
