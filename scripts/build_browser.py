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
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from panel.io.convert import convert_apps

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Build the in-browser (Pyodide) app.")
    parser.add_argument("--base-url", required=True, help="URL the output directory is served at")
    parser.add_argument("--out", default="build/browser", help="output directory")
    parser.add_argument("--wheel", help="use this wheel instead of building one")
    args = parser.parse_args(argv)

    out = (ROOT / args.out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    if args.wheel:
        wheel = Path(shutil.copy(args.wheel, out))
    else:
        # The hatch build hook compiles the JS bundle into the wheel.
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out)], cwd=ROOT, check=True
        )
        wheel = next(out.glob("conda_visualizer-*-py3-none-any.whl"))

    # Prerendering runs the app here; make it render the browser UI, not this machine's envs.
    os.environ["CONDA_VISUALIZER_BROWSER"] = "1"
    convert_apps(
        [str(ROOT / "browser" / "index.py")],
        dest_path=str(out),
        runtime="pyodide-worker",
        requirements=[f"{args.base_url.rstrip('/')}/{wheel.name}"],
        build_index=False,
        build_pwa=True,
        title="Conda Visualizer",
    )
    for path in sorted(out.iterdir()):
        print(f"{path.stat().st_size:>10}  {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
