"""飞书卡片按钮和需求表单。不在客户端值里传递任务号、批准内容或文件。"""
import json


def text(value):
    return {'tag':'plain_text','content':value}


def render(title,body,actions,form=None):
    elements=[{'tag':'div','text':text(body)}]
    def button(action,label):
        return {'tag':'button','text':text(label),'type':'default',
                'behaviors':[{'type':'callback','value':{'wb_action':action}}]}
    if form:
        action,label=form
        submit=button(action,label)
        submit.update(name='wb_'+action,type='primary',form_action_type='submit')
        elements.append({'tag':'form','name':'request','elements':[
            {'tag':'input','name':'requirement','required':True,'placeholder':text('输入需求或补充内容')},submit]})
    columns_per_row = 1 if any(a.startswith(('outer_open_', 'outer_replace_')) for a,_ in actions) else 2
    for i in range(0,len(actions),columns_per_row):
        columns=[{'tag':'column','width':'weighted','weight':1,'elements':[button(action,label)]}
                 for action,label in actions[i:i+columns_per_row]]
        elements.append({'tag':'column_set','columns':columns})
    return {'schema':'2.0','config':{'update_multi':True,'enable_forward':False},
            'header':{'title':text(title)},'body':{'elements':elements}}


def decode_click(event):
    if event.get('action_tag')!='button':return None
    try:
        value=json.loads(event.get('action_value') or '{}')
        if isinstance(value,dict) and set(value)=={'wb_action'}:
            action=value['wb_action']
        elif not value and event.get('action_name') in ('wb_submit_goal','wb_submit_note'):
            action=event['action_name'][3:]
        else:return None
        if not isinstance(action,str):return None
        content=''
        if action in ('submit_goal','submit_note'):
            fields=json.loads(event.get('form_value',''))
            if not isinstance(fields,dict):return None
            content=fields.get('requirement')
            if not isinstance(content,str) or not 1<=len(content.strip())<=6000:return None
        return action,content.strip()
    except (TypeError,ValueError):return None
