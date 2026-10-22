import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import yaml
from pydantic import ValidationError

from harness.events import EventLog
from harness.llm import BudgetExceeded, LLMClient
from harness.agents.common import strict_json
from harness.schemas import ClaimCheck, ImprovedPrompt, LengthBand, Verdict


ROOT = Path(__file__).resolve().parents[1]


def improved_prompt(criteria):
    return {
        "costar": {"context": "ctx", "objective": "obj", "style": "plain", "tone": "neutral", "audience": "reader", "response": "answer"},
        "length_band": {"min": 1, "max": 20},
        "success_criteria": criteria,
        "assumptions": [],
        "changes_summary": "clarified",
    }


class T1Tests(unittest.TestCase):
    def test_schema_validators(self):
        three = ["Include a summary", "Exclude speculation", "Return three bullets"]
        for criteria in (three[:2], three + ["Cite sources"] * 4):
            with self.assertRaises(ValidationError):
                ImprovedPrompt.model_validate(improved_prompt(criteria))
        ImprovedPrompt.model_validate(improved_prompt(three))
        ImprovedPrompt.model_validate(improved_prompt(three + ["Cite sources"] * 3))
        with self.assertRaises(ValidationError):
            LengthBand(min=5, max=5)
        with self.assertRaises(ValidationError):
            Verdict(decision="REVISE", scores={}, total=0.5, issues=[], claims=[], length={"status": "OK"})
        with self.assertRaises(ValidationError):
            Verdict(decision="PROMPT_PROBLEM", scores={}, total=0.5, issues=[{"metric": "x", "severity": "minor", "quote": "q", "fix": "f"}], claims=[], length={"status": "OK"})

    def test_config_defaults(self):
        config = yaml.safe_load((ROOT / "harness" / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["models"]["pe"], {"provider": "openrouter", "name": "cheap-model"})
        self.assertEqual(config["thresholds"], {"pass_total": 0.85, "factual_floor": 0.8, "stall_delta": 0.02, "fuzzy_match": 0.8})
        self.assertEqual(config["budget"]["per_agent"], {"pe": 8000, "ex": 25000, "vr": 20000, "mc": 4000})

    def test_usage_accounting_and_abort(self):
        config = yaml.safe_load((ROOT / "harness" / "config.yaml").read_text(encoding="utf-8"))
        client = LLMClient(config)
        client.account("pe", 6400)
        self.assertEqual(client.usage.agents["pe"], 6400)
        self.assertEqual(client.usage.total, 6400)
        self.assertTrue(client.warnings)
        with self.assertRaises(BudgetExceeded):
            client.account("pe", 1600)
        self.assertEqual(client.usage.agents["pe"], 8000)
        config["budget"]["total_tokens"] = 10
        config["budget"]["per_agent"]["ex"] = 100
        total_client = LLMClient(config)
        total_client.account("ex", 8)
        with self.assertRaises(BudgetExceeded):
            total_client.account("ex", 2)

    def test_strict_json_has_exactly_one_repair_retry(self):
        class ResponseClient:
            def __init__(self, responses):
                self.responses = iter(responses)
                self.calls = 0

            def complete(self, agent, messages):
                self.calls += 1
                return next(self.responses), 4, 0.01

        client = ResponseClient(["not json", '{"claim":"c","status":"supported","evidence":"e"}'])
        result, tokens, latency = strict_json(client, "vr", ClaimCheck, [])
        self.assertEqual(result.claim, "c")
        self.assertEqual(client.calls, 2)
        self.assertEqual(tokens, 8)
        self.assertAlmostEqual(latency, 0.02)
        invalid = ResponseClient(["bad", "still bad"])
        with self.assertRaisesRegex(RuntimeError, "ESCALATED"):
            strict_json(invalid, "vr", ClaimCheck, [])
        self.assertEqual(invalid.calls, 2)

    def test_event_jsonl_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            EventLog(path, "run-1").write("EXECUTE", "ex", 1, {"ex": 12}, 0.25)
            event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertTrue({"run_id", "state", "agent", "prompt_version", "token_counts", "latency"} <= event.keys())
            self.assertEqual(event["token_counts"], {"ex": 12})

    def test_stub_cli_run_resume_report(self):
        run_id = f"t1-smoke-{uuid.uuid4().hex}"
        run_dir = ROOT / "runs" / run_id
        try:
            command = [sys.executable, "-m", "harness.cli", "--root", str(ROOT), "run", "--request", "Say hello", "--run-id", run_id]
            run = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertTrue((run_dir / "events.jsonl").exists())
            self.assertTrue((run_dir / "drafts" / "round-1.json").exists())
            self.assertTrue((run_dir / "verdicts" / "round-1.json").exists())
            self.assertEqual(json.loads((run_dir / "report.json").read_text())["status"], "DONE")
            report = subprocess.run([sys.executable, "-m", "harness.cli", "--root", str(ROOT), "report", run_id], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
            resumed = subprocess.run([sys.executable, "-m", "harness.cli", "--root", str(ROOT), "resume", run_id], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        finally:
            import shutil
            shutil.rmtree(run_dir)


if __name__ == "__main__":
    unittest.main()
