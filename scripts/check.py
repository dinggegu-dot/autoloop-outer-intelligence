"""本机和 CI 共用的无账户回归入口。"""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / '网页工作台'
MOBILE = ROOT / '飞书工作入口'

def run(command, cwd, **variables):
    subprocess.run(command,cwd=cwd,check=True,env={**os.environ,**variables})

def main():
    run([sys.executable,'-B','scripts/check_repository.py'],ROOT)
    run(['git','diff','--check','origin/main...HEAD'], ROOT)
    for file in sorted(BOARD.glob('*.js')):
        run(['node','--check',str(file)],BOARD)
    run(['node','--test','test_journey.mjs','test_presentation.mjs'],BOARD)
    run([sys.executable,'-B','-m','unittest','discover','-q'],BOARD)
    modules=['superagent.'+p.stem for p in sorted((BOARD/'superagent').glob('test_*.py'))]
    run([sys.executable,'-B','-m','unittest','-q',*modules],BOARD,
        PYTHONPATH=os.pathsep.join([str(BOARD),str(BOARD/'superagent')]))
    run([sys.executable,'-B','-m','unittest','discover','-q'],MOBILE)

if __name__ == '__main__':
    main()
