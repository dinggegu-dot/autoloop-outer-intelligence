"""真实本地子进程验证：测试替身不调用模型、不连接中台。"""
import os
import json
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import task_runner
from task_runner import Runner, ExecutionTimeout
from workbench import Store
from execution_policy import ProgressWindow


class ProcessGuardTest(unittest.TestCase):
    def test_timeout_stops_entire_owned_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();store=Store(root)
            task,_=store.create({'name':'进程清理检查','goal':'本机隔离替身','request_id':str(uuid.uuid4()),'role_models':{'plan':'gpt-6-astra','execute':'gpt-6.1-terra','review':'gpt-6-astra'}})
            marker=root/'group.txt';arguments=root/'arguments.json';fake=root/'fake-codex'
            fake.write_text('#!'+sys.executable+'\nimport os,sys,subprocess,time\nsys.stdin.read()\n'
                +f'open({str(marker)!r},"w").write(str(os.getpgrp()))\n'
                +f'import json; open({str(arguments)!r},"w").write(json.dumps(sys.argv))\n'
                +'subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\ntime.sleep(60)\n')
            fake.chmod(0o700)
            def short_window(started, remaining):
                return ProgressWindow(started,remaining,hard=3,idle=3)
            try:
                with patch('task_runner.CODEX',str(fake)),patch('task_runner.ProgressWindow',side_effect=short_window):
                    with self.assertRaises(ExecutionTimeout):
                        Runner(store,task['id'],1,{}).call('execute')
                self.assertTrue(marker.exists())
                args=json.loads(arguments.read_text());self.assertEqual(args[args.index('-m')+1],'gpt-6.1-terra')
                group=marker.read_text()
                output=subprocess.check_output(['/bin/ps','-ax','-o','pgid=','-o','stat='],text=True)
                self.assertFalse(any(len(row)==2 and row[0]==group and not row[1].startswith('Z') for row in (line.split() for line in output.splitlines())))
                current=store.get(task['id'])
                self.assertIsNone(current['model_group']);self.assertIsNone(current['active_call']);self.assertGreater(current['elapsed_seconds'],.4)
            finally:
                if marker.exists():
                    try:os.killpg(int(marker.read_text()),signal.SIGKILL)
                    except ProcessLookupError:pass

if __name__=='__main__':unittest.main()
