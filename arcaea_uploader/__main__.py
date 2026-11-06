"""Desktop entry point with an optional read-only CLI."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .planner import PlanError, build_plan, resolve_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Arcaea-Uploader desktop app and read-only planner")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    gui_parser = commands.add_parser("gui", help="Open the desktop window")
    gui_parser.add_argument("--config", type=Path)
    plan_parser = commands.add_parser("plan", help="Inspect all packages and print a JSON plan")
    plan_parser.add_argument("--config", required=True, type=Path)
    preprocess_parser = commands.add_parser("preprocess", help="Normalize collected chart packages into work copies")
    preprocess_parser.add_argument("--config", required=True, type=Path)
    preprocess_parser.add_argument("--scan-only", action="store_true", help="Report without copying or converting")
    preprocess_parser.add_argument("--resume", type=Path, help="Continue a previous batch inside the configured release_root")
    args = parser.parse_args(argv)
    if args.smoke_test:
        from .smoke import run_smoke_test
        return run_smoke_test(args.smoke_test)
    if args.command in {None, "gui"}:
        from .gui import launch
        return launch(getattr(args, "config", None))
    try:
        if args.command == "preprocess":
            from .preprocess import preprocess_settings
            from .settings import load_settings
            plan = preprocess_settings(load_settings(args.config), args.config.resolve().parent,
                                       scan_only=args.scan_only, resume_from=args.resume)
        else:
            plan = build_plan(resolve_config(args.config))
    except (ValueError, OSError) as exc:
        print(f"Configuration/plan error: {exc}", file=sys.stderr)
        return 2
    # ensure_ascii also works in legacy Windows consoles; redirected reports stay valid JSON.
    print(json.dumps(plan, ensure_ascii=True, indent=2, allow_nan=False))
    return 0 if plan["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
