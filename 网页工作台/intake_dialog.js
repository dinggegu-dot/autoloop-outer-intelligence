// 弹窗独立于轮询重绘的任务详情，保留使用者正在输入的资料。
export function editIntake(task,tasks,post,refresh){
  const dialog=document.createElement('dialog');
  const add=(tag,text,parent=dialog)=>{
    const element=document.createElement(tag);
    element.textContent=text;
    parent.append(element);
    return element;
  };
  dialog.setAttribute('aria-label','补充与关联工单');
  add('h3','补充与关联工单');
  add('p',task.name+' · '+task.id.slice(-8));
  const input=add('textarea','',add('label','补充资料 '));
  input.rows=4;input.cols=36;input.maxLength=8000;
  const save=add('button','保存补充');
  const status=add('p','');
  status.setAttribute('role','status');
  let pending=false;
  let noteRequest=crypto.randomUUID();
  input.oninput=()=>{noteRequest=crypto.randomUUID();};
  async function submit(action,body){
    if(pending)return;
    pending=true;
    try{
      await post('/api/tasks/'+task.id+'/'+action,body);
      dialog.close();
      await refresh();
    }catch(error){status.textContent=error.message;}
    finally{pending=false;}
  }
  save.onclick=()=>{
    if(!input.value.trim()){status.textContent='请先填写资料。';input.focus();return;}
    submit('note',{text:input.value,request_id:noteRequest});
  };
  const targets=tasks.filter(item=>item.id!==task.id && item.can_move_notes);
  if(task.inbox_notes.length && task.can_move_notes && targets.length){
    add('h4','纠正资料归属');
    const note=add('select','',add('label','资料 '));
    for(const item of task.inbox_notes)note.add(new Option(item.at+' · '+item.body.slice(0,50),item.message));
    const preview=add('p','');
    let moveRequest;
    note.onchange=()=>{moveRequest=crypto.randomUUID();preview.textContent=task.inbox_notes[note.selectedIndex].body;};
    note.onchange();
    const dest=add('select','',add('label','移至 '));
    dest.add(new Option('请选择目标工单',''));
    dest.onchange=()=>{moveRequest=crypto.randomUUID();};
    for(const item of targets)dest.add(new Option(item.name+' · '+item.id.slice(-8),item.id));
    add('button','确认改投').onclick=()=>{
      if(!dest.value){status.textContent='请选择目标工单。';return;}
      const item=task.inbox_notes[note.selectedIndex];
      submit('move-note',{message:item.message,revision:item.revision,target:dest.value,request_id:moveRequest});
    };
  }else if(task.inbox_notes.length){
    add('p','资料改投需要两张尚未派发的工单。已派发的任务可在方案修改中说明更正。');
  }
  add('button','关闭').onclick=()=>dialog.close();
  dialog.onclose=()=>dialog.remove();
  document.body.append(dialog);dialog.showModal();input.focus();
}
