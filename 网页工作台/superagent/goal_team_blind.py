"""把同一份留出输入送给单 Sol 与团队，私有来源映射由宿主封存。"""
import json
from pathlib import Path

from superagent import goal_blind_review as blind
from workbench import Problem


def required_blind(acceptance):
    rows = [row for row in acceptance.get('criteria', [])
            if row.get('required') and row.get('verifier_id') == 'blind-projection']
    if len(rows) > 1:
        raise Problem('每单只能绑定一个盲评候选')
    return rows[0] if rows else None


def make_blind_draft(ask, assignment, context, item, workspace, directory, calls, *, allowed):
    from superagent.goal_runtime import save_json

    if allowed < 4 or not assignment.get('preference_answer'):
        raise Problem('盲评生成需要已生效的偏好和可用的四个角色轮次')
    if assignment['step'] + 1 != len(assignment['plan']['steps']):
        raise Problem('匿名对照须在同一步合并所有必要验收项')
    exemplar = Path(__file__).resolve().parents[1] / '验收样例' / '匿名评测' / '留出输入.json'
    heldout = blind.load_json(exemplar.read_bytes())
    expected = item['expected_result']['case_ids']
    if expected != [row['id'] for row in heldout['cases']]:
        raise Problem('授权的例号顺序与本机冻结的合成留出资料不一致')
    responses = {'type':'object','additionalProperties':False,
                 'properties':{case_id:{'type':'string'} for case_id in expected},'required':expected}
    base_context = {key:context[key] for key in ('goal','owner_preference')}
    if context.get('repair_or_review_feedback'):
        base_context['repair_or_review_feedback'] = context['repair_or_review_feedback']
    lead = ask('lead','根据已记录的偏好写下修改方法和保留项。先冻结策略，后续才会读取留出情形。',
               base_context, effort='medium')
    advisor = ask('advisor','独立给出一个可观察反例及修法。此时只读取用户原话和意见。',
                  base_context, effort='medium')
    input_context = {**base_context,'heldout':heldout}
    instruction = (
        '为 heldout.cases 中的每个情形写一两句自然的回应。'
        '按输入 id 把回应写到输出的同名字段，只填写回应正文。不要写模型名、路线名或来源标签。'
    )
    first = ask('implementer',instruction,input_context,effort='medium',response_schema=responses)
    second = ask('implementer',instruction,{**input_context,'lead_plan':lead,'independent_advice':advisor},
                 effort='medium',response_schema=responses)
    if set(first) != set(expected) or set(second) != set(expected):
        raise Problem('匿名生成回执缺少已冻结的情形编号')
    baseline, team = ({'cases':[{'id':case_id,'response':arm[case_id]} for case_id in expected]}
                      for arm in (first, second))
    public, private = blind.prepare(assignment['id'],heldout,baseline,team)
    path = workspace/item['test_spec']['path']
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise Problem('匿名候选路径不能是链接')
    # 两份明确指向不同的实际 Codex 调用。路径和源内容都留在受信目录。
    origins = {key:{'ref':call['id'],'prompt_sha256':call['prompt_sha256']}
               for key,call in zip(('baseline','team'),calls[-2:])}
    save_json(directory/'blind-preparation.json',dict(public=public,private=private,heldout=heldout,
                                                     baseline=baseline,team=team,origins=origins))
    save_json(path,public['projection'])
    return {'summary':'已将同一份合成留出资料按两种路线实际生成并封存匿名版，等待独立复验和本人锁票。',
            'action':'done','options':[],'retain':[],'verify':['独立检查例号、结构及显式来源标记']}
