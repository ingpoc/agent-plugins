from uuid import uuid4
from test_v2 import V2Base


class PreferenceTests(V2Base):
    def test_save_receipt_reports_committed_replacement_on_retry(self):
        args = {'request_id': str(uuid4()), 'type': 'user_decision', 'scope': 'project:fixture',
                'rule': 'badge_color', 'decision': 'blue', 'why': 'Owner preference', 'entities': ['fixture']}
        first_receipt = self.store.save(args, mcp=True)
        self.assertNotIn('superseded_ids', first_receipt)
        first = first_receipt['decision_id']
        args = {**args, 'request_id': str(uuid4()), 'decision': 'green'}
        saved = self.store.save(args, mcp=True)
        self.assertEqual(saved['superseded_ids'], [first])
        self.assertEqual(self.store.save(args, mcp=True), saved)
        self.assertEqual(self.store.get({'decision_id': first}, mcp=True)['record']['replacement_id'], saved['decision_id'])

    def test_exact_recall_and_supersession(self):
        cases = [('tool:netflix.com', 'profile_select', 'Guru'),
                 ('tool:gmail', 'account_select', 'Work'),
                 ('tool:macos', 'default_browser', 'Safari'),
                 ('project:agent-plugins', 'merge_strategy', 'Rebase')]
        ids = {}
        for scope, rule, value in cases:
            ids[scope] = self.save(type='user_decision', scope=scope, rule=rule,
                                   decision=value, why='Owner preference', entities=[scope.split(':')[1], rule])
        self.store.ab_today = lambda: '2026-10-18'
        for scope, rule, value in cases:
            found = self.find('unrelated wording', scope=scope, rule=rule)
            self.assertEqual([r['decision'] for r in found['matches']], [value])
        scope, rule, _ = cases[0]
        replacement = self.save(type='user_decision', scope=scope, rule=rule,
                                decision='Family', why='Owner preference', entities=['netflix.com', rule])
        self.save(type='user_decision', scope=scope, rule='language_select',
                  decision='English', why='Owner preference', entities=['netflix.com', 'language_select'])
        self.assertEqual([r['decision_id'] for r in self.find('', scope=scope, rule=rule)['matches']], [replacement])
        self.assertEqual(len(self.find('', scope=scope)['matches']), 2)
        self.assertNotEqual(ids[scope], replacement)
