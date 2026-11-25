#!/usr/bin/env python3
"""验证现有交付插件内所有经过 gh 的写操作，含审查闭包中的 PR 和状态写入。"""

import base64
import json
import os
from pathlib import Path
from subprocess import run
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '网页工作台'))
from superagent.development_delivery import read_release
from superagent.development_handoff import verify_settings_safety
from workbench import Store


def guarded_arguments(target, argv):
    prefix = 'repos/' + target['repo'] + '/'
    expected_pr = f"pulls/{target['number']}"
    if argv == ['api','user','--jq','.id']:
        return False
    if argv[:1] == ['api'] and len(argv) > 1 and argv[1].startswith(prefix):
        path = argv[1][len(prefix):]
        flags = argv[2:]
        if flags in ([],['--method','GET'],['--paginate','--slurp']):
            return False
        if len(flags) != 4 or flags[0] != '--method' or flags[2:] != ['--input','-']:
            raise ValueError('GitHub 命令参数不在发布授权内')
        method = flags[1]
        if (method == 'POST' and path == 'statuses/'+target['head']
                or method == 'PATCH' and path == expected_pr
                or method == 'PUT' and path == expected_pr+'/merge' and target['mode'] == 'release'):
            return True
    if argv == ['pr','ready',str(target['number']),'--repo',target['repo']] and target['mode'] == 'release':
        return True
    raise ValueError('GitHub 命令不在本次工单交付范围内')


def main():
    target = json.loads(os.environ['AUTOLOOP_PINNED_DELIVERY_TARGET'])
    real_gh = os.environ['AUTOLOOP_PINNED_REAL_GH']
    store = Store(os.environ['AUTOLOOP_TICKET_DELIVERY_RUNTIME'])

    def approval():
        auth = read_release(store,target['ticket']).get('authorization') or {}
        if auth.get('id') != target['authorization'] or not auth.get('artifact_match'):
            raise ValueError('工单发布授权已变化或缺少文件匹配证据')

    args = sys.argv[1:]
    approval()
    write = guarded_arguments(target,args)
    if write:
        pr = run([real_gh,'api',f"repos/{target['repo']}/pulls/{target['number']}"],capture_output=True,check=True)
        pr = json.loads(pr.stdout)
        if (pr['head']['sha'] != target['head'] or pr['head']['ref'] != target['branch']
                or pr['head']['repo']['full_name'].casefold() != target['repo'].casefold()
                or pr['base']['ref'] != 'main' or pr.get('auto_merge')
                or any(label.get('name') in ('deliver','deliver-review') for label in pr['labels'])):
            raise ValueError('发布前检测到 PR 提交变化或由其他交付轮处理')
        approval()
        if args[:1] == ['api']:
            payload = sys.stdin.buffer.read(1024*1024+1)
            if len(payload)>1024*1024:
                raise ValueError('GitHub 写操作超出长度')
            body = json.loads(payload)
            if (args[3] == 'PATCH' and (set(body) != {'body'} or not isinstance(body['body'],str))
                    or args[3] == 'PUT' and body != {'sha':target['head'],'merge_method':'squash'}):
                raise ValueError('GitHub 写入内容与授权版本不符')
            approval()
            result = run([real_gh,*args],input=payload,stdout=sys.stdout.buffer,check=True,stderr=-1)
            return
    if args[:1] == ['api'] and '/contents/ops/delivery.json?' in args[1]:
        result = run([real_gh,*args],capture_output=True,check=True)
        settings = json.loads(base64.b64decode(json.loads(result.stdout)['content']))
        verify_settings_safety(settings,target['repo'])
        sys.stdout.buffer.write(result.stdout)
        return
    os.execv(real_gh,[real_gh,*args])


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('本单发布守卫拒绝了越权、变更或未经确认的 GitHub 操作')
