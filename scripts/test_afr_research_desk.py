import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from live_afr_research import Trial, FILES, Problem


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name in ('requests','dispatch','synthetic-vault','preview'):
            (self.root/name).mkdir()
        self.ticket='wb-'+'a'*32
        self.desk=Trial(self.root,self.ticket,'http://127.0.0.1:12345','test')
        self.desk.preview_vault=self.root/'preview'
        self.rid='1'*32
        self.body={'request_id':self.rid,'query':'比较试点资料','parent':None,'consent':True}

    def test_repeated_post_reuses_event_and_claimed_turn_blocks_overlapping_request(self):
        def hub(url,path,key,body=None):
            if body is None: return {'runs':[{'run':1,'state':'claimed'}]}
            self.assertEqual(set(body),{'id','type','subject'})
            return {'event':3,'routed':['afr-research']}
        with patch('live_afr_research.http',side_effect=hub) as mocked:
            self.assertEqual(self.desk.publish(self.body)['run'],1)
            self.assertEqual(self.desk.publish(self.body)['run'],1)
            with self.assertRaises(Problem):self.desk.publish(dict(self.body,request_id='2'*32))
            with self.assertRaises(Problem):self.desk.publish(dict(self.body,query='不同任务'))
            self.assertEqual(sum(len(call.args)>3 for call in mocked.call_args_list),1)

    def test_native_export_replays_identical_files_and_preserves_reader_edits(self):
        (self.root/'requests'/(self.rid+'.json')).write_text(json.dumps(dict(self.body,ticket=self.ticket)))
        archive=self.desk.out/self.rid
        archive.mkdir(parents=True)
        for name in FILES:(archive/name).write_text('{}')
        with patch.object(self.desk,'state',return_value={'state':'done','status':'ok'}):
            result=self.desk.export_canvas(self.rid)
            self.assertEqual(result,self.desk.export_canvas(self.rid))
            target=self.desk.preview_vault/'研究交付'/self.ticket/self.rid
            self.assertFalse((target/'研究记录.json').exists())
            (target/FILES[0]).write_text('我的改动')
            with self.assertRaises(Problem):self.desk.export_canvas(self.rid)
            self.assertEqual((target/FILES[0]).read_text(),'我的改动')

    def test_user_question_requires_consent_and_valid_parent(self):
        with self.assertRaises(Problem):self.desk.publish(dict(self.body,consent=False))
        with self.assertRaises(Problem):self.desk.publish(dict(self.body,parent='2'*32))
        self.assertEqual(list((self.root/'requests').iterdir()),[])

if __name__=='__main__':unittest.main()
