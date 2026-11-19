"""DSH 本机自助接入；不安装 Autoloop、不读取/复制/打印凭据。"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from engine_adapters import (CONFIG_NAME, ConnectionProblem, connection_status, load_connection,
                             signature, validate_connection)

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME = ROOT / '.runtime'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.dsh-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.chmod(temporary,0o600)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def register(root, config, replace=False):
    config=validate_connection(config)
    path=root/CONFIG_NAME
    if path.is_symlink():raise ConnectionProblem('连接文件不得为符号链接')
    if path.exists():
        old=load_connection(root)
        if old==config:return False
        if not replace:raise ConnectionProblem('已有不同连接；核对现役任务后显式 --replace，旧版本任务不会静默改路由')
    # version 不启动任务；headless --help 会初始化配置，不能当只读探针。
    result=subprocess.run([config['program'],'--version'],capture_output=True,text=True,timeout=20)
    import re
    version=re.search(r'(?<!\d)(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?',result.stdout)
    if result.returncode or not version or tuple(map(int,version.groups()[:3]))<(0,2,2):
        raise ConnectionProblem('需要 DSH 0.2.2 或更新；当前版本未通过检查，请勿由脚本自动安装或升级')
    atomic_json(path,config)
    return True


def probe(root):
    """显式命令才运行一次真实 DeepSeek，仅操作隔离探针目录，限时 60 秒。"""
    from workbench import Store
    from task_runner import Runner
    config=load_connection(root)
    runroot=root/'connection-tests'/uuid.uuid4().hex
    atomic_json(runroot/CONFIG_NAME,config)
    store=Store(runroot)
    task,_=store.create({'name':'DSH 接入探针','goal':'只在当前工作区创建 readiness.txt，内容严格为 DSH_READY。用 shell 读回验证后返回 JSON 回执，artifacts 包含 readiness.txt。不得执行其他业务。',
                         'acceptance':'真实文件存在且内容为 DSH_READY','request_id':str(uuid.uuid4())})
    store.change(task['id'],lambda t:t.update(time_budget_seconds=60))
    # 不通过 Autoloop 派发、不审批业务；直接验证一次实施引擎。
    result=Runner(store,task['id'],1,{},allow_unverified_dsh=True).call('execute')
    target=store.workspace(task['id'])/'readiness.txt'
    if result.get('status')!='completed' or target.is_symlink() or not target.is_file() or target.read_text().strip()!='DSH_READY' or 'readiness.txt' not in result['artifacts']:
        raise ConnectionProblem('探针未生成并登记正确文件，不标记接通')
    if load_connection(root)!=config:raise ConnectionProblem('探针期间连接发生变化，请重试验证当前连接')
    proof={'passed':True,'config_hash':signature(config),'evidence':str(runroot),'task':task['id'],
           'scope':'仅一次真实 DSH 实施探针，未验证 Astra 规划审核和完整中台任务',
           'usage':store.get(task['id']).get('usage_by_role',{})}
    atomic_json(root/'dsh-verification.json',proof)
    return proof


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',type=Path,default=DEFAULT_RUNTIME)
    commands=parser.add_subparsers(dest='action',required=True)
    commands.add_parser('status',help='只查看脱敏接入状态')
    reg=commands.add_parser('register',help='登记现有本机 DSH；不启动模型')
    reg.add_argument('--program',default=shutil.which('dsh'))
    reg.add_argument('--home',required=True,help='当前已登录/配置过的 DSH_HOME；不是凭据文件')
    reg.add_argument('--provider',required=True,choices=['deepseek-account','deepseek-official'])
    reg.add_argument('--model',required=True,help='在 DSH 核实的实际模型 ID')
    reg.add_argument('--replace',action='store_true')
    commands.add_parser('probe',help='运行一次真实 DeepSeek 文件探针，最多 60 秒，会消耗所选账号额度')
    args=parser.parse_args();root=args.runtime_root.expanduser().resolve()
    try:
        if args.action=='register':
            config={'version':1,'program':str(Path(args.program).expanduser().absolute()) if args.program else '',
                    'home':str(Path(args.home).expanduser().resolve()),'provider':args.provider,'model':args.model}
            changed=register(root,config,args.replace)
            print('DSH 连接已登记；尚未运行真实模型探针。' if changed else '相同连接已登记，未重复修改。')
        elif args.action=='probe':
            proof=probe(root)
            print('DSH 实施探针通过。完整业务仍需网页发布→方案审批→实施→Astra 审核。')
            print('证据：'+proof['evidence'])
        print(json.dumps(connection_status(root),ensure_ascii=False,indent=2))
    except Exception as error:
        from workbench import Problem
        if args.action=='probe':
            # 失败使旧探针凭证失效，防止网页保留过时的通过状态。
            atomic_json(root/'dsh-verification.json',{'passed':False})
        print(str(error) if isinstance(error,(ConnectionProblem,Problem)) else '接入检查失败；未标记接通，请检查本机程序及隔离诊断。',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':sys.exit(main())
