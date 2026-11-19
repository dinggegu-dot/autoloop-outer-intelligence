import {kingdoms} from './kingdoms.js';
import {imageFor} from './travel_art.js';
import {buildGlobe} from './globe.js';

const by=id=>document.getElementById(id);
const destinations=[
  ['目标登记', 'mission', '查看本单的目标与当前版本。'],
  ['待我决定', 'decision', '核对需要本人处理的决定。'],
  ['目标范围', 'scope', '查看交付范围和明确排除的事项。'],
  ['验收条件', 'criteria', '核对验收方法、输入和预期结果。'],
  ['授权预算', 'budget', '核对权限与可用执行时间。'],
  ['执行步骤', 'progress', '查看已登记的实施步骤。'],
  ['逐项验收', 'checks', '核对每一项独立检查的结果。'],
  ['成果交付', 'artifacts', '查看归档文件与下载入口。'],
  ['修改要求', 'revisions', '指定一项原要求进行替换或补充。'],
  ['角色调用', 'outer-history', '展开每次调用的输入、返回和用量。'],
];
const query=new URLSearchParams(location.search);
let current=kingdoms.find(k=>k.key===query.get('kingdom'))||kingdoms[7];
let world,paused=matchMedia('(prefers-reduced-motion: reduce)').matches;
let rows=[],ticket=query.get('trip')||'',chosen=Boolean(query.has('trip')),enabled=false,reading=false;
const title=r=>r.goal_contract?.goal_statement||r.summary||r.id;
const route=by('kingdom-route'),mode=by('journey-mode'),live=by('journey-live'),dialog=by('travel-dialog');
const localArt=document.documentElement.dataset.localArt==='on';
for(const img of document.querySelectorAll('[data-art]'))img.src=imageFor(img.dataset.art);
if(localArt)document.querySelector('.game-footer p').textContent='私人工作台 · 游戏美术 © Nintendo';
else {document.querySelector('.game-brand img').alt='旅行飞艇';document.querySelector('.moon-counter img').alt='金月';}

function board(section='',extra={}){
  const params=new URLSearchParams(extra);
  if(ticket&&!params.has('station')&&!params.has('new'))params.set('goal',ticket);
  return '/workbench'+(params.size?'?'+params:'')+(section?'#'+section:'');
}
function popup(title,text,href){
  dialog.querySelector('h2').textContent=title;dialog.querySelector('p').textContent=text;
  dialog.querySelector('a').href=href;
  if(!dialog.open)dialog.showModal();
}
function go(){
  const [label,section]=destinations[kingdoms.indexOf(current)];
  if(ticket){location.assign(board(section));return;}
  popup(current.name+' · '+label,current.description+'。在地图内容中选择工单，可从这里打开它的'+label+'。',board());
}
function selectStage(id,animate=true){
  const prev=current;
  current=kingdoms.find(k=>k.id===id)||current;
  const i=kingdoms.indexOf(current),row=rows.find(r=>r.id===ticket);
  const scene=by('kingdom-scene'),figure=by('kingdom-character');
  scene.src=imageFor(current.key+'-scene.'+current.ext);scene.alt=current.name+'旅行明信片';
  figure.src=imageFor(current.key+'-person.png');figure.alt=localArt?current.character:current.name+'地标';
  by('kingdom-lore').textContent=current.description;
  by('game-stage-id').textContent='当前目的地 · '+String(i+1).padStart(2,'0')+(row?' · '+current.name:'');
  by('game-stage-title').textContent=row?destinations[i][0]:current.name;
  by('game-stage-description').textContent=row?destinations[i][2]:destinations[i][0];
  by('game-stage-state').textContent=row?'工单 · '+row.journey.label:'王国图鉴';
  by('game-project-state').textContent=row?'工单 v'+row.revision:'自由探索';
  document.querySelector('.postcard-label').textContent=current.english;
  document.querySelector('.postcard-stamp').textContent=String(i+1).padStart(2,'0')+' / 10';
  for(const [j,b] of [...route.children].entries()){
    b.querySelector('strong').textContent=row?destinations[j][0]:kingdoms[j].name;
    b.querySelector('small').textContent=row?kingdoms[j].name:destinations[j][0];
    const on=b.dataset.id===current.id;b.classList.toggle('selected',on);b.setAttribute('aria-pressed',String(on));
    if(on&&prev!==current&&route.scrollWidth>route.clientWidth)centerStop(b);
  }
  if(prev!==current)world?.setStage(current.id,animate);
}
for(const [i,k] of kingdoms.entries()){
  const b=document.createElement('button'),dot=document.createElement('span'),img=document.createElement('img');
  b.dataset.id=k.id;b.className='kingdom-stop';dot.className='stop-dot';img.src=imageFor(k.key+'-person.png');img.alt='';
  const name=document.createElement('strong'),role=document.createElement('small');name.textContent=k.name;role.textContent=destinations[i][0];
  dot.append(img);b.append(dot,name,role);b.onclick=()=>selectStage(k.id);route.append(b);
}
function centerStop(b){
  if(b&&route.scrollWidth>route.clientWidth)route.scrollTo({left:b.offsetLeft-route.clientWidth/2+b.clientWidth/2,behavior:paused?'instant':'smooth'});
}
new ResizeObserver(()=>centerStop(route.querySelector('.selected'))).observe(route);
function step(dir){selectStage(kingdoms[(kingdoms.indexOf(current)+dir+kingdoms.length)%kingdoms.length].id);}
by('game-enter').onclick=go;by('game-prev').onclick=()=>step(-1);by('game-next').onclick=()=>step(1);
for(const button of document.querySelectorAll('[data-view]'))button.onclick=()=>{
  const v=button.dataset.view;
  if(v==='agents'&&!ticket){popup('外层智能角色','选择一张工单可查看 Intake Assessor、Acceptance Designer 和 Goal Coordinator 的实际调用。',board());return;}
  if(v==='approvals')location.assign(board('',{station:'decision'}));
  else location.assign(board(v==='agents'?'outer-history':v==='activity'?'timeline':''));
};
for(const button of document.querySelectorAll('[data-create-task]'))button.onclick=()=>location.assign(board('',{new:'1'}));
by('game-mobile').onclick=()=>popup('本人飞书私聊','在已连接的机器人私聊发送目标。回复原卡可补充或替换要求，批准前可以核对这张工单的范围、验收和预算。',board('revisions'));
function motion(){
  by('kingdom-screen').classList.toggle('motion-paused',paused);
  by('globe-motion').textContent=paused?'▶ 恢复飞行动效':'Ⅱ 暂停飞行动效';
  by('globe-motion').setAttribute('aria-pressed',String(paused));
}
by('globe-motion').onclick=()=>{paused=!paused;motion();};motion();
function inspect(value){
  by('globe-zoom').setAttribute('aria-pressed',String(value));
  by('globe-zoom').textContent=value?'↶ 返回全景':'⌕ 近看王国';
  by('kingdom-screen').classList.toggle('inspecting',value);
  world?.inspect?.(value);
}
by('globe-zoom').onclick=()=>inspect(by('globe-zoom').getAttribute('aria-pressed')!=='true');
by('globe-reset').onclick=()=>{inspect(false);world?.reset();};
document.addEventListener('keydown',e=>{
  if(dialog.open||e.altKey||e.ctrlKey||e.metaKey||/INPUT|SELECT|TEXTAREA/.test(e.target.tagName))return;
  if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();step(e.key==='ArrowLeft'?-1:1);}
  if(e.key.toLowerCase()==='a')go();
  if(e.key.toLowerCase()==='b')location.assign(board());
  if(e.key.toLowerCase()==='y')location.assign(board('timeline'));
});
function renderLive(){
  const row=rows.find(r=>r.id===ticket);
  live.replaceChildren();live.hidden=!row;
  if(row){
    const copy=document.createElement('div');copy.className='live-copy';
    const label=document.createElement('span');label.className='live-eyebrow';label.textContent='当前工单 · '+row.journey.label;
    const h=document.createElement('h3');h.textContent=title(row);const p=document.createElement('p');p.textContent=row.journey.instruction;
    const a=document.createElement('a');a.href=board(({decision:'decision',delivery:'artifacts',flight:'progress',dock:'decision'})[row.journey.station]);a.textContent=row.journey.station==='decision'?'去决定 →':'查看工单 →';
    copy.append(label,h,p);live.append(copy,a);
  }
  selectStage(current.id,false);
}
mode.onchange=()=>{chosen=true;ticket=mode.value;const url=new URL(location.href);url.searchParams.set('trip',ticket);history.replaceState(null,'',url);renderLive();};
async function sync(){
  if(reading)return;reading=true;
  try{
    const session=await fetch('/api/session',{cache:'no-store'});if(!session.ok)throw Error();
    const response=await fetch('/api/outer',{cache:'no-store'});if(!response.ok)throw Error();
    const data=await response.json();enabled=data.enabled;rows=data.drafts||[];
    if(!chosen){ticket=(rows.find(r=>r.journey.station==='decision')||rows[0])?.id||'';chosen=true;}
    const options=[new Option('王国图鉴',''),...rows.map(r=>new Option(title(r),r.id))];
    if(ticket&&!rows.some(r=>r.id===ticket))options.push(new Option('指定工单暂不可用',ticket));
    if([...mode.options].map(o=>o.value+o.text).join()!==options.map(o=>o.value+o.text).join())mode.replaceChildren(...options);
    mode.value=ticket;
    by('game-open').textContent=enabled?rows.filter(r=>r.journey.station==='decision').length:'—';
    by('game-moons').textContent=enabled?String(rows.filter(r=>r.journey.station==='delivery').length).padStart(2,'0'):'—';
    by('game-total').textContent=enabled?'/ '+rows.length:'';
    by('game-connection').textContent=enabled?'已读取工单':'王国图鉴';
    renderLive();
  }catch{by('game-connection').textContent='工单状态待核对';}
  finally{reading=false;}
}
by('game-moon-caption').textContent='已交付工单';
selectStage(current.id,false);sync();
setInterval(()=>{if(!document.hidden)sync();},8000);
const setStatus=text=>{by('globe-status').textContent=text;};
try {world=buildGlobe({host:by('globe-canvas'),selectedStage:()=>current.id,selectStage,motionPaused:()=>paused,imageFor,setStatus});}
catch {
  const disc=document.createElement('div');disc.className='fallback-globe';disc.style.backgroundImage='url('+imageFor('world.png')+')';by('globe-canvas').append(disc);
  world={setStage:id=>{disc.style.backgroundPositionX=(kingdoms.findIndex(k=>k.id===id)*10)+'%';},reset:()=>world.setStage(current.id)};
  world.reset();for(const id of ['globe-zoom','globe-motion','globe-reset'])by(id).hidden=true;
  setStatus('平面航图 · 下方切换王国');
}
