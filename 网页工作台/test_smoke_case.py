import json
import tempfile
import unittest
from pathlib import Path

from smoke_case import CASES, check_report, prepare_cases
from workbench import Store


class SmokeCaseTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()

    def test_pair_preparation_does_not_dispatch_or_overwrite(self):
        ids=prepare_cases(self.root,CASES)
        store=Store(self.root)
        self.assertEqual(len(store.list()),2)
        for task in store.list():
            self.assertEqual((task['status'],task['calls'],task['time_budget_seconds']),('created',0,900))
            self.assertEqual(set(task['role_models'].values()),{'gpt-6-astra'})
        self.assertTrue((store.workspace(ids['资料报告'])/'input.csv').is_file())
        self.assertTrue((store.workspace(ids['代码修复'])/'test_stats_tool.py').is_file())
        with self.assertRaises(ValueError):prepare_cases(self.root,CASES)
        with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_report_numbers_belong_to_their_rows(self):
        (self.root/'totals.json').write_text(json.dumps({'软件':7,'能源':5}))
        report=self.root/'report.md'
        header='| 行业 | 条目数 |\n| :--- | ---: |\n'
        sources='\n来源：示例甲、示例乙、示例丙。'
        report.write_text(header+'| 能源 | 5 |\n| 软件 | 7 |\n'+sources)
        self.assertEqual(check_report(self.root),{'软件':7,'能源':5})
        for bad in ('软件 5，能源 7。',header+'| 软件 | 5 |\n| 能源 | 7 |',
                    header+'| 软件 | 7 |\n| 软件 | 5 |\n| 能源 | 5 |'):
            report.write_text(bad+sources)
            with self.assertRaises(ValueError):check_report(self.root)
