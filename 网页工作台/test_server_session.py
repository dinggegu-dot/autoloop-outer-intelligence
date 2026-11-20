import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from server import Handler, AfrQueryHandler

class SessionIsolationTest(unittest.TestCase):
    def test_query_only_service_never_dispatches_task_or_model_requests(self):
        handler = AfrQueryHandler.__new__(AfrQueryHandler)
        handler.send_error = Mock()
        with patch.object(Handler,'do_GET') as get, patch.object(Handler,'do_POST') as post:
            for path in ('/api/tasks','/api/outer/submit','/api/afr/process','/api/state'):
                handler.path = path
                handler.do_GET()
                handler.do_POST()
            self.assertEqual(handler.send_error.call_count,8)
            handler.send_error.assert_called_with(404)
            get.assert_not_called()
            post.assert_not_called()
            handler.path = '/api/afr/read'
            handler.do_POST()
            post.assert_called_once()

    def test_two_local_workbenches_keep_independent_sessions(self):
        cookies='odyssey_session_8765=production; odyssey_session_8766=fixture; odyssey_session=old'
        def check(port,token):
            request=SimpleNamespace(headers={'Cookie':cookies},server=SimpleNamespace(server_port=port,session_token=token))
            return Handler.session(request)
        self.assertTrue(check(8765,'production'));self.assertTrue(check(8766,'fixture'))
        self.assertFalse(check(8765,'fixture'));self.assertFalse(check(8766,'production'));self.assertFalse(check(8765,'old'))

if __name__=='__main__':unittest.main()
