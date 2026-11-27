from unittest import TestCase
from unittest.mock import Mock, patch

from superagent import team_profiles as profiles


class TeamProfileTest(TestCase):
    def test_role_identity_and_version_are_frozen(self):
        frozen = profiles.new_team_profile()
        self.assertTrue(profiles.supported(frozen))
        self.assertEqual(frozen['roles']['lead'], 'gpt-6.2-sol')
        self.assertEqual(frozen['roles']['implementer'], 'gpt-6.2-sol')
        self.assertEqual(frozen['roles']['advisor'], 'gpt-6.2-astra')
        self.assertEqual(profiles.LEGACY_TEAM['roles']['advisor'], 'gpt-6.2-sol')
        self.assertEqual(profiles.role_effort(frozen, 'advisor', 'medium'), 'high')
        self.assertEqual(profiles.role_effort(frozen, 'lead', 'medium', 'compare'), 'high')
        frozen['roles']['lead'] = 'unregistered'
        self.assertFalse(profiles.supported(frozen))
        self.assertEqual(profiles.new_team_profile()['roles']['lead'], 'gpt-6.2-sol')

    def test_ticket_choices_keep_model_effort_and_history(self):
        options = {
            'lead': {'runtime':'codex-app-server', 'model':'gpt-6.2-astra', 'effort':'medium'},
            'implementer': {'runtime':'codex-app-server', 'model':'gpt-6.2-sol', 'effort':'high'},
        }
        frozen = profiles.new_team_profile(options)
        self.assertEqual(frozen['roles']['lead'], 'gpt-6.2-astra')
        self.assertEqual(profiles.role_effort(frozen, 'lead', 'high', 'compare'), 'medium')
        self.assertEqual(profiles.role_effort(frozen, 'implementer', 'medium'), 'high')
        self.assertEqual(profiles.role_effort(frozen, 'advisor', 'medium'), 'high')
        with patch.object(profiles, 'CODING_AGENTS', {}):
            self.assertTrue(profiles.supported(frozen))
            with self.assertRaises(ValueError):
                profiles.new_team_profile(options)
        frozen['roles']['lead'] = 'gpt-6.2-sol'
        self.assertFalse(profiles.supported(frozen))

    def test_only_registered_codex_choices_fit_current_consent(self):
        choice = {'runtime':'codex-app-server', 'model':'gpt-6.2-astra', 'effort':''}
        default_effort = profiles.new_team_profile({'lead':choice})
        self.assertEqual(profiles.role_effort(default_effort,'lead','medium','compare'),'high')
        for changes in ({'runtime':'deepseek-dsh'}, {'runtime':'/usr/local/bin/codex'},
                        {'model':'unknown'}, {'effort':'max'}, {'executable':'/usr/bin/sh'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                profiles.new_team_profile({'lead':dict(choice, **changes)})
        with patch('superagent.goal_antigravity.executable', return_value=None):
            self.assertEqual(set(profiles.team_options()['agents']), {'codex-app-server'})

    def test_antigravity_choices_freeze_separately_from_existing_codex_profiles(self):
        choice = {'runtime':profiles.ANTIGRAVITY, 'model':'gemini-4-hydrogen', 'effort':'high'}
        with patch('superagent.goal_antigravity.executable', return_value='/trusted/agy'):
            frozen = profiles.new_team_profile({'advisor':choice, 'implementer':choice})
        self.assertEqual(frozen['version'], profiles.MIXED_VERSION)
        self.assertTrue(profiles.supported(frozen))
        self.assertTrue(profiles.uses_antigravity(frozen))
        self.assertEqual(profiles.role_runtime(frozen, 'lead'), 'codex-app-server')
        self.assertFalse(profiles.supported(dict(frozen, version=profiles.SELECTED_VERSION)))
        with patch('superagent.goal_antigravity.executable', return_value=None):
            self.assertTrue(profiles.supported(frozen))
            with self.assertRaises(ValueError):
                profiles.new_team_profile({'implementer':choice})

    def test_alternative_coding_agent_uses_same_readonly_role_contract(self):
        future = dict(profiles.new_team_profile(), version=4, role_runtimes={'advisor':'fixture-agent'})
        adapter = Mock(return_value={'session':'fixture', 'model':future['roles']['advisor']})
        with patch.dict(profiles.PROFILES, {4:future}), patch.dict(profiles.ROLE_ADAPTERS, {'fixture-agent':adapter}):
            result = profiles.invoke_role(future, 'advisor', effort='high', sandbox='read-only')
            self.assertEqual(result['model'], 'gpt-6.2-astra')
            adapter.assert_called_once_with(model='gpt-6.2-astra', effort='high', sandbox='read-only')
            with self.assertRaises(ValueError):
                profiles.invoke_role(future, 'advisor', effort='high', sandbox='workspace-write')
        with self.assertRaises(ValueError):
            profiles.invoke_role(future, 'advisor', effort='high', sandbox='read-only')
