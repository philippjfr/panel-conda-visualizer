"""Command line entry point: ``conda-visualizer serve|convert|outdated|plan|leaves|...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import panel as pn

from .app import create_app
from .export import export_html
from .graph import DependencyGraph
from .sources import SourceError, default_source_path, open_source
from .updates import (
    apply_plan,
    fetch_availability,
    outdated,
    plan_change,
    with_availability,
)


def _load(args) -> DependencyGraph:
    source = open_source(args.source or default_source_path())
    graph = source.graph(args.platform, args.environment)
    for warning in graph.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return graph


def _targets(pins: list[str] | None) -> dict[str, str]:
    targets = {}
    for pin in pins or []:
        name, sep, version = pin.partition("=")
        # Accept both pkg=1.2 and pkg==1.2.
        version = version.lstrip("=")
        if not (sep and name and version):
            raise SourceError(f"expected NAME=VERSION, got {pin!r}")
        targets[name] = version
    return targets


def _with_updates(graph: DependencyGraph, args) -> DependencyGraph:
    """Attach newest versions and the effect of any requested version changes."""
    pins = _targets(args.set)
    if not (args.latest or pins or args.update_all):
        return graph
    graph = with_availability(graph, fetch_availability(graph))
    if pins or args.update_all:
        graph = apply_plan(graph, plan_change(graph, pins, update_all=args.update_all))
    return graph


def _view(graph: DependencyGraph, args) -> DependencyGraph:
    hide = set(args.hide or [])
    if not args.virtual:
        hide |= set(graph.without_kinds(["virtual"]))
    if args.small:
        hide |= {"python", "conda"}
    focus = [args.focus] if args.focus else []
    if args.focus and args.focus not in graph:
        raise SourceError(f"package {args.focus!r} not found")
    nodes = graph.neighborhood(focus, args.direction, args.depth or None, args.exclude or [], hide)
    return graph.subgraph(nodes)


def cmd_convert(args) -> None:
    graph = _view(_with_updates(_load(args), args), args)
    output = Path(args.output) if args.output else None
    fmt = args.format or (output.suffix.lstrip(".") if output else "dot")
    if fmt == "html":
        export_html(graph, output or "graph.html", title=args.title, inline=args.inline)
        return
    text = {"dot": graph.to_dot, "gv": graph.to_dot, "json": graph.to_json}[fmt]()
    if output:
        output.write_text(text)
    else:
        sys.stdout.write(text)


def cmd_serve(args) -> None:
    pn.serve(
        lambda: create_app(args.source or default_source_path()),
        port=args.port,
        show=not args.no_show,
        title="Conda Visualizer",
    )


def cmd_leaves(args) -> None:
    graph = _load(args)
    leaves = graph.roots() if args.with_cycles else graph.leaves()
    print(json.dumps(leaves) if args.json else "\n".join(leaves))


def cmd_cycles(args) -> None:
    for cycle in _load(args).cycles():
        print(" -> ".join([*cycle, cycle[0]]))


def cmd_outdated(args) -> None:
    graph = _load(args)
    rows = outdated(with_availability(graph, fetch_availability(graph)))
    if args.json:
        print(json.dumps(rows))
        return
    width = max((len(r["name"]) for r in rows), default=0)
    for row in rows:
        print(f"{row['name']:<{width}}  {row['version']}  ->  {row['latest']}")


def cmd_plan(args) -> None:
    graph = _load(args)
    plan = plan_change(graph, _targets(args.pins), update_all=args.update_all)
    if args.json:
        print(json.dumps(plan.rows()))
        return
    for row in plan.rows():
        print(f"{row['change']:<9} {plan.changes[row['name']].describe()}")
    if not plan:
        print("no changes")


def cmd_related(args) -> None:
    graph = _load(args)
    exclude = [e for item in args.exclude or [] for e in item.split(",")]
    if args.small:
        exclude += ["python", "conda"]
    if args.package not in graph:
        print(f'warning: package "{args.package}" not found', file=sys.stderr)
        sys.exit(1)
    if args.tree:
        print(
            graph.tree(
                args.package, reverse=args.command == "whoneeds", exclude=exclude, full=args.full
            )
        )
        return
    query = graph.depends if args.command == "depends" else graph.whoneeds
    found = query(args.package, recursive=args.recursive, exclude=exclude)
    print(json.dumps(found) if args.json else "\n".join(found))


def parser() -> argparse.ArgumentParser:
    source_help = (
        "prefix, environment name, pixi.lock, conda-lock.yml, explicit lockfile, "
        "environment.yml or DOT file (default: active environment)"
    )
    selection = argparse.ArgumentParser(add_help=False)
    selection.add_argument("--platform", help="platform for lockfiles and environment.yml")
    selection.add_argument("--environment", help="pixi.lock environment")
    # serve/convert take the source positionally; the conda-tree style
    # commands take it as -p/-n like conda-tree, leaving positionals for packages.
    common = argparse.ArgumentParser(add_help=False, parents=[selection])
    common.add_argument("source", nargs="?", help=source_help)
    query = argparse.ArgumentParser(add_help=False, parents=[selection])
    query.add_argument("-p", "--prefix", "-n", "--name", dest="source", help=source_help)

    p = argparse.ArgumentParser(prog="conda-visualizer", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", parents=[common], help="open the interactive app")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--no-show", action="store_true", help="don't open a browser")
    serve.set_defaults(func=cmd_serve)

    convert = sub.add_parser(
        "convert", parents=[common], help="write DOT, JSON or standalone interactive HTML"
    )
    convert.add_argument("-o", "--output", help="output file; format follows the suffix")
    convert.add_argument("-f", "--format", choices=["dot", "json", "html"])
    convert.add_argument("--focus", help="only show packages related to this one")
    convert.add_argument("--direction", choices=["both", "depends", "whoneeds"], default="both")
    convert.add_argument("--depth", type=int, default=0, help="0 = unlimited")
    convert.add_argument("--exclude", action="append", help="don't expand past this package")
    convert.add_argument("--hide", action="append", help="drop this package")
    convert.add_argument("--small", action="store_true", help="hide python and conda")
    convert.add_argument("--virtual", action="store_true", help="keep virtual packages")
    convert.add_argument(
        "--latest", action="store_true", help="look up newer versions and mark outdated packages"
    )
    convert.add_argument(
        "--set",
        action="append",
        metavar="NAME=VERSION",
        help="preview switching a package to this version and mark what changes (repeatable)",
    )
    convert.add_argument("--update-all", action="store_true", help="preview updating every package")
    convert.add_argument("--title", help="HTML page title")
    convert.add_argument(
        "--inline", action="store_true", help="embed Bokeh/Panel JS in the HTML file"
    )
    convert.set_defaults(func=cmd_convert)

    outdated_cmd = sub.add_parser(
        "outdated", parents=[query], help="packages with a newer version in their channels"
    )
    outdated_cmd.add_argument("--json", action="store_true")
    outdated_cmd.set_defaults(func=cmd_outdated)

    plan = sub.add_parser(
        "plan", parents=[query], help="packages that change when some are set to other versions"
    )
    plan.add_argument("pins", nargs="*", metavar="NAME=VERSION")
    plan.add_argument("--update-all", action="store_true", help="update every package")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=cmd_plan)

    leaves = sub.add_parser("leaves", parents=[query], help="packages nothing depends on")
    leaves.add_argument("--with-cycles", action="store_true")
    leaves.add_argument("--json", action="store_true")
    leaves.set_defaults(func=cmd_leaves)

    cycles = sub.add_parser("cycles", parents=[query], help="dependency cycles")
    cycles.set_defaults(func=cmd_cycles)

    for name, desc in (
        ("depends", "dependencies of a package"),
        ("whoneeds", "packages that need a package"),
    ):
        cmd = sub.add_parser(name, parents=[query], help=desc)
        cmd.add_argument("package")
        cmd.add_argument("-r", "--recursive", action="store_true")
        cmd.add_argument("-t", "--tree", action="store_true")
        cmd.add_argument("--json", action="store_true")
        cmd.add_argument("--exclude", action="append", help="comma separated, repeatable")
        cmd.add_argument("--small", action="store_true", help="exclude python and conda")
        cmd.add_argument("--full", action="store_true", help="expand repeated subtrees")
        cmd.set_defaults(func=cmd_related)
    return p


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    try:
        args.func(args)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
