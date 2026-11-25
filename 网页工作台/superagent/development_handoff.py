"""本机另签后只开或复用已推送的准确 PR，后续交给现有交付插件。"""

import base64
import binascii
import json
import hashlib
import fcntl
import os
import stat
from contextlib import contextmanager
import re
from subprocess import PIPE, TimeoutExpired, run
from urllib.parse import quote, urlencode

from superagent.development_delivery import MODES, _SHA, _approved_candidate, _key, authorize_release, read_release
from superagent import goal_checks
from superagent.outer_verification import sha256
from workbench import Problem

_BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,179}')


@contextmanager
def handoff_lock(root, repo, base, branch):
    directory = root/'.development-locks'
    directory.mkdir(mode=0o700,exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Problem('开发交付锁目录必须由本人私有持有',409)
    key = sha256([repo.casefold(),base,branch])
    fd = os.open(directory/(key+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise Problem('同一仓库分支的交付已在执行，未再次创建或写入 PR',409) from None
        yield fd
    finally:
        os.close(fd)


def additional_files(diff, verified_files, approved_paths, regular_nonexecutables):
    if not isinstance(diff,list) or len(diff)>=300:
        raise Problem('PR 变更清单不完整或超出本入口可核对范围',409)
    passive = ('.md','.txt','.png','.jpg','.jpeg','.webp')
    extra = sorted({
        path for row in diff for path in (row.get('filename'),row.get('previous_filename'))
        if path and path not in verified_files
    })
    consequential = {
        path for path in extra
        if (not path.lower().endswith(passive) or path.startswith(('.github/','ops/'))
            or path not in regular_nonexecutables())
    }
    if consequential != set(approved_paths):
        raise Problem('未验且可能影响运行的 PR 文件须逐项另签：' + '、'.join(sorted(consequential)),409)
    return extra



def gh_api(path, body=None):
    # 固定 GitHub 主机，错误只记录阶段，CLI 原始输出和登录信息不进入工单。
    args = ['gh', 'api', '--hostname', 'github.com', path]
    if body is not None:
        args.extend(('--method','POST','--input','-'))
    try:
        result = run(args, input=json.dumps(body) if body is not None else None,
                     text=True, stdout=PIPE, stderr=PIPE, timeout=30)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, TimeoutExpired, ValueError):
        pass
    raise Problem('GitHub 请求未确认，请核对登录、仓库权限及 PR 当前状态', 502)


def verify_settings_safety(settings, repo):
    settings_repo = settings.get('repo')
    review = settings.get('review') or {}
    agents = review.get('agents') or {}
    if (not isinstance(settings_repo,str) or settings_repo.casefold() != repo.casefold()
            or 'command' in review or settings.get('admin_merge')
            or any(isinstance(item,dict) and 'command' in item for item in agents.values())):
        raise ValueError('该仓库设置自定义本机审查命令、管理员合并或仓库身份不符')



def nonexecutable_paths(api_at, head):
    tree = api_at(f'git/commits/{head}')['tree']['sha']
    if not _SHA.fullmatch(tree):
        raise Problem('无法核对额外文件的 Git 文件模式',409)
    entries = api_at(f'git/trees/{tree}?recursive=1')
    if entries.get('truncated'):
        raise Problem('额外文件的 Git 文件模式清单已截断',409)
    return {entry['path'] for entry in entries['tree'] if entry['type']=='blob' and entry['mode']=='100644'}



def verify_head_files(api_at, head, files):
    if not isinstance(files,dict) or not files:
        raise Problem('工单缺少已验文件清单，未创建或派发 PR',409)
    try:
        tree = api_at(f'git/commits/{head}')['tree']['sha']
        if not _SHA.fullmatch(tree):
            raise ValueError('tree')
        trees, blobs = {}, {}
        for path, digest in files.items():
            parts = goal_checks._path(path).split('/')
            if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest):
                raise ValueError('digest')
            current = tree
            for index, part in enumerate(parts):
                if current not in trees:
                    response = api_at(f'git/trees/{current}')
                    if response.get('truncated'):
                        raise ValueError('tree coverage')
                    trees[current] = {entry['path']:entry for entry in response['tree']}
                item = trees[current][part]
                terminal = index == len(parts)-1
                valid = ((item['type'],item['mode']) in (('blob','100644'),('blob','100755'))
                         if terminal else item['type'] == 'tree' and item['mode'] == '040000')
                if not valid or not _SHA.fullmatch(item['sha']):
                    raise ValueError('nonregular path')
                current = item['sha']
            if current not in blobs:
                blob = api_at(f'git/blobs/{current}')
                if (blob['encoding'] != 'base64' or blob['sha'] != current
                        or not isinstance(blob['size'],int) or not 0 <= blob['size'] <= goal_checks.MAX_WORKSPACE_FILE):
                    raise ValueError('blob')
                data = base64.b64decode(''.join(blob['content'].split()),validate=True)
                if len(data) != blob['size']:
                    raise ValueError('blob size')
                blobs[current] = hashlib.sha256(data).hexdigest()
            if blobs[current] != digest:
                raise ValueError('mismatched bytes')
    except (Problem, KeyError, TypeError, AttributeError, ValueError, binascii.Error):
        raise Problem('已推送提交的路径或字节与工单验收文件不一致，未创建或派发 PR',409) from None
    return {'mapping':'same_path','files_checked':len(files)}



def prepare_handoff(store, ticket, *, mandate, digest, repo, base, branch, head, mode,
                    api=gh_api, validate_settings=None, approved_extra=()):
    _key(repo, 1, head)
    if base != 'main' or mode not in MODES or not isinstance(branch,str) or not _BRANCH.fullmatch(branch):
        raise Problem('只能交付本仓库已推送的分支到 main')
    if branch == base or any(not item or item in ('.','..') or item.endswith('.lock') for item in branch.split('/')):
        raise Problem('PR 源分支无效')
    def confirmed():
        task = store.get(ticket)
        candidate = _approved_candidate(task)
        if not candidate or candidate[:2] != (mandate,digest):
            raise Problem('工单尚未验收，或输入与当前已验归档版本不一致', 409)
        files = task['goal_delivery'].get('files')
        if not isinstance(files,dict) or not files or sha256(sorted(files.items())) != digest:
            raise Problem('工单的已验文件清单与归档摘要不一致，未创建或派发 PR',409)
        return files

    files = confirmed()
    prefix = 'repos/' + repo
    api_at = lambda path: api(prefix + '/' + path)

    try:
        base_ref = api_at('git/ref/heads/main')['object']['sha']
        if not _SHA.fullmatch(base_ref):
            raise ValueError('invalid base')
        blob = api_at('contents/ops/delivery.json?' + urlencode({'ref':base_ref}))
        settings = json.loads(base64.b64decode(blob['content']))
        verify_settings_safety(settings, repo)
        if validate_settings is not None:
            validate_settings(settings)
        repo = settings['repo']
    except (Problem, ValueError, TypeError, AttributeError, KeyError, binascii.Error):
        raise Problem('未能核对所选仓库 main 的交付配置，未创建或派发 PR', 409) from None

    def branch_head():
        return api_at('git/ref/heads/' + quote(branch,safe='/'))['object']['sha']

    def target_pr(pr):
        return (type(pr.get('number')) is int and pr['number'] > 0 and pr.get('state') == 'open'
                and pr['head']['sha'] == head and pr['head']['ref'] == branch
                and pr['head']['repo']['full_name'].casefold() == repo.casefold()
                and pr['base']['ref'] == base and not pr.get('auto_merge')
                and not any(label.get('name') in ('deliver','deliver-review') for label in pr.get('labels', ())))

    regular = None

    def regular_nonexecutables():
        nonlocal regular
        if regular is None:
            regular = nonexecutable_paths(api_at,head)
        return regular

    def match_changes(artifact_match, changes):
        unverified = additional_files(changes,files,approved_extra,regular_nonexecutables)
        if unverified:
            artifact_match['additional_pr_files'] = unverified
            artifact_match['approved_additional_files'] = sorted(approved_extra)

    def pr_changes(pr):
        changed = pr.get('changed_files')
        if type(changed) is not int or not 0 < changed < 300:
            raise Problem('无法完整核对 PR 的附加变更',409)
        pages = [api_at(f"pulls/{pr['number']}/files?per_page=100&page={page}")
                 for page in range(1,(changed-1)//100+2)]
        if not all(isinstance(page,list) for page in pages):
            raise Problem('PR 变更清单格式不正确',409)
        rows = [item for page in pages for item in page]
        if len(rows)!=changed:
            raise Problem('PR 的变更清单未完整读取',409)
        return rows

    previous = read_release(store,ticket).get('authorization') or {}
    if (previous.get('repo'),previous.get('head'),previous.get('mode')) == (repo.casefold(),head,mode):
        prior_pr = api_at(f"pulls/{previous['number']}")
        if prior_pr.get('merged_at'):
            if (mode != 'release' or prior_pr['head']['sha'] != head or prior_pr['head']['ref'] != branch
                    or prior_pr['head']['repo']['full_name'].casefold() != repo.casefold()
                    or prior_pr['base']['ref'] != base):
                raise Problem('已合并 PR 不匹配当前授权，未续验',409)
            artifact_match = verify_head_files(api_at,head,files)
            old_match = previous.get('artifact_match') or {}
            if old_match.get('pr_changes_checked'):
                artifact_match = old_match
            else:
                match_changes(artifact_match,pr_changes(prior_pr))
                artifact_match['pr_changes_checked'] = True
            authorized = authorize_release(store,ticket,mandate=mandate,digest=digest,repo=repo,
                                           number=previous['number'],head=head,mode=mode,artifact_match=artifact_match)
            return {'repo':repo,'number':previous['number'],'head':head,'base':base,'mode':mode,'ticket':ticket,
                    'branch':branch,'authorization':authorized['authorization']['id']}
    if branch_head() != head:
        raise Problem('已推送的分支与本次授权提交不一致，未创建或派发 PR', 409)
    artifact_match = verify_head_files(api_at, head, files)
    diff = api_at(f'compare/{base_ref}...{head}').get('files')
    match_changes(artifact_match,diff)
    artifact_match['pr_changes_checked'] = True
    selected = api_at('pulls?' + urlencode({
        'state':'open','head':repo.split('/')[0]+':'+branch,'base':base,'per_page':100,
    }))
    if len(selected) > 1 or selected and not target_pr(selected[0]):
        raise Problem('已有 PR 与批准范围不符或正在由另一交付轮处理', 409)
    if selected:
        number = selected[0]['number']
    else:
        confirmed()
        if branch_head() != head:
            raise Problem('创建 PR 前分支已变化，未派发', 409)
        created = api(prefix+'/pulls', {'title':'交付已验收开发改动','base':base,'head':branch,'draft':True,
                                      'body':'由已验收的 Autoloop 开发工单授权交付。'})
        if not target_pr(created):
            raise Problem('创建后发现分支或 PR 状态变化，草稿未交付，请重新核对', 409)
        number = created['number']
    current_pr = api_at(f'pulls/{number}')
    if not target_pr(current_pr) or branch_head() != head:
        raise Problem('PR 已变化，未交付，请重新核对', 409)
    exact_match = {key:artifact_match[key] for key in ('mapping','files_checked')}
    match_changes(exact_match,pr_changes(current_pr))
    exact_match['pr_changes_checked'] = True
    if exact_match != artifact_match:
        raise Problem('实际 PR 的额外变更与建单前清单不一致，未派发',409)
    authorized = authorize_release(store,ticket,mandate=mandate,digest=digest,repo=repo,number=number,head=head,mode=mode,
                                   artifact_match=artifact_match)
    return {'repo':repo,'number':number,'head':head,'base':base,'mode':mode,'ticket':ticket,'branch':branch,
            'authorization':authorized['authorization']['id']}
