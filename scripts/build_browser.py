"""Build the in-browser (Pyodide) app with ``panel convert``.

Writes the wheel plus ``index.html``/``index.js`` to ``--out``. The app installs
the wheel from ``--base-url``: Panel turns local wheel paths into ``file:`` URLs,
which a browser cannot fetch, so the wheel has to be served from somewhere.

    python scripts/build_browser.py --base-url http://localhost:8123
    python scripts/build_browser.py \\
        --base-url https://cdn.holoviz.org/panel-conda-visualizer/v0.1.0a1/app

The Panel and Bokeh versions loaded in the browser are the ones installed where
this runs. To convert with a different Panel than the build environment, build
the wheel first and pass it with ``--wheel`` from an environment with that Panel.

``--bundle`` (Panel with bundle support) instead copies Pyodide and every wheel
into ``pyodide/`` next to the app, so it needs no CDN or PyPI at runtime:

    python scripts/build_browser.py --bundle --panel-version local --wheel WHEEL
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from panel.io.convert import convert_apps

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Build the in-browser (Pyodide) app.")
    parser.add_argument("--base-url", help="URL the output directory is served at")
    parser.add_argument("--out", default="build/browser", help="output directory")
    parser.add_argument("--wheel", help="use this wheel instead of building one")
    parser.add_argument(
        "--runtime",
        default="pyodide-worker",
        choices=["pyodide-worker", "pyodide"],
        help="run Python in a web worker (keeps the page responsive) or on the main thread",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="write only index.html: no PWA files and no wheel copy, so --base-url must "
        "already host the wheel",
    )
    parser.add_argument(
        "--bundle",
        action="store_true",
        help="bundle Pyodide and every wheel next to the app so it runs without network "
        "access (needs a Panel with bundle support, node and npm)",
    )
    parser.add_argument(
        "--panel-version",
        default="auto",
        help="'local' uses the Panel and Bokeh wheels built in a Panel checkout",
    )
    args = parser.parse_args(argv)
    if not (args.base_url or args.bundle):
        parser.error("--base-url is required unless --bundle is given")

    out = (ROOT / args.out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    # Bundled and standalone builds take the wheel from elsewhere, not from out.
    copy_wheel = not (args.standalone or args.bundle)
    if args.wheel:
        wheel = Path(shutil.copy(args.wheel, out)) if copy_wheel else Path(args.wheel).resolve()
    else:
        wheel_dir = out if copy_wheel else Path(tempfile.mkdtemp())
        # The hatch build hook compiles the JS bundle into the wheel.
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheel_dir)],
            cwd=ROOT,
            check=True,
        )
        wheel = next(wheel_dir.glob("conda_visualizer-*-py3-none-any.whl"))
    # Bundling copies local wheels itself; otherwise the page fetches it from base_url.
    requirement = wheel.as_uri() if args.bundle else f"{args.base_url.rstrip('/')}/{wheel.name}"

    # Prerendering runs the app here; make it render the browser UI, not this machine's envs.
    os.environ["CONDA_VISUALIZER_BROWSER"] = "1"
    convert_apps(
        [str(ROOT / "browser" / "index.py")],
        dest_path=str(out),
        runtime=args.runtime,
        requirements=[requirement],
        panel_version=args.panel_version,
        bundle=args.bundle,
        build_index=False,
        build_pwa=not args.standalone,
        title="Conda Visualizer",
    )
    for path in sorted(out.iterdir()):
        print(f"{path.stat().st_size:>10}  {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
