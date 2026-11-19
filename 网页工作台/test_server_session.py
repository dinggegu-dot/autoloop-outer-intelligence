import unittest
from types import SimpleNamespace
from server import Handler

class SessionIsolationTest(unittest.TestCase):
    def test_two_local_workbenches_keep_independent_sessions(self):
        cookies='odyssey_session_8765=production; odyssey_session_8766=fixture; odyssey_session=old'
        def check(port,token):
            request=SimpleNamespace(headers={'Cookie':cookies},server=SimpleNamespace(server_port=port,session_token=token))
            return Handler.session(request)
        self.assertTrue(check(8765,'production'));self.assertTrue(check(8766,'fixture'))
        self.assertFalse(check(8765,'fixture'));self.assertFalse(check(8766,'production'));self.assertFalse(check(8765,'old'))

if __name__=='__main__':unittest.main()
