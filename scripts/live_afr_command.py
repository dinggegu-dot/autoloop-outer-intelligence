"""Run AFR v1 and a real Autoloop Hub/Worker using only the AFR synthetic fixture."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import Request, ProxyHandler, build_opener

from afr_command_agent import EVENT, SOURCE

AGENT = 'afr-local-lookup'
FIXTURE_QUERY = '机柜样本42'
NO_MATCH_QUERY = '空白对照词776b35'
ADAPTER = Path(__file__).with_name('afr_command_agent.py').resolve()
HEX = re.compile(r'[a-f0-9]{64}')
TICKET = re.compile(r'wb-[a-f0-9]{32}')


def rules(workdir, python, afr_root, afr_python, vault, requests):
    command = [str(python),'-B',str(ADAPTER),'--afr-root',str(afr_root),
               '--afr-python',str(afr_python),'--vault',str(vault),'--requests',str(requests)]
    return {'sources':{SOURCE:{'name':'AFR 本机查询'}},'channels':{},
            'agents':{AGENT:{'name':'AFR 本机查询','worker':'local','use':'command',
                             'command':command,'timeout':1}},
            'rules':[{'name':AGENT,'when':{'source':SOURCE,'types':[EVENT]},'to':AGENT,
                      'cwd':str(workdir),'parallel':1,'tell':'never',
                      'prompt':'Read the private request by ID and use the registered AFR v1 CLI.'}]}


def put(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False)+'\n')


def http(url, path, key=None, value=None):
    headers = {'Content-Type':'application/json'}
    if key:
        headers['Authorization']='Bearer '+key
    body = json.dumps(value).encode() if value is not None else None
    with build_opener(ProxyHandler({})).open(Request(url+path,data=body,headers=headers),timeout=3) as response:
        return json.load(response)


def wait_status(ready, process, limit=15):
    expires = time.monotonic()+limit
    while time.monotonic()<expires:
        if process.poll() is not None:
            raise RuntimeError('isolated_service_exited')
        try:
            found=ready()
            if found: return found
        except OSError:
            pass
        time.sleep(.15)
    raise RuntimeError('isolated_service_timeout')


def stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


def invoke(args):
    root=args.stage.resolve()
    if (args.stage.is_symlink() or root.parent != Path('/private/tmp') or not root.is_dir()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077 or any(root.iterdir())):
        raise ValueError('fresh_private_trial_directory_required')
    key_home=root/'hub'
    worker_home=root/'worker'
    vault=root/'synthetic-vault'
    inbox=root/'private-requests'
    cwd=root/'worker-cwd'
    for directory in (worker_home,key_home,vault,inbox,cwd):
        directory.mkdir()
    source = args.afr_root/'afr_agent/tests/fixtures/上架率小样.md'
    note = vault/source.name
    shutil.copyfile(source,note)
    test_input=note.read_text()
    if FIXTURE_QUERY not in test_input:
        raise ValueError('unexpected_fixture')
    rule_json = rules(cwd,Path(sys.executable),args.afr_root,args.afr_python,vault,inbox)
    put(root/'candidate.rules.json',rule_json)
    put(key_home/'rules.json',rule_json)
    cli=[str(args.node),'--disable-warning=ExperimentalWarning',str(args.autoloop_entry)]
    def env(home):
        return {'PATH':os.defpath,'HOME':str(Path.home()),'TMPDIR':str(root),'AGENT_AUTOLOOP_HOME':str(home)}
    def run(home,*command):
        return subprocess.run([*cli,*command],env=env(home),check=True,capture_output=True,
                              text=True,timeout=10).stdout.strip()
    run(key_home,'check')
    owner=run(key_home,'key','new','owner','owner')
    source_key=run(key_home,'key','new','source',SOURCE)
    worker_key=run(key_home,'key','new','worker','local')
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]{20,300}',key) for key in (owner,source_key,worker_key)):
        raise RuntimeError('unexpected_isolated_key_shape')
    if args.port:
        port=args.port
    else:
        with socket.socket() as available:
            available.bind(('127.0.0.1',0))
            port=available.getsockname()[1]
    url='http://127.0.0.1:'+str(port)
    put(key_home/'config.json',{'url':url,'hub':{'listen':'127.0.0.1:'+str(port)},'owner':owner,
                               'source':{'name':SOURCE,'key':source_key}})
    put(worker_home/'config.json',{'url':url,'worker':{'name':'local','key':worker_key,'capacity':1}})
    with ExitStack() as services:
        def launch(home):
            log=services.enter_context((home/'up.log').open('w'))
            child=subprocess.Popen([*cli,'up'],env=env(home),stdout=log,stderr=subprocess.STDOUT)
            services.callback(stop,child)
            return child
        hub=launch(key_home)
        wait_status(lambda:'listening on 127.0.0.1:'+str(port) in (key_home/'up.log').read_text()
                    and isinstance(http(url,'/v1/state',owner),dict),hub)
        worker=launch(worker_home)
        wait_status(lambda:any(x['id']==AGENT and x['online'] for x in http(url,'/v1/state',owner)['agents']),worker)

        def dispatch(request_id, local_input, expected):
            request_id = args.ticket+'-'+request_id if args.ticket else request_id
            put(inbox/(request_id+'.json'),local_input)
            event={'id':request_id,'type':EVENT,'subject':request_id}
            created=http(url,'/v1/events',source_key,event)
            assert not created['duplicate'] and created['routed']==[AGENT], created.get('routed')
            duplicate=http(url,'/v1/events',source_key,event)
            assert duplicate['duplicate'] and duplicate['event']==created['event']
            def finished():
                rows=http(url,f"/v1/events/{created['event']}",source_key)['runs']
                return rows if rows and rows[0]['state'] in ('done','failed','cancelled','nothing') else None
            rows=wait_status(finished,worker,limit=20)
            assert len(rows)==1 and rows[0]['state']=='done'
            row=rows[0]
            result=json.loads(row['summary'])
            assert set(result)=={'version','status','verified_count','references','request_id'}
            assert result['version']==1 and result['request_id']==request_id and result['status']==expected
            assert result['verified_count']==len(result['references'])
            assert ('ok'==expected)==(result['verified_count']>0)
            assert all(set(r)=={'ref','revision'} and all(HEX.fullmatch(value) for value in r.values())
                       for r in result['references'])
            ledger=http(url,f"/v1/runs/{row['run']}/why",owner)
            serialized=json.dumps(ledger,ensure_ascii=False)
            for private in (FIXTURE_QUERY,NO_MATCH_QUERY,'十台机柜','上架率小样.md','excerpt','obsidian://'):
                assert private not in serialized
            return {'event':created['event'],'run':row['run'],'receipt':result,
                    'route_confirmed':True,'duplicate_reused_run':True,'private_fields_absent_from_trace':True}

        cases={}
        cases['lookup']=dispatch('lookup',{'query':FIXTURE_QUERY},'ok')
        cases['no_match']=dispatch('no-match',{'query':NO_MATCH_QUERY},'no_match')
        reference=cases['lookup']['receipt']['references'][0]
        note.write_text(test_input.replace('十台机柜','十二台机柜'))
        cases['revision_changed']=dispatch('old-revision',reference,'revision_changed')
        cases['lookup_after_edit']=dispatch('lookup-after-edit',{'query':FIXTURE_QUERY},'ok')
        current=next(ref for ref in cases['lookup_after_edit']['receipt']['references'] if ref['ref']==reference['ref'])
        assert current['revision']!=reference['revision']
        put(root/'evidence.json',{'schema_version':1,'afr_cli_version':1,'hub_worker':'real_local',
            'fixture_sha256':hashlib.sha256(test_input.encode()).hexdigest(),'cases':cases,
            'ticket_id':args.ticket,
            'original_fixture_unchanged':source.read_text()==test_input,'external_model_calls':0})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',type=Path)
    parser.add_argument('--afr-root',required=True,type=Path)
    parser.add_argument('--afr-python',required=True,type=Path)
    parser.add_argument('--node',required=True,type=Path)
    parser.add_argument('--autoloop-entry',required=True,type=Path)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--ticket',help='Optional V2 ticket correlation; never submits or approves it')
    args=parser.parse_args()
    os.umask(0o077)
    if args.ticket and not TICKET.fullmatch(args.ticket):
        parser.error('ticket must be a Workbench ticket id')
    paths=(args.afr_root,args.afr_python,args.node,args.autoloop_entry)
    if not all(path.is_absolute() and path.exists() for path in paths) or args.port!=0 and not 1024<=args.port<=65535:
        parser.error('paths must be absolute and existing, port must be 0 or 1024..65535')
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupt)
    invoke(args)
    print('AFR_SEARCH_READ_REAL_HUB_VERIFIED')


if __name__=='__main__':
    main()
