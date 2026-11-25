#!/usr/bin/env python3
"""供可信本机操作者登记开发发布授权，也可作为 delivery 插件的进度回调。"""

import argparse
import json
import os
import stat
import shutil
import sys
from subprocess import run
from tempfile import TemporaryDirectory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '网页工作台'))
from superagent.development_delivery import authorize_release, read_release, record_progress, register_release
from superagent.development_handoff import handoff_lock, prepare_handoff
from workbench import Problem, Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path)
    commands = parser.add_subparsers(dest='action')
    approve = commands.add_parser('authorize')
    handoff = commands.add_parser('handoff')
    for command in (approve,handoff):
        command.add_argument('ticket')
        for field in ('digest', 'repo', 'head'):
            command.add_argument('--' + field, required=True)
        command.add_argument('--mandate', type=int, required=True)
        command.add_argument('--mode', choices=('review', 'release'), required=True)
    approve.add_argument('--number', type=int, required=True)
    handoff.add_argument('--base', choices=('main',), required=True)
    handoff.add_argument('--branch', required=True)
    handoff.add_argument('--allow-extra-file', action='append', default=[])
    install_default = Path.home() / '.agent-autoloop'
    handoff.add_argument('--delivery-app',type=Path,default=install_default/'app')
    handoff.add_argument('--node',type=Path,required=True)
    commands.add_parser('status').add_argument('ticket')
    commands.add_parser('register').add_argument('ticket')
    args = parser.parse_args()
    root = args.runtime or os.environ.get('AUTOLOOP_TICKET_DELIVERY_RUNTIME')
    if not root or not Path(root).is_absolute():
        parser.error('请指定本机正式工单账本的绝对目录')
    root = Path(root).resolve(strict=True)
    ledger = root / 'tasks.sqlite3'
    info = ledger.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        parser.error('仅接受本人持有且权限为私有的工单账本')
    store = Store(root)
    if args.action == 'authorize':
        result = authorize_release(store, args.ticket, **{
            key: vars(args)[key] for key in ('mandate','digest','repo','number','head','mode')
        })
    elif args.action == 'handoff':
        github = shutil.which('gh')
        if not github:
            parser.error('需要本机已安装并授权的 GitHub CLI')
        github = str(Path(github).resolve(strict=True))
        node = args.node.resolve(strict=True)
        app = args.delivery_app.resolve(strict=True)
        if not node.is_file() or not all((app / 'plugins/delivery' / file).is_file()
                                         for file in ('delivery.ts','github.ts','handoff.ts')):
            parser.error('缺少可信安装的 Node 24 或交付插件')
        here = Path(__file__).resolve()
        worker = here.with_name('pinned_ticket_delivery.mjs')

        def validate_settings(settings):
            checked = run([str(node), str(worker), str(app), '--validate'],
                          input=json.dumps(settings), text=True, capture_output=True)
            if checked.returncode:
                raise Problem('可信交付工具拒绝仓库配置', 409)

        with handoff_lock(root,args.repo,args.base,args.branch) as lock:
            target = prepare_handoff(store,args.ticket, validate_settings=validate_settings, approved_extra=args.allow_extra_file, **{
                key: vars(args)[key] for key in ('mandate','digest','repo','base','branch','head','mode')
            })
            with TemporaryDirectory(prefix='autoloop-pinned-gh-') as commands_path:
                (Path(commands_path)/'gh').symlink_to(here.with_name('pinned_delivery_gh.py'))
                result = run([str(node), str(worker), str(app), str(here), json.dumps(target)], pass_fds=(lock,), env=dict(
                    os.environ, AUTOLOOP_TICKET_DELIVERY_RUNTIME=str(root), GH_HOST='github.com',
                    GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0',
                    AUTOLOOP_PINNED_DELIVERY_TARGET=json.dumps(target), AUTOLOOP_PINNED_REAL_GH=github,
                    PATH=commands_path+os.pathsep+os.environ.get('PATH',''),
                ))
        print(json.dumps(read_release(store,args.ticket),ensure_ascii=False))
        return result.returncode
    elif args.action == 'register':
        result = register_release(store, args.ticket)
    elif args.action == 'status':
        result = read_release(store, args.ticket)
    else:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            parser.error('交付回执超出大小上限')
        result = record_progress(store, json.loads(raw))
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Problem, ValueError, OSError) as error:
        sys.exit(str(error))
