import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from harness.agents.mc import MemoryCurator
from harness.compress import HeadroomProxy, compress
from harness.memory import Mem0Store


class FakeMem0:
    def __init__(self, items=None, reject_filters=False):
        self.items = list(items or [])
        self.reject_filters = reject_filters
        self.search_calls = []
        self.add_calls = []

    def search(self, query, *, user_id, limit, filters=None):
        self.search_calls.append((user_id, limit, filters))
        if filters and self.reject_filters:
            raise ValueError("unsupported filter syntax")
        found = [item for item in self.items if item.get("user_id") == user_id]
        if filters:
            allowed = set(filters["AND"][1]["metadata.type"]["in"])
            found = [item for item in found if item.get("metadata", {}).get("type") in allowed]
        return {"results": found[:limit]}

    def add(self, text, *, user_id, agent_id, metadata):
        self.add_calls.append((text, user_id, agent_id, metadata))
        self.items.append({"memory": text, "user_id": user_id, "agent_id": agent_id, "metadata": metadata})


class T6MemoryTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"memory": "Prefers concise answers", "user_id": "u1", "metadata": {"type": "preference"}},
            {"memory": "Earlier failure", "user_id": "u1", "metadata": {"type": "failure"}},
            {"memory": "Other user's preference", "user_id": "u2", "metadata": {"type": "preference"}},
        ]

    def test_reads_filter_by_agent_types_and_user(self):
        client = FakeMem0(self.items)
        store = Mem0Store({"mem0": {"user_id": "u1", "top_k": 5}}, client=client)
        result = store.search("answer", agent="pe")
        self.assertEqual([x["metadata"]["type"] for x in result], ["preference"])
        user, limit, filters = client.search_calls[0]
        self.assertEqual(user, "u1")
        self.assertEqual(limit, 5)
        self.assertEqual(filters["AND"][0], {"user_id": "u1"})
        self.assertEqual(store.search("quality", agent="vr")[0]["metadata"]["type"], "failure")

    def test_unsupported_filters_fallback_fetches_15_and_filters_locally(self):
        client = FakeMem0(self.items, reject_filters=True)
        store = Mem0Store({"mem0": {"user_id": "u1"}}, client=client)
        result = store.search("answer", agent="pe")
        self.assertEqual([x["metadata"]["type"] for x in result], ["preference"])
        self.assertEqual(client.search_calls[1], ("u1", 15, None))

    def test_writes_are_gate_scoped_deduplicated_safe_and_capped(self):
        client = FakeMem0()
        store = Mem0Store({"mem0": {"user_id": "u1", "max_writes_per_run": 99}}, client=client)
        item = {"text": "The user prefers short answers", "type": "preference", "confidence": 0.9}
        self.assertFalse(store.write(item, run_id="r", gate2="accept", accepted=True, agent_id="ex"))
        self.assertFalse(store.write(item, run_id="r", gate2="pending", accepted=True))
        self.assertFalse(store.write({**item, "text": "API_KEY=secret"}, run_id="r", gate2="accept", accepted=True))
        self.assertTrue(store.write(item, run_id="r", gate2="accept", accepted=True))
        self.assertFalse(store.write(item, run_id="r", gate2="accept", accepted=True))
        self.assertEqual(client.add_calls[0][1:], ("u1", "mc", {"type": "preference", "run_id": "r", "agent_id": "mc"}))
        for i in range(10):
            self.assertEqual(store.write({"text": f"Preference number {i}", "type": "preference"},
                                         run_id="r2", gate2="accept", accepted=True), i < 4)
        self.assertEqual(store.write_count, 5)
        self.assertEqual(store.write_count, 5)
        reject_store = Mem0Store({"mem0": {"user_id": "u1"}}, client=FakeMem0())
        self.assertTrue(reject_store.write({"text": "Answer was too verbose", "type": "failure"},
                                           run_id="r3", gate2="reject"))
        self.assertFalse(reject_store.write({"text": "User did not accept this preference", "type": "preference"},
                                            run_id="r3", gate2="reject"))

    def test_domain_facts_require_verified_acceptance(self):
        store = Mem0Store({"mem0": {"user_id": "u1"}}, client=FakeMem0())
        fact = {"text": "Verified fact", "type": "domain_fact"}
        self.assertFalse(store.write(fact, run_id="r", gate2="accept", accepted=True))
        self.assertTrue(store.write(fact, run_id="r", gate2="accept", accepted=True, verified=True))

    def test_mc_emits_only_gate_eligible_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "mc.md"
            prompt.write_text("version: 1\n", encoding="utf-8")
            curator = MemoryCurator(prompt)
            self.assertEqual(curator.run_stub("request"), [])
            rejected = curator.run_stub("request", gate2="reject", rejection_reason="Too long")
            self.assertEqual([(x.text, x.type) for x in rejected], [("Too long", "failure")])
            accepted = curator.run_stub("request", gate2="accept", accepted_preferences=["Concise"],
                                        passed_patterns=["Use examples"], verified_facts=["Fact"])
            self.assertEqual([x.type for x in accepted], ["preference", "prompt_pattern", "domain_fact"])


class T6HeadroomTests(unittest.TestCase):
    def test_health_failure_fails_open_to_direct_provider(self):
        proxy = HeadroomProxy({"headroom": {"base_url": "http://proxy:8787", "mode": "proxy"}},
                              opener=Mock(side_effect=OSError("offline")))
        self.assertFalse(proxy.check_health())
        self.assertFalse(proxy.enabled)
        self.assertIsNone(proxy.provider_base_url)
        self.assertIn("using direct providers", proxy.warning)

    def test_health_and_savings_are_captured(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        runner = Mock(return_value=Mock(returncode=0, stdout='{"tokens_saved": 1234}'))
        proxy = HeadroomProxy({"headroom": {"base_url": "http://proxy:8787", "output_shaper": False}},
                              opener=Mock(return_value=response), command_runner=runner)
        self.assertTrue(proxy.check_health())
        self.assertEqual(proxy.provider_base_url, "http://proxy:8787")
        self.assertEqual(proxy.capture_savings(), 1234)
        self.assertEqual(proxy.capture_savings(12), 12)
        self.assertFalse(proxy.output_shaper)

    def test_savings_failure_is_nonfatal_and_protected_text_stays_verbatim(self):
        proxy = HeadroomProxy(command_runner=Mock(side_effect=FileNotFoundError("headroom missing")))
        self.assertEqual(proxy.capture_savings(), 0)
        prompt = "approved prompt\n\ncurrent draft"
        self.assertEqual(compress(prompt, never_compress=["approved_costar_prompt", "current_draft"]), prompt)
        self.assertEqual(proxy.compress(prompt, protected=True), prompt)


if __name__ == "__main__":
    unittest.main()
