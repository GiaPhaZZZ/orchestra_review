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


def gate1_cli(payload: dict) -> dict:
    print("\nGate 1: review the proposed prompt")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    decision = input("Choose approve, edit, or reject: ").strip().lower()
    if decision == "edit":
        edited = input("Enter the complete edited ImprovedPrompt as JSON: ")
        return {"decision": "edit", "prompt": json.loads(edited)}
    if decision == "reject":
        return {"decision": "reject", "reason": input("Reason for rejection: ").strip()}
    return decision


def gate2_cli(payload: dict) -> str:
    print("\nGate 2: review the verified draft")
    print(payload["draft"])
    print("Scores:", json.dumps(payload["verdict"].get("scores", {}), ensure_ascii=False))
    decision = input("Choose accept, reject, or press Enter for no answer: ").strip().lower()
    if decision == "reject":
        return "reject:" + input("Reason for rejection: ").strip()
    return decision


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
        state = controller.run(args.request, gate1=gate1_cli, gate2=gate2_cli)
    else:
        state = controller.resume(gate1=gate1_cli, gate2=gate2_cli)
    print(json.dumps({"run_id": run_id, "status": state["state"], "run_dir": str(controller.run_dir)}, indent=2))
    return 0 if state["state"] == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
