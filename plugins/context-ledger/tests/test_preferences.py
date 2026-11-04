from test_v2 import V2Base


class PreferenceTests(V2Base):
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
