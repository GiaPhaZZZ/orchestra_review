"""Command line entry point for stub runs and persisted run reports."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import yaml

from harness.controller import Controller


ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("configuration root must be a mapping")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description="Prompt → Execute → Verify scaffold")
    parser.add_argument("--config", type=Path, default=ROOT / "harness" / "config.yaml")
    parser.add_argument("--root", type=Path, default=ROOT, help="workspace root for prompts and runs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="start a deterministic stub run")
    run.add_argument("--request", required=True)
    run.add_argument("--run-id", default=None)
    resume = subparsers.add_parser("resume", help="resume a persisted run checkpoint")
    resume.add_argument("run_id")
    report = subparsers.add_parser("report", help="print a persisted JSON report")
    report.add_argument("run_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    runs = root / "runs"
    if args.command == "report":
        report_path = runs / args.run_id / "report.json"
        if not report_path.exists():
            raise SystemExit(f"report not found: {report_path}")
        print(report_path.read_text(encoding="utf-8"), end="")
        return 0

    config = load_config(args.config)
    run_id = args.run_id if args.command == "run" and args.run_id else (str(uuid.uuid4()) if args.command == "run" else args.run_id)
    controller = Controller(config, repo_root=root, run_id=run_id, stub=True)
    if args.command == "run":
        state = controller.run(args.request)
    else:
        state = controller.resume()
    print(json.dumps({"run_id": run_id, "status": state["phase"], "run_dir": str(controller.run_dir)}, indent=2))
    return 0 if state["phase"] == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
