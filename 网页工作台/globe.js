import * as THREE from './vendor/three.module.js';
import {kingdoms} from './kingdoms.js';
import {addKingdomRelief} from './kingdom_relief.js';

export function buildGlobe({host, selectedStage, selectStage, motionPaused, imageFor, setStatus}) {
  const ids=kingdoms.map(k=>k.id);
  const renderer=new THREE.WebGLRenderer({antialias:true,alpha:true,powerPreference:'high-performance'});renderer.setPixelRatio(Math.min(devicePixelRatio,1.7));renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.05;host.append(renderer.domElement);
  const scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(40,1,.1,160);camera.position.set(0,0,19);
  scene.add(new THREE.AmbientLight('#c7dffc',1.48));
  const sun=new THREE.DirectionalLight('#fff1d9',2.86);sun.position.set(-8,10,15);scene.add(sun);const rimLight=new THREE.DirectionalLight('#75c9ff',2);rimLight.position.set(7,2,-5);scene.add(rimLight);
  const planet=new THREE.Group();scene.add(planet);const R=5.0;
  const material=new THREE.MeshStandardMaterial({color:'#9cd4ed',roughness:.85,metalness:.05});
  const globe=new THREE.Mesh(new THREE.SphereGeometry(R,128,96),material);planet.add(globe);
  new THREE.TextureLoader().load(imageFor('world.png'),texture=>{texture.colorSpace=THREE.SRGBColorSpace;texture.anisotropy=Math.min(8,renderer.capabilities.getMaxAnisotropy());material.map=texture;material.bumpMap=texture;material.bumpScale=.035;material.color.set('#ffffff');material.needsUpdate=true;setStatus('拖动探索星球 · 选择目的地查看真实进度');},undefined,()=>{setStatus('地图纹理未加载 · 可继续选择阶段');});
  const atmosphere=new THREE.Mesh(new THREE.SphereGeometry(R*1.025,64,48),new THREE.ShaderMaterial({vertexShader:'varying vec3 n; varying vec3 v; void main(){ vec4 p=modelViewMatrix*vec4(position,1.0); n=normalize(normalMatrix*normal); v=normalize(-p.xyz); gl_Position=projectionMatrix*p; }',fragmentShader:'varying vec3 n; varying vec3 v; void main(){ float f=pow(1.0-abs(dot(normalize(n),normalize(v))),3.2); gl_FragColor=vec4(0.15,0.64,1.0,f*0.64); }',transparent:true,depthWrite:false,side:THREE.FrontSide,blending:THREE.AdditiveBlending}));planet.add(atmosphere);
  // 星球上的目的地坐标，任务阶段只由账本决定，空间位置用于旅行导航。
  const coordinates=[[16,-160],[4,-150],[23,-122],[-19,-84],[-35,-87],[27,22],[69,34],[-8,45],[15,71],[13,-43]];
  function vector(lat,lon,r=R+.09){const p=THREE.MathUtils.degToRad(90-lat),t=THREE.MathUtils.degToRad(lon+180);return new THREE.Vector3(-r*Math.sin(p)*Math.cos(t),r*Math.cos(p),r*Math.sin(p)*Math.sin(t));}
  const anchors=coordinates.map(p=>vector(...p));
  addKingdomRelief(THREE,planet,anchors);
  const gold=new THREE.MeshStandardMaterial({color:'#ffe49b',emissive:'#b8842b',emissiveIntensity:.18,roughness:.5});
  const pendingMaterial=new THREE.MeshStandardMaterial({color:'#778ba0'});
  const white=new THREE.MeshStandardMaterial({color:'#fff9df',roughness:.55});
  const markers=[],picks=[],loader=new THREE.TextureLoader();
  anchors.forEach((v,i)=>{const g=new THREE.Group();g.position.copy(v);g.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),v.clone().normalize());planet.add(g);const m=new THREE.Mesh(new THREE.SphereGeometry(.09,16,12),gold);m.position.y=.05;m.userData.id=ids[i];g.add(m);picks.push(m);const halo=new THREE.Mesh(new THREE.TorusGeometry(.17,.018,8,36),white);halo.rotation.x=-Math.PI/2;halo.position.y=.08;g.add(halo);const portraitMaterial=new THREE.SpriteMaterial({transparent:true,opacity:0,depthWrite:false});
    const portrait=new THREE.Sprite(portraitMaterial);portrait.position.copy(v.clone().normalize().multiplyScalar(R+.98));portrait.scale.set(.65,.95,1);portrait.userData.id=ids[i];planet.add(portrait);picks.push(portrait);
    loader.load(imageFor(kingdoms[i].key+'-person.png'),texture=>{texture.colorSpace=THREE.SRGBColorSpace;portraitMaterial.opacity=1;portraitMaterial.map=texture;portraitMaterial.needsUpdate=true;portrait.userData.ratio=texture.image.width/texture.image.height;const size=ids[i]===selectedStage()?.83:.48;portrait.scale.set(size*portrait.userData.ratio,size,1);});
    markers.push({g,m,halo,portrait});});
  for(let i=0;i<anchors.length-1;i++){const a=anchors[i].clone().normalize(),b=anchors[i+1].clone().normalize(),angle=a.angleTo(b),pts=[];for(let j=0;j<=72;j++){const t=j/72,v=a.clone().multiplyScalar(Math.sin((1-t)*angle)).add(b.clone().multiplyScalar(Math.sin(t*angle))).divideScalar(Math.sin(angle));pts.push(v.multiplyScalar(R+.08));}const line=new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts),72,.011,6,false),gold);planet.add(line);}
  const targetQ=new THREE.Quaternion();let turning=true, approach=0, inspecting=false, closeBlend=0, distance=19;
  const far=new THREE.Vector3(),near=new THREE.Vector3(),look=new THREE.Vector3(),focus=new THREE.Vector3();
  const viewingDirection=new THREE.Vector3(.22,.32,.92).normalize();
  const cameraOffset=new THREE.Vector3(.32,-2.8,2.55);
  function setStage(id,animate=true){const i=ids.indexOf(id);if(i<0)return;approach=animate&&!motionPaused()?1:0;targetQ.setFromUnitVectors(anchors[i].clone().normalize(),new THREE.Vector3(.22,.32,.92).normalize());turning=true;if(motionPaused()||!animate)planet.quaternion.copy(targetQ);markers.forEach((m,j)=>{m.m.material=j===i?red:gold;m.halo.scale.setScalar(j===i?1.7:1);m.m.scale.setScalar(j===i?1.6:1);const size=j===i?.83:.48;m.portrait.scale.set(size*(m.portrait.userData.ratio||.7),size,1);});}
  const red=new THREE.MeshStandardMaterial({color:'#ec3f36',roughness:.37,metalness:.03});
  const shipMaterial=new THREE.SpriteMaterial({transparent:true,depthWrite:false});
  const ship=new THREE.Sprite(shipMaterial);scene.add(ship);
  let shipRatio=.76;
  loader.load(imageFor('ship.png'),texture=>{texture.colorSpace=THREE.SRGBColorSpace;shipMaterial.map=texture;shipMaterial.needsUpdate=true;shipRatio=texture.image.width/texture.image.height;});
  // 远景星点使用单个 Points draw call，保持舞台干净。
  const starPositions=[];let seed=42;function random(){seed=(seed*1664525+1013904223)>>>0;return seed/4294967296;}
  for(let i=0;i<170;i++)starPositions.push((random()-.5)*100,(random()-.5)*65,-25-random()*25);
  const starGeo=new THREE.BufferGeometry();starGeo.setAttribute('position',new THREE.Float32BufferAttribute(starPositions,3));scene.add(new THREE.Points(starGeo,new THREE.PointsMaterial({color:'#9cbdd6',size:.055,transparent:true,opacity:.6,depthWrite:false})));
  const ray=new THREE.Raycaster(),pointer=new THREE.Vector2();let drag=null;
  renderer.domElement.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,moved:false};renderer.domElement.setPointerCapture(e.pointerId);host.focus({preventScroll:true});});
  renderer.domElement.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)>5)drag.moved=true;if(drag.moved){turning=false;const q=new THREE.Quaternion().setFromEuler(new THREE.Euler(dy*.004,dx*.004,0));planet.quaternion.premultiply(q);}drag.x=e.clientX;drag.y=e.clientY;});
  renderer.domElement.addEventListener('pointerup',e=>{if(drag&&!drag.moved){const r=renderer.domElement.getBoundingClientRect();pointer.set((e.clientX-r.left)/r.width*2-1,-(e.clientY-r.top)/r.height*2+1);ray.setFromCamera(pointer,camera);const hits=ray.intersectObjects([globe,...picks]);if(hits[0]?.object.userData.id)selectStage(hits[0].object.userData.id);}drag=null;});
  renderer.domElement.addEventListener('pointercancel',()=>drag=null);renderer.domElement.addEventListener('lostpointercapture',()=>drag=null);
  renderer.domElement.addEventListener('wheel',e=>{e.preventDefault();if(!inspecting)distance=THREE.MathUtils.clamp(distance+e.deltaY*.012,12,26);},{passive:false});
  function resize(){const w=host.clientWidth,h=host.clientHeight;if(!w||!h)return;renderer.setSize(w,h,false);camera.aspect=w/h;camera.zoom=w<600?.86:1;camera.updateProjectionMatrix();if(w<600){planet.position.set(0,1.45,0);planet.scale.setScalar(.69);distance=23;}else if(w<1000){planet.position.set(2.6,.7,0);planet.scale.setScalar(.91);}else{planet.position.set(2.6,.9,0);planet.scale.setScalar(1);}}
  new ResizeObserver(resize).observe(host);resize();
  const stateObserver=new MutationObserver(()=>{if(document.body.classList.contains('game-view'))start();else stop();});stateObserver.observe(document.body,{attributes:true,attributeFilter:['class']});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else start();});
  let frame=0,last=0,t=0,lost=false;
  renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();lost=true;stop();setStatus('3D 连接中断 · 下方阶段菜单仍可使用');});
  function start(){if(!frame&&!document.hidden&&document.body.classList.contains('game-view')&&!lost)frame=requestAnimationFrame(animate);}
  function stop(){cancelAnimationFrame(frame);frame=0;last=0;}
  function animate(now){frame=0;if(document.hidden||!document.body.classList.contains('game-view')||lost)return;const dt=Math.min((now-(last||now))/1000,.05);last=now;if(!motionPaused()){t+=dt;approach=Math.max(0,approach-dt*.65);}
    if(turning){if(motionPaused())planet.quaternion.copy(targetQ);else planet.quaternion.slerp(targetQ,1-Math.exp(-dt*3.8));}
    const scale=planet.scale.x,bank=motionPaused()?0:Math.sin(approach*Math.PI);ship.position.set(-2.9-.45*bank,.3+Math.sin(t*.8)*.12+.27*bank,4.9).multiplyScalar(scale).add(planet.position);ship.scale.set(3.5*shipRatio*scale,3.5*scale,1);shipMaterial.rotation=Math.sin(t*.7)*.025+bank*.09;
    const blendStep=1-Math.exp(-dt*5);
    closeBlend=motionPaused()?Number(inspecting):THREE.MathUtils.lerp(closeBlend,Number(inspecting),blendStep);
    far.set(0,0,distance);
    focus.copy(viewingDirection).multiplyScalar((R+.37)*scale).add(planet.position);
    near.copy(cameraOffset).multiplyScalar(scale*(host.clientWidth<600?1.32:1)).add(focus);
    camera.position.lerpVectors(far,near,closeBlend);
    look.copy(focus);
    if(host.clientWidth>=600)look.x-=Math.min(1.15,camera.aspect*.65)*scale;
    look.multiplyScalar(closeBlend);camera.lookAt(look);
    ship.visible=closeBlend<.65;shipMaterial.opacity=1-Math.min(1,closeBlend/.65);
    markers.forEach(m=>{m.portrait.material.opacity=m.portrait.material.map?Math.max(0,1-1.8*closeBlend):0;});
    renderer.render(scene,camera);frame=requestAnimationFrame(animate);
  }
  setStage(selectedStage(),false);start();
  return {inspect(value){inspecting=value;setStage(selectedStage(),true);},setStage,setStates(states){markers.forEach((m,i)=>{m.g.visible=m.portrait.visible=states.some(s=>s.id===ids[i]);});states.forEach(s=>{const i=ids.indexOf(s.id);if(i<0)return;markers[i].halo.material=s.state==='pending'?pendingMaterial:white;});},reset(){inspecting=false;distance=host.clientWidth<600?23:19;setStage(selectedStage(),true);}};
}
