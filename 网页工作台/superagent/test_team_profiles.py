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

    def test_alternative_coding_agent_uses_same_readonly_role_contract(self):
        future = dict(profiles.new_team_profile(), version=3, role_runtimes={'advisor':'fixture-agent'})
        adapter = Mock(return_value={'session':'fixture', 'model':future['roles']['advisor']})
        with patch.dict(profiles.PROFILES, {3:future}), patch.dict(profiles.ROLE_ADAPTERS, {'fixture-agent':adapter}):
            result = profiles.invoke_role(future, 'advisor', effort='high', sandbox='read-only')
            self.assertEqual(result['model'], 'gpt-6.2-astra')
            adapter.assert_called_once_with(model='gpt-6.2-astra', effort='high', sandbox='read-only')
            with self.assertRaises(ValueError):
                profiles.invoke_role(future, 'advisor', effort='high', sandbox='workspace-write')
        with self.assertRaises(ValueError):
            profiles.invoke_role(future, 'advisor', effort='high', sandbox='read-only')
