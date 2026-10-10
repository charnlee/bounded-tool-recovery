import tempfile
import unittest
from pathlib import Path

from drc.sqlite_contracts import Service, evaluate, make_task, run_episode


class ContractTests(unittest.TestCase):
    def episode(self, family="message", fault="late", contract="stale", method="bounded_verify"):
        with tempfile.TemporaryDirectory() as folder:
            return run_episode(Path(folder) / "state.db", family, 0, fault, contract, method)

    def test_ack_is_not_completion(self):
        self.assertFalse(self.episode(fault="false_ack", method="ack_only")["metrics"]["scts"])
        self.assertTrue(self.episode(fault="false_ack")["metrics"]["scts"])

    def test_durable_delayed_effects_settle_after_abstention(self):
        r = self.episode(contract="unknown")
        self.assertEqual(r["decision"], "abstain")
        self.assertTrue(r["metrics"]["goal"])
        self.assertFalse(r["metrics"]["scts"])
        self.assertTrue(r["evaluator_only"]["durable_reopen_equal"])

    def test_state_only_metric_misses_overwrite_history(self):
        r = self.episode(family="settings", fault="after", method="retry_all")
        self.assertTrue(r["metrics"]["goal_but_unsafe"])
        self.assertEqual(r["metrics"]["duplicate_effects"], 2)

    def test_recovery_fault_is_reverified(self):
        r = self.episode(family="transfer", fault="recovery_partial")
        self.assertTrue(r["metrics"]["scts"])
        self.assertEqual(sum(e["action"] == "write" for e in r["visible_events"]), 2)

    def test_violated_bound_is_not_a_guarantee(self):
        r = self.episode(fault="late", contract="violated")
        self.assertGreater(r["metrics"]["duplicate_effects"], 0)

    def test_grader_ignores_cached_metrics_and_response_labels(self):
        r = self.episode()
        expected = evaluate(r)
        r["metrics"] = {"scts": False}
        for e in r["visible_events"]:
            e["result"] = {"status": "untrusted"}
        self.assertEqual(evaluate(r), expected)

    def test_precommit_observations_cannot_disambiguate_late(self):
        observations = []
        with tempfile.TemporaryDirectory() as folder:
            for fault in ("before", "late"):
                s = Service(Path(folder) / f"{fault}.db", make_task("message", 0), fault,
                            {"lag": 0, "settle": 3})
                s.call("query")
                s.call("write", [0, 1])
                s.call("query")
                observations.append(s.events.copy())
                s.finish()
        self.assertEqual(*observations)


if __name__ == "__main__":
    unittest.main()
