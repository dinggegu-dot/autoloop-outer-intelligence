import {kingdoms} from './kingdoms.js';

// 随发行版提供的绘制素材。私人实例也可以挂载原工作区已有的画册。
const cache=new Map();
const palette=[
  ['#758ca1','#a4b5b1','#324956'],['#8cc4b6','#8aac75','#4d7656'],
  ['#edaa77','#d78058','#954839'],['#9bcddd','#72babe','#526a89'],
  ['#72afb0','#80a075','#315946'],['#a7cbdc','#829bad','#364b61'],
  ['#adcbe3','#c6dfe6','#7494ab'],['#8bcddc','#7accc2','#3e969b'],
  ['#f6afbf','#dfa0a9','#925973'],['#c2adba','#89a78e','#444e60'],
];
function canvas(w,h){const el=document.createElement('canvas');el.width=w;el.height=h;return el.getContext('2d');}
function path(c,pts,color){c.fillStyle=color;c.beginPath();pts.forEach(([x,y],i)=>i?c.lineTo(x,y):c.moveTo(x,y));c.closePath();c.fill();}
function oval(c,x,y,rx,ry,color){c.fillStyle=color;c.beginPath();c.ellipse(x,y,rx,ry,0,0,Math.PI*2);c.fill();}
function tile(c,x,y,w,h,color){c.fillStyle=color;c.fillRect(x,y,w,h);}
function badge(c,i){
  const [sky,mid,ink]=palette[i], gold='#fbd479', ivory='#fff2d3';
  function tower(x,y,w,h){tile(c,x-w/2,y-h,w,h,ivory);path(c,[[x-w*.75,y-h],[x,y-h-24],[x+w*.75,y-h]],ink);tile(c,x-3,y-h+8,6,10,gold);}
  if(i===0){oval(c,60,75,45,10,ink);tile(c,36,37,48,38,ink);oval(c,60,37,24,7,'#6b8792');tile(c,36,63,48,7,gold);}
  if(i===1){path(c,[[12,79],[21,49],[42,46],[46,30],[95,25],[105,77]],ink);tile(c,43,30,45,7,mid);for(let n=0;n<4;n++)tile(c,48+7*n,37,6,39+n%2*5,n%2?ivory:'#9bdad7');}
  if(i===2){for(let n=0;n<5;n++)tile(c,48-n*6,66-n*8,24+n*12,8,n%2?'#dd8858':ink);tile(c,24,30,72,5,gold);}
  if(i===3){tower(27,80,13,34);tower(94,80,13,34);c.strokeStyle=gold;c.lineWidth=9;c.beginPath();c.arc(61,60,24,Math.PI,2*Math.PI);c.stroke();tile(c,33,68,55,6,ivory);}
  if(i===4){for(const [x,y] of [[32,72],[84,65],[64,81]])path(c,[[x-17,y],[x,y-48],[x+17,y]],ink);tile(c,14,61,88,6,'#d7946a');tile(c,21,65,5,16,ivory);tile(c,91,65,5,16,ivory);}
  if(i===5){for(const [x,h] of [[19,27],[40,53],[61,40],[82,28]]){tile(c,x,82-h,19,h,ink);for(let y=86-h;y<78;y+=9){tile(c,x+3,y,4,4,gold);tile(c,x+11,y,4,4,ivory);}}tile(c,49,17,2,12,ivory);}
  if(i===6){for(const [x,y,s] of [[39,79,41],[70,80,56],[93,81,29]]){path(c,[[x-s*.64,y],[x,y-s],[x+s*.64,y]],ink);path(c,[[x-s*.26,y-s*.59],[x,y-s],[x+s*.26,y-s*.59],[x+3,y-s*.68],[x-4,y-s*.54]],ivory);}}
  if(i===7){path(c,[[42,83],[51,35],[68,35],[78,83]],ivory);path(c,[[47,61],[72,61],[74,71],[45,71]],'#d55349');tile(c,47,31,25,9,ink);tile(c,52,32,14,6,gold);path(c,[[42,31],[60,18],[78,31]],'#c75248');}
  if(i===8){oval(c,61,57,35,22,ink);tile(c,24,51,74,7,gold);for(let j=0;j<3;j++)oval(c,47+j*14,47-j%2*8,7,6,j%2?ivory:'#8bc27e');path(c,[[83,51],[94,21],[99,23],[90,52]],ivory);}
  if(i===9){for(let n=0;n<3;n++){const w=55-13*n,y=73-18*n;tile(c,60-w/2,y-11,w,13,n%2?ivory:'#c96850');path(c,[[52-w/2,y-11],[60,y-23],[68+w/2,y-11]],ink);}}
}
function postcard(i,portrait){
  const c=canvas(portrait?160:440,portrait?156:252);
  if(portrait){c.scale(1.26,1.26);c.translate(3,14);badge(c,i);return c.canvas;}
  const [sky,mid,ink]=palette[i];
  const wash=c.createLinearGradient(0,0,0,252);wash.addColorStop(0,sky);wash.addColorStop(1,'#fff2c4');tile(c,0,0,440,252,wash);
  oval(c,328,54,25,25,'#fff1c6');
  for(let band=0;band<3;band++){
    const pts=[[0,252]];
    for(let x=0;x<=460;x+=12){const y=137+band*24+Math.sin(x*.013+i+band*.7)*22+Math.sin(x*.029+i)*10;pts.push([x,y]);}
    pts.push([460,252]);path(c,pts,[mid,ink,mid][band]);
  }
  c.save();c.translate(104,-27);c.scale(2.2,2.2);badge(c,i);c.restore();
  for(let n=0;n<65;n++){const x=n*83%440,y=210+n*31%42;tile(c,x,y,4+n%12,1,'#fff4d880');}
  return c.canvas;
}
function airship(){
  const c=canvas(230,300);
  const shade=c.createLinearGradient(39,0,194,0);shade.addColorStop(0,'#dca85a');shade.addColorStop(.28,'#fff0bc');shade.addColorStop(.55,'#fffbdf');shade.addColorStop(1,'#cca25f');
  oval(c,114,93,78,83,shade);
  c.strokeStyle='#a37848';c.lineWidth=3;
  for(const k of [-.8,0,.8]){c.beginPath();c.moveTo(113,11);c.bezierCurveTo(113+k*98,37,113+k*98,145,113,176);c.stroke();}
  for(const x of [59,86,141,169]){c.beginPath();c.moveTo(x,153);c.lineTo(112+(x-113)*.53,218);c.stroke();}
  oval(c,114,88,16,15,'#d65841');oval(c,114,88,11,10,'#ffe4aa');
  tile(c,105,176,16,29,'#ad8050');oval(c,114,177,20,6,'#d8ad62');
  tile(c,69,209,80,41,'#ca4837');oval(c,109,209,40,10,'#ef7851');
  for(const x of [85,110,135]){oval(c,x,222,8,8,'#f6cd78');oval(c,x,222,5.5,5.5,'#3b778b');}
  path(c,[[43,244],[166,244],[151,264],[75,269]],'#aa4036');tile(c,47,243,120,5,'#f0bd66');path(c,[[151,240],[179,221],[174,245]],'#dd7054');
  return c.canvas;
}
const islands=[[16,-160,29,23],[1,-146,21,18],[27,-113,44,31],[-21,-83,28,31],[-39,-92,27,23],[29,18,39,25],[67,38,44,21],[-9,48,22,18],[13,77,25,23],[14,-42,16,12],[0,150,31,40],[-48,114,39,20]];
function chart(){
  const c=canvas(1024,512),pixels=c.createImageData(1024,512);
  for(let y=0;y<512;y++)for(let x=0;x<1024;x++){
    const lon=x*360/1024-180,lat=90-y*180/512;
    const wave=Math.sin(lon*.16+lat*.13)*.12+Math.sin(lon*.45-lat*.21)*.055+Math.sin(lon*.88+lat*.56)*.026;
    let h=-5,region=0;
    for(let j=0;j<islands.length;j++){const [a,b,w,v]=islands[j],dx=((lon-b+540)%360-180)/w,k=1-dx*dx-((lat-a)/v)**2;if(k>h){h=k;region=j;}}
    h+=wave;
    const ridge=Math.sin(lon*.27+Math.sin(lat*.18)*3)+Math.sin(lat*.37-lon*.08);
    const grain=Math.sin(x*78.233+y*33.413)*5;
    let rgb;
    if(h<-.18){const f=Math.min(1,(-h-.18)*.82);rgb=[54-29*f,171-72*f,184-20*f];}
    else if(h<0)rgb=[106+35*(1+h*5),203,202];
    else if(h<.09)rgb=[238,213,157];
    else if(region===6||Math.abs(lat)>62)rgb=[191+ridge*10,213+ridge*9,218+ridge*8];
    else if(region===2||region===8)rgb=[207+ridge*15,153+ridge*13,98+ridge*12];
    else rgb=[119+ridge*19,157+ridge*15,102+ridge*10];
    const contour=h>0&&Math.abs((h*14)%1-.5)<.075?-9:0;
    const n=(y*1024+x)*4;
    for(let k=0;k<3;k++)pixels.data[n+k]=rgb[k]+grain+contour;
    pixels.data[n+3]=255;
  }
  c.putImageData(pixels,0,0);return c.canvas;
}
export function imageFor(name) {
  if(document.documentElement.dataset.localArt==='on')return '/assets/odyssey/'+name;
  if(cache.has(name))return cache.get(name);
  let img;
  if(name==='world.png')img=chart();
  else if(name==='ship.png'||name==='mario.png'||name==='red-cappy.png')img=airship();
  else if(name==='power-moon.png'){
    const c=canvas(64,64);oval(c,32,32,24,26,'#f7cf63');c.globalCompositeOperation='destination-out';oval(c,41,23,18,20,'#fff');img=c.canvas;
  }else{const i=kingdoms.findIndex(k=>name.startsWith(k.key+'-'));img=postcard(Math.max(0,i),name.includes('-person'));}
  const result=img.toDataURL();cache.set(name,result);return result;
}
