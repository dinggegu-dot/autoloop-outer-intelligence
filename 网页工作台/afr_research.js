'use strict';
const id=s=>document.getElementById(s);
const ticket=location.pathname.split('/').pop();
const api='/api/research/'+ticket;
id('back').href='http://127.0.0.1:8765/?task='+encodeURIComponent(ticket);
id('ticket').textContent='工单 '+ticket.slice(-8);
const first='梳理晨雾试点的七份材料，比较不同观点，注明出处和未决问题。';
const follow='继续研究晨雾试点：哪些来源解释了分歧，还有哪些问题需要复核？';
const labels={requested:'等待领取',queued:'排队中',claimed:'读取与归纳中',running:'研究中',done:'已返回',failed:'运行失败',cancelled:'已取消',nothing:'没有派工',unconfirmed:'派发待确认'};
const terminal=new Set(['done','failed','cancelled','nothing','unconfirmed']);
let seenLast='',token='',turns=[],current='',latest='',lastKey='',sending=false,busyRead=false,nonce='';
function node(type,text){const n=document.createElement(type);n.textContent=text;return n;}
async function req(path,body){
 const opts={cache:'no-store'};
 if(body){opts.method='POST';opts.headers={'Content-Type':'application/json','X-Odyssey-CSRF':token};opts.body=JSON.stringify(body);}
 const response=await fetch(path,opts);const data=await response.json();
 if(!response.ok)throw Error(data.error||'请刷新后核对本轮状态。');return data;
}
function fileUrl(turn,name,view){const q=new URLSearchParams({file:name});if(view)q.set('view','inline');return api+'/'+turn.request_id+'?'+q;}
function link(title,url){const a=node('a',title);a.href=url;return a;}
function section(parent,title,lines){if(!lines?.length)return;const box=node('details','');box.append(node('summary',title));const list=node('ul','');for(const text of lines)list.append(node('li',String(text)));box.append(list);parent.append(box);}
function showCanvas(graph){
 const d=document.createElement('dialog');d.className='graph-dialog';const close=node('button','关闭白板');close.onclick=()=>d.close();
 const bar=node('div','');bar.className='section-head';bar.append(node('h2','研究白板'),close);d.append(bar);
 const viewport=node('div','');viewport.className='graph-scroll';viewport.tabIndex=0;viewport.setAttribute('aria-label','研究白板，可滚动查看论点与来源');
 const stage=node('div','');stage.className='graph-stage';const nodes=graph.nodes||[];
 const left=Math.min(0,...nodes.map(n=>n.x))-20,top=Math.min(0,...nodes.map(n=>n.y))-20,scale=.7;
 const xy=n=>({x:(n.x-left)*scale,y:(n.y-top)*scale,w:n.width*scale,h:n.height*scale});
 const width=Math.ceil(Math.max(1,...nodes.map(n=>xy(n).x+xy(n).w))+20),height=Math.ceil(Math.max(1,...nodes.map(n=>xy(n).y+xy(n).h))+20);
 stage.style.width=width+'px';stage.style.height=height+'px';
 const line=document.createElement('canvas');line.width=width;line.height=height;line.setAttribute('aria-hidden','true');const ctx=line.getContext('2d');stage.append(line);
 const positions=new Map(nodes.map(n=>[n.id,xy(n)]));ctx.strokeStyle='#a1b2ab';ctx.lineWidth=2;
 for(const e of graph.edges||[]){const a=positions.get(e.fromNode),b=positions.get(e.toNode);if(!a||!b)continue;
   const x=a.x+a.w,y=a.y+a.h/2,end=b.y+b.h/2;ctx.beginPath();ctx.moveTo(x,y);ctx.bezierCurveTo(x+45,y,b.x-45,end,b.x,end);ctx.stroke();}
 for(const n of nodes){const v=xy(n),card=node('article',n.text||n.file||'出处');card.className='graph-card';card.dataset.color=n.color||'';
  Object.assign(card.style,{left:v.x+'px',top:v.y+'px',width:v.w+'px',height:v.h+'px'});stage.append(card);}
 viewport.append(stage);d.append(viewport);document.body.append(d);d.onclose=()=>d.remove();d.showModal();
}
function controls(){
 const active=turns.some(t=>!terminal.has(t.state));
 const parent=turns.filter(t=>t.files.includes('研究记录.json')).at(-1);
 latest=parent?.request_id||'';
 id('send').disabled=!token||active||sending;
 id('send').textContent=active?'研究进行中':latest?'发送同单追问':'发送研究任务';
 id('action').textContent=latest?'沿第 '+(turns.indexOf(parent)+1)+' 轮追问':'发送研究任务';
 id('question').placeholder=latest?follow:first;
}
async function draw(){
 const box=id('answer'),turn=turns.find(t=>t.request_id===current);
 if(!turn){box.replaceChildren(node('p','提交研究任务后，这里会显示执行进度、结论和出处。'));return;}
 const key=JSON.stringify([turn.request_id,turn.state,turn.status,turn.files]);
 if(lastKey===key)return;
 const head=node('h3',turn.query);
 const tag=node('p','Run '+(turn.run??'待派发')+' · '+(labels[turn.state]||turn.state)+(turn.status?' · '+turn.status:''));tag.className='meta';
 box.replaceChildren(head,tag);
 if(!turn.files.includes('研究记录.json')){if(turn.state==='done')box.append(node('p',turn.status==='invalid_local_input'?'执行器读取输入时失败，本轮没有生成成果。':'本轮未产出研究成果。可查看其他轮次或重新发送。'));lastKey=key;return;}
 const data=await req(fileUrl(turn,'研究记录.json',true));
 if(current!==turn.request_id)return;
 const read=data.read||[];
 const count=new Set(read.map(r=>r.path)).size;
 const inventory=data.coverage?.inventory;if(inventory)box.append(node('p','清单共 '+inventory.listed_materials+' 份 · 未读 '+inventory.unread_passages+' 段 · 跳过 '+inventory.skipped+' 份'));
 box.append(node('p','已固定读取 '+count+' 份材料、'+read.length+' 段原文。'));
 for(const claim of data.claims||[]){const el=node('article','');el.className='claim';el.append(node('p',claim.statement));
  for(const c of claim.citations){const row=read.find(r=>r.ref===c.ref);el.append(node('blockquote',c.quote));
   if(row){const original=node('details','');original.append(node('summary','回查原文 · '+(row.heading||row.path)),node('p',row.text),node('small','版本 '+row.revision));el.append(original);}}box.append(el);}
 section(box,'未决问题',data.open_questions);
 section(box,'覆盖范围与限制',data.limits);
 section(box,'已读版本',read.map(r=>r.path+' · '+r.revision));
 section(box,'工具调用',(data.actions||[]).map(a=>typeof a==='string'?a:JSON.stringify(a)));
 const nav=node('div','');nav.className='results-links';
 const canvas=link('查看白板',fileUrl(turn,'研究白板.canvas',true));
 canvas.onclick=async e=>{e.preventDefault();try{const graph=await req(canvas.href);if(current!==turn.request_id)return;
 showCanvas(graph);}catch(err){id('status').textContent=err.message;}};
 nav.append(canvas);
 for(const file of turn.files)nav.append(link('下载'+file,fileUrl(turn,file)));
 if(turn.obsidian)nav.append(link('在 Obsidian 打开',turn.obsidian));
 else if(turn.can_export){const send=node('button','送到 Obsidian 试点库');send.onclick=async()=>{send.disabled=true;
 try{const res=await req(api+'/canvas',{request_id:turn.request_id});send.replaceWith(link('在 Obsidian 打开',res.obsidian));}
 catch(err){id('status').textContent=err.message;send.disabled=false;}};nav.append(send);}
 box.append(nav);lastKey=key;
}
async function refresh(){
 if(busyRead)return;busyRead=true;
 try{const data=await req(api);turns=data.turns;const sel=id('turn');const last=turns.at(-1);
 if(!turns.some(t=>t.request_id===current)||seenLast===current)current=last?.request_id||'';seenLast=last?.request_id||'';
 sel.replaceChildren(...turns.map((t,i)=>new Option('第 '+(i+1)+' 轮 · '+(labels[t.state]||t.state),t.request_id)));sel.value=current;sel.hidden=!turns.length;
 controls();await draw();
 }catch(e){id('status').textContent=e.message;}finally{busyRead=false;}
}
id('turn').onchange=()=>{current=id('turn').value;lastKey='';draw().catch(e=>id('status').textContent=e.message);};
id('question').oninput=()=>{nonce='';};
id('research').onsubmit=async event=>{
 event.preventDefault();if(sending||!id('consent').checked)return;sending=true;controls();nonce ||= crypto.randomUUID().replaceAll('-','');
 try{await req(api,{request_id:nonce,query:id('question').value,parent:latest||null,consent:true});current=nonce;nonce='';id('question').value='';id('consent').checked=false;id('status').textContent='已发送到此工单。';await refresh();}
 catch(e){id('status').textContent=e.message;}finally{sending=false;controls();}
};
(async()=>{try{token=(await req('/api/session')).csrf;await refresh();setInterval(()=>{if(!document.hidden)refresh();},3000);}
catch(e){id('status').textContent=e.message;}})();
