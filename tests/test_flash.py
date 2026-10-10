import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from drc.flash_api import FlashBudget, FlashClient, load_key
from drc.api_budget import BudgetLimit
from drc.flash_recovery import make_dataset, run_episode, RULES, FAULTS, public_input


class FlashTests(unittest.TestCase):
    def test_truncation_charged_without_retry(self):
        from io import BytesIO
        with tempfile.TemporaryDirectory() as d:
            budget = FlashBudget("dev", Path(d)/"wallet.db")
            response = {"model":"deepseek-flash", "choices":[{"finish_reason":"length", "message":{"content":"{\"ready\":"}}],
                        "usage":{"prompt_tokens":100,"completion_tokens":64}}
            with patch('drc.flash_api.load_key',return_value='fake-test-key'), patch('urllib.request.urlopen',return_value=BytesIO(json.dumps(response).encode())) as request:
                client=FlashClient(budget,'test',Path(d)/'calls',64,prompt='Return JSON')
                self.assertIsNone(client.ask({},validate_action=False))
                self.assertEqual(request.call_count,1)
                self.assertEqual(client.calls[0]['status'],'error')
                self.assertEqual(client.calls[0]['conservative_charge_micro_usd'],107)
                self.assertNotIn('fake-test-key',next((Path(d)/'calls').glob('*.json')).read_text())

    def test_dependency_admission_and_recovery(self):
        from drc.flash_recovery import VariableService, external_instances
        with tempfile.TemporaryDirectory() as d:
            inst=external_instances()[0]
            s=VariableService(Path(d)/'dep.db',inst['task'],'clean',inst['timing'])
            s.call('query');s.call('write',[0,1,2,3])
            current=s.call('query')['values']
            self.assertEqual(current[:2],inst['task']['expected'][:2])
            self.assertEqual(current[2:],inst['task']['base'][2:])
            s.call('write',[2,3]);s.call('query')
            end=s.finish()
            self.assertEqual(end['final']['values'],inst['task']['expected'])
            self.assertEqual(len(end['ledger']),4)

    def test_historical_budget_counts(self):
        with tempfile.TemporaryDirectory() as d:
            b = FlashBudget("dev", Path(d) / "wallet.db")
            with sqlite3.connect(b.path) as db:
                db.execute("INSERT INTO requests VALUES ('old','old',89999999,89999999,'usage_recorded','old',NULL,NULL)")
            with self.assertRaises(BudgetLimit):
                b.reserve("new", 100, 100)

    def test_stage_budget_and_missing_usage(self):
        with tempfile.TemporaryDirectory() as d:
            b = FlashBudget("dev", Path(d) / "wallet.db")
            rid = b.reserve("one", 100, 10)
            self.assertEqual(b.settle(rid), 42)
            with sqlite3.connect(b.path) as db:
                db.execute("UPDATE requests SET charged=2999999")
            with self.assertRaises(BudgetLimit):
                b.reserve("two", 100, 10)

    def test_key_parser_not_shell(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "env"
            p.write_text('deepseek-api_key="fake-$literal"\n')
            self.assertEqual(load_key(p), "fake-$literal")
            p.write_text('deepseek-api_key=a\nDEEPSEEK_API_KEY=b\n')
            with self.assertRaises(ValueError):
                load_key(p)

    def test_rules_and_public_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            for inst in make_dataset()["instances"]:
                for fault in FAULTS:
                    for method in RULES:
                        row = run_episode(Path(d) / f'{inst["instance_id"]}-{fault}-{method}.db', inst, fault, method)
                        self.assertTrue(row["metrics"]["scts"], (inst, fault, method))
                        events = row["visible_events"][:2]
                        raw = public_input(inst["task"], events, inst["timing"]["bound"], "raw", 8)
                        full = public_input(inst["task"], events, inst["timing"]["bound"], "full", 8)
                        self.assertEqual(full["visible_events"], raw["visible_events"])
                        self.assertNotIn("evaluator_only", full)
                        self.assertNotIn("fault", full)


if __name__ == "__main__":
    unittest.main()
