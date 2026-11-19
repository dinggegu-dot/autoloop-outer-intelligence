"""网页修改请求：持久请求、停稳、归档、重新规划，服务端负责续办。"""
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from agent_catalog import validate_roles, MODELS
from workbench import Problem, TaskBusy, now, RULE

ACTIVE = ('queued','claimed','unknown')


def change_reviewer(wb,task_id,body):
    """仅切换停稳任务的审核者；旧版归档，已批方案和预算不重做。"""
    from engine_adapters import resolve_routes, ConnectionProblem
    request_id=body.get('request_id');version=body.get('version');model=body.get('model')
    if not re.fullmatch(r'[a-f0-9-]{36}',str(request_id)) or type(version) is not int or model not in MODELS:
        raise Problem('请指定有效请求编号、当前版本与审核模型')
    payload={'version':version,'model':model}
    with wb.store.lease(task_id):
        task=wb.store.get(task_id)
        known=task.get('reviewer_requests',{}).get(request_id)
        if known:
            if known!=payload:raise Problem('同一审核变更请求不能更改内容',409)
        else:
            runs,_=wb.hub.records()
            if any(r['lane']==task_id and r['state'] in ACTIVE for r in runs):
                raise Problem('请等待当前执行停稳后切换审核模型',409)
            task=wb.store.recover_call(task_id)
            if task['version']!=version or task.get('pending_revision') or task['phase']!='review' or task['status'] not in ('awaiting_approval','blocked','paused','budget_exhausted'):
                raise Problem('仅支持停在审核阶段的未完成任务；其他修改请使用新版方案',409)
            approval=task.get('approval') or {}
            if approval.get('state')=='open' and approval.get('kind')!='review':
                raise Problem('请先处理当前业务审批，不能通过切换审核模型跳过',409)
            plan=task.get('plan') or {}
            original=next((d for d in reversed(task['decisions']) if d.get('kind')=='plan' and d.get('version')==version and d.get('approved')),None)
            if not original or not plan.get('steps') or not all(s.get('state')=='completed' for s in plan['steps']):
                raise Problem('必须已有用户批准方案且实施步骤全部完成',409)
            roles=validate_roles({**task['role_models'],'review':model})
            if roles==task['role_models']:raise Problem('审核模型没有变化，请使用原续办入口')
            try:routes=resolve_routes({**task,'role_models':roles,'role_routes':{}},wb.store.root)
            except ConnectionProblem as error:raise Problem(str(error)) from None
            if any(task.get('role_routes',{}).get(r)!=routes[r] for r in ('plan','execute')):
                raise Problem('规划或实施连接也发生变化，不能仅切换审核模型',409)
            manifest=archive(wb,task,request_id)
            def promote(t):
                new_version=version+1
                t.setdefault('versions',[]).append({'version':version,'status':t['status'],'summary':(t.get('review') or {}).get('summary',''),
                    'artifacts':list(manifest['files']),'at':manifest['at'],'role_models':t['role_models'],'role_routes':t.get('role_routes',{})})
                text='用户仅切换独立审核模型为 '+MODELS[model]+'；沿用已确认目标、实施方案、完成的成果和剩余时间预算，不重新规划或重做实施。当前模型分工替代旧目标文字中的审核模型称谓。'
                t.setdefault('changes',[]).append({'version':new_version,'text':text,'artifact':'','at':now()})
                # 继承的是同一方案的原批准，不伪造另一张用户审批卡。
                t['decisions'].append({'kind':'plan','version':new_version,'approved':True,'source':'retained_approval',
                    'from_version':version,'from_approval':original.get('approval'),'at':now(),'choice':'沿用已批准实施方案','action':text})
                t.setdefault('reviewer_requests',{})[request_id]=payload
                t.update(version=new_version,plan={**plan,'version':new_version,'retained_from_version':version},role_models=roles,role_routes=routes,
                    phase='review',status='created',approval=None,review=None,paused=False,error='',review_failures=0,
                    active_role=None,active_model=None,active_engine=None)
            wb.store.change(task_id,promote)
            wb.store.outbox(task_id,request_id)
            wb.store.event(task_id,'reviewer_changed','已按用户要求切换审核模型，沿用已批方案和成果',{'version':version+1,'from':task['role_models']['review'],'to':model,'request_id':request_id})
    result=wb.dispatch(task_id,request_id)
    return {'queued':bool(result and RULE in result.get('routed',[])),'version':wb.store.get(task_id)['version'],'event':(result or {}).get('event')}


def request_revision(wb,task_id,body):
    request_id=body.get('request_id')
    if not re.fullmatch(r'[a-f0-9-]{36}',str(request_id)):
        raise Problem('缺少有效请求编号')
    text=body.get('text');version=body.get('version');artifact=body.get('artifact','')
    if not isinstance(text,str) or not 1<=len(text.strip())<=6000 or type(version) is not int:
        raise Problem('请填写修改要求（最多 6000 字）并指定当前版本')
    try:roles=validate_roles(body.get('role_models'))
    except ValueError as e:raise Problem(str(e)) from None
    payload={'base_version':version,'text':text.strip(),'artifact':artifact,'role_models':roles}
    def save(task):
        old=task.get('revision_requests',{}).get(request_id)
        if old:
            if old['payload']!=payload:raise Problem('同一修改请求不能提交不同内容',409)
            if task.get('pending_revision')==request_id:
                old['error']=''
                if old['state']=='suspended':old['state']='waiting_stop' if task['version']==version else 'resume_planning'
                if old['state']=='blocked_dispatch':
                    old.update(state='dispatching',dispatch_id=str(uuid.uuid4()))
            return
        if task.get('pending_revision'):raise Problem('上一项修改仍在处理，请先等待或重试',409)
        if task['version']!=version:raise Problem('方案版本已变化，请重新核对修改内容',409)
        if artifact and artifact not in task['artifacts']:raise Problem('所选成果不属于当前版本',409)
        request={'id':request_id,'payload':payload,'at':now(),'state':'waiting_stop','error':'','dispatch_id':request_id}
        task.setdefault('revision_requests',{})[request_id]=request
        task.update(pending_revision=request_id,paused=True)
    wb.store.change(task_id,save)
    task=wb.store.get(task_id)
    if task.get('pending_revision')==request_id and task['revision_requests'][request_id]['state'] in ('waiting_stop','resume_planning'):
        cancel_revision(wb,task_id,request_id)
    return {'recorded':True,'request':wb.store.get(task_id)['revision_requests'][request_id]}


def cancel_revision(wb,task_id,request_id):
    error=''
    try:
        runs,_=wb.hub.records()
        for run in runs:
            if run['lane']==task_id and run['state'] in ACTIVE:
                wb.hub.request('POST','/v1/runs/%d/cancel'%run['id'],{})
    except (Problem,OSError,ValueError) as e:
        error='停止尚未确认，请重试修改请求：'+str(e)[:180]
    wb.store.change(task_id,lambda t:t['revision_requests'][request_id].update(error=error))


def archive(wb,task,request_id):
    base=wb.store.workspace(task['id']).parent/'versions';base.mkdir(exist_ok=True)
    dest=base/str(task['version'])
    if dest.exists():
        manifest=json.loads((dest/'manifest.json').read_text())
        if manifest['revision_request']!=request_id:raise Problem('版本归档冲突，已停止改版')
        return manifest
    staging=base/('.pending-'+uuid.uuid4().hex);staging.mkdir()
    manifest={'version':task['version'],'revision_request':request_id,'at':now(),'files':{},'task':task}
    try:
        total=0
        for name in task['artifacts']:
            source=wb.artifact(task['id'],name)
            # 已登记产物保存为独立副本；新版模型不能改写历史验收原件。
            total+=source.stat().st_size
            if total>100*1024*1024:raise Problem('登记成果超过 100 MB，请先整理后再改版')
            target=staging/'artifacts'/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            manifest['files'][name]={'size':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
        (staging/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        os.replace(staging,dest)
    except Exception:
        shutil.rmtree(staging,ignore_errors=True)
        raise
    return manifest


def reconcile(wb):
    # GET/SSE 不改变任务；服务端定时器在关闭浏览器后仍能完成停稳和派发。
    for summary in wb.store.list():
        request_id=summary.get('pending_revision')
        if not request_id:continue
        try:
            task=wb.store.get(summary['id']);request=task['revision_requests'][request_id]
            if request['state'] not in ('waiting_stop','dispatching','resume_planning'):continue
            if request['error'] and request['state']!='dispatching':continue
            if request['state']=='resume_planning':
                runs,_=wb.hub.records()
                if any(r['lane']==task['id'] and r['state'] in ACTIVE for r in runs):continue
                with wb.store.lease(task['id']):
                    wb.store.recover_call(task['id'])
                    def restart(t):
                        t.update(paused=False,status='created',error='')
                        t['revision_requests'][request_id].update(state='dispatching',dispatch_id=str(uuid.uuid4()),error='')
                    wb.store.change(task['id'],restart)
            elif request['state']=='waiting_stop':
                runs,_=wb.hub.records()
                if any(r['lane']==task['id'] and r['state'] in ACTIVE for r in runs):continue
                with wb.store.lease(task['id']):
                    task=wb.store.recover_call(task['id']);request=task['revision_requests'][request_id]
                    if task['version']!=request['payload']['base_version']:raise Problem('版本已变化，不能套用旧修改',409)
                    manifest=archive(wb,task,request_id)
                    payload=request['payload']
                    def promote(t):
                        old={'version':t['version'],'status':t['status'],'summary':(t.get('review') or t.get('plan') or {}).get('summary','尚未形成方案'),
                             'artifacts':list(manifest['files']),'at':manifest['at'],'role_models':t.get('role_models',{}),'role_routes':t.get('role_routes',{})}
                        t.setdefault('versions',[]).append(old)
                        t.setdefault('changes',[]).append({'version':t['version']+1,'text':payload['text'],'artifact':payload['artifact'],'at':request['at']})
                        t.update(version=t['version']+1,phase='plan',step=0,plan=None,approval=None,review=None,checks=[],artifacts=[],paused=False,
                                 status='created',error='',execution_mode='auto',time_accounting_since=t.get('time_accounting_since') or now(),role_models=payload['role_models'],role_routes={},review_failures=0,active_role=None,active_model=None,active_engine=None)
                        t['revision_requests'][request_id].update(state='dispatching',version=t['version'],error='')
                    wb.store.change(task['id'],promote)
                    wb.store.event(task['id'],'revision','已保存旧版本，正在重新规划',{'version':task['version']+1})
            # 必须释放执行锁后派发；原生 worker 可在 HTTP 返回前领取事件。
            dispatch_id=wb.store.get(task['id'])['revision_requests'][request_id].get('dispatch_id',request_id)
            prior=wb.store.dispatch_result(task['id'],dispatch_id)
            if prior and 'odyssey-workbench' not in prior.get('routed',[]):
                wb.store.change(task['id'],lambda t:t['revision_requests'][request_id].update(state='blocked_dispatch',error='中台未路由这次请求，请恢复规则后点击重试'))
                continue
            result=wb.dispatch(task['id'],dispatch_id)
            if result:
                def complete(t):
                    t['revision_requests'][request_id].update(state='planning',error='')
                    if t.get('pending_revision')==request_id:t['pending_revision']=None
                wb.store.change(task['id'],complete)
        except TaskBusy:
            continue
        except (Problem,OSError,ValueError) as error:
            wb.store.change(summary['id'],lambda t:t['revision_requests'][request_id].update(error=str(error)[:240]))
