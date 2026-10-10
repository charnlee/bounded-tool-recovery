import json
import tempfile
import unittest
from pathlib import Path
from drc.flash_recovery import make_dataset, VariableService, external_instances
from drc.flash_revision import (RevisionService, run_revision, matched_public,
                                terminal_snapshot, terminal_metrics, next_rule, action)


class Scripted:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []
    def ask(self, public):
        return next(self.actions)


class RevisionTests(unittest.TestCase):
    def test_early_declaration_is_not_settled_success(self):
        inst = next(x for x in make_dataset()['instances'] if x['task']['variant'] == 1)
        with tempfile.TemporaryDirectory() as d:
            r = run_revision(Path(d)/'a.db', inst, 'late', 'raw', model=Scripted([action('COMPLETE')]))
            self.assertTrue(r['metrics']['scts'])
            self.assertFalse(r['terminal_metrics']['declaration_correct_now'])
            self.assertTrue(r['terminal_metrics']['premature_declaration'])

    def test_terminal_slot_cannot_issue_tool_action(self):
        inst = make_dataset()['instances'][0]
        with tempfile.TemporaryDirectory() as d:
            r = run_revision(Path(d)/'a.db', inst, 'clean', 'raw', 'terminal',
                             Scripted([action('QUERY')]*9))
            self.assertEqual(len(r['visible_events']), 10)
            self.assertEqual(r['decision'], 'terminal_action_rejected')

    def test_matched_input_bytes_and_public_only(self):
        inst = make_dataset()['instances'][1]
        with tempfile.TemporaryDirectory() as d:
            s = VariableService(Path(d)/'a.db', inst['task'], 'partial', inst['timing'])
            s.call('query'); s.call('write', [0,1]); s.call('query')
            a, b = [matched_public(inst['task'], s.events, inst['timing']['bound'], m, 8) for m in ('computed','restated')]
            self.assertEqual(len(json.dumps(a, sort_keys=True)), len(json.dumps(b, sort_keys=True)))
            self.assertEqual(a['visible_events'], b['visible_events'])
            self.assertNotIn('evaluator_only', a)
            self.assertNotIn('fault', a)
            s.finish()

    def test_per_item_bound_and_initial_dependency_nonadmission(self):
        inst = external_instances()[0]
        with tempfile.TemporaryDirectory() as d:
            s = RevisionService(Path(d)/'a.db', inst['task'], 'clean', inst['timing'])
            s.call('query'); s.call('write', list(range(4)))
            a = next_rule(inst['task'], s.events, 9, 'item_rule', set())
            self.assertEqual(a['action'], 'QUERY')
            s.call('query')
            a = next_rule(inst['task'], s.events, 9, 'item_rule', set())
            self.assertEqual(a['indices'], [2,3])
            s.finish()

    def test_item_rule_does_not_retry_before_bound(self):
        inst = next(x for x in make_dataset()['instances'] if x['task']['variant'] == 1)
        with tempfile.TemporaryDirectory() as d:
            s = RevisionService(Path(d)/'a.db', inst['task'], 'late', inst['timing'])
            s.call('query'); s.call('write', list(range(len(inst['task']['base'])))); s.call('query')
            a = next_rule(inst['task'], s.events, inst['timing']['bound'], 'item_rule', set())
            self.assertEqual(a['action'], 'WAIT')
            s.finish()


if __name__ == '__main__':
    unittest.main()
