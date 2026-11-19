// 以每站的切平面为地面。相同几何体合并实例，十座微缩王国共用材质。
export function addKingdomRelief(THREE, planet, anchors) {
  const shapes = {
    block: new THREE.BoxGeometry(1, 1, 1),
    drum: new THREE.CylinderGeometry(.5, .5, 1, 24),
    terrace: new THREE.CylinderGeometry(.5, .43, 1, 8),
    spire: new THREE.ConeGeometry(.5, 1, 5),
    ball: new THREE.SphereGeometry(.5, 16, 12),
    rock: new THREE.IcosahedronGeometry(.5, 0),
    ring: new THREE.TorusGeometry(.43, .068, 8, 24),
  };
  const material = new THREE.MeshStandardMaterial({roughness:.77, metalness:.06});
  const batches = new Map();
  const frame = new THREE.Object3D(), piece = new THREE.Object3D();
  const up = new THREE.Vector3(0,1,0);
  const cream='#fff0cb', brass='#efb945', dark='#354859', scarlet='#db4941';
  const turf=['#78959b','#92bc78','#e6a257','#74c2c3','#619978','#839ca4','#cde4e6','#78c9ba','#e6a5b2','#749777'];
  anchors.forEach((anchor, i) => {
    frame.position.copy(anchor).multiplyScalar(.992);
    frame.quaternion.setFromUnitVectors(up, anchor.clone().normalize());
    frame.rotateY(i * .59);
    frame.updateMatrix();
    function put(shape, color, x,y,z, w,h,d, angle=0, tilt=0) {
      piece.position.set(x,y,z);
      piece.scale.set(w,h,d);
      piece.rotation.set(tilt,angle,0);
      piece.updateMatrix();
      if (!batches.has(shape)) batches.set(shape,[]);
      batches.get(shape).push([new THREE.Matrix4().multiplyMatrices(frame.matrix,piece.matrix), color]);
    }
    function tree(x,z,size,snow=false) {
      put('drum','#815d47',x,.18,z,.05,.22,.05);
      put('spire',snow?'#799d9b':'#237263',x,.27,z,size,.34,size,.4);
      put('spire',snow?'#f0fbff':'#59a96f',x,.4,z,size*.73,.27,size*.73,.4);
    }
    function tower(x,z,height,color,roof) {
      put('drum',color,x,.12+height/2,z,.23,height,.23);
      put('drum',brass,x,height+.1,z,.28,.06,.28);
      put('spire',roof,x,height+.24,z,.34,.26,.34);
    }
    put('terrace','#86705c',0,.013,0,.86,.17,.69);
    put('terrace','#d7ba8c',0,.082,0,.9,.086,.72);
    put('terrace',turf[i],0,.135,0,.92,.065,.74);
    // 海岸碎石、台阶和两侧地貌把建筑与球面接起来，保留航标的点击空间。
    for(let n=0;n<11;n++){
      const angle=n*2.39996+i, size=.06+(n%4)*.022;
      put('rock',n%3?turf[i]:'#d4ba92',Math.cos(angle)*(.46+n%2*.065),.028,Math.sin(angle)*.37,size*1.5,size,size,angle);
    }
    for(let n=0;n<4;n++)put('block',n%2?'#d6bb92':'#b49c79',.03,.13-n*.026,.33+n*.038,.135,.035,.05);
    if([1,4,6,7].includes(i)){
      for(const [x,z] of [[-.48,-.17],[-.39,-.36],[.38,-.28]])tree(x,z,i===6?.19:.23,i===6);
    }
    if (i===0) { // 帽塔、金色帽带和路灯
      put('drum',dark,-.05,.21,0,.57,.09,.47);
      put('drum','#586878',-.05,.42,0,.36,.4,.33);
      put('drum',brass,-.05,.28,0,.37,.08,.34);
      for (const x of [-.31,.3]) {put('drum',dark,x,.25,.15,.035,.22,.035);put('ball',cream,x,.38,.15,.09,.1,.09);}
    } else if (i===1) { // 台地瀑布、落水与浪花
      put('terrace','#957756',-.12,.33,-.06,.62,.39,.43);
      put('terrace','#86b85e',-.12,.54,-.06,.64,.065,.44);
      for(let j=0;j<5;j++){const x=-.24+j*.062;put('block',j%2?'#8ee4e3':'#c9ffff',x,.34,.145,.061,.42,.065);put('ball','#e8ffff',x,.18,.2,.12,.08,.1);}
      tree(.26,-.08,.24);
    } else if (i===2) { // 倒金字塔、层阶和仙人掌
      for(let j=0;j<4;j++)put('block',j%2?'#ec9956':'#b75e48',-.09,.29+j*.082,0,.18+j*.11,.084,.18+j*.11,.64);
      for(const x of [.24,-.31]){put('drum','#469b76',x,.28,.14,.063,.27,.063);put('ball','#eb6476',x,.43,.14,.075,.065,.075);}
    } else if (i===3) { // 湖庭石柱和金环
      for(const x of [-.23,.23]){put('drum',cream,x,.34,0,.12,.38,.12);put('drum','#a486bd',x,.54,0,.17,.07,.17);}
      put('ring',brass,0,.48,0,.61,.61,.61);put('terrace','#89ddec',0,.18,.19,.39,.03,.23);
    } else if (i===4) { // 铁桥跨过松林
      for(const [x,z] of [[-.29,0],[.29,-.06],[.14,.17],[-.12,-.13]]) tree(x,z,.27);
      for(const x of [-.20,.20])put('block','#b45544',x,.32,.07,.06,.35,.07);
      put('block',scarlet,0,.5,.07,.56,.07,.14);
      for(let j=0;j<5;j++)put('block',brass,-.23+j*.115,.57,.13,.025,.12,.025);
    } else if (i===5) { // 退台高楼与夜窗
      for(const [x,z,h] of [[-.24,.08,.38],[-.05,-.08,.75],[.18,.05,.51],[.28,-.12,.31]]){
        put('block',dark,x,.16+h/2,z,.17,h,.16);
        put('block','#8297a3',x,.18+h,z,.12,.09,.11);
        for(let floor=0;floor<Math.floor(h/.085);floor++){
          const y=.205+floor*.084;
          for(const dx of [-.049,0,.049]){
            const color=(floor+Math.round(dx*100))%3?'#f4d79a':'#82acb9';
            for(const face of [-1,1]){
              put('block',color,x+dx,y,z+face*.082,.025,.041,.009);
              put('block',color,x+face*.087,y,z+dx,.009,.041,.025);
            }
          }
        }
        for(const side of [-1,1])for(const face of [-1,1])put('block','#72838c',x+side*.083,.16+h/2,z+face*.076,.012,h,.012);
        put('block','#9babae',x,.13+h,z,.183,.024,.172);
      }
      put('drum',cream,-.05,1.03,-.08,.023,.22,.023);
    } else if (i===6) {
      for(const [x,z,s] of [[-.19,-.04,.5],[.05,-.14,.67],[.27,0,.37]]){
        put('spire','#769bb4',x,.15+s/2,z,s*.74,s,s*.74);
        put('spire','#f6fbff',x,.15+s*.81,z,s*.31,s*.40,s*.31);
      }
      put('ball','#e4f5f8',-.07,.18,.18,.24,.21,.22);put('ball','#63859a',-.07,.17,.27,.11,.1,.04);
    } else if (i===7) {
      put('drum',cream,-.10,.39,-.06,.23,.46,.23);
      put('drum',scarlet,-.10,.36,-.06,.236,.10,.236);
      put('block','#8e6957',.17,.2,.16,.37,.07,.12);
      for(const x of [.05,.29])put('drum',brass,x,.16,.19,.04,.18,.04);
      for(let j=0;j<3;j++)put('rock','#f3b585',.2+j*.07,.19,-.13+j*.04,.095,.14,.08);
    } else if (i===8) {
      put('spire','#d78391',-.13,.3,-.06,.61,.43,.52);
      put('ball','#744762',.08,.37,.08,.42,.27,.35);
      put('drum','#ffad79',.08,.49,.08,.4,.035,.33);
      for(let j=0;j<3;j++)put('rock',j%2?'#73ab69':'#fff0bb',-.03+j*.08,.53,.08,.085,.073,.075);
      put('block',cream,.22,.63,.1,.037,.29,.04,-.3,.3);
    } else {
      for(let j=0;j<3;j++){
        const s=1-j*.21;put('block',j%2?cream:scarlet,0,.26+j*.17,0,.38*s,.18,.32*s);
        put('terrace',dark,0,.37+j*.17,0,.57*s,.10,.49*s,Math.PI/8);
      }
      for(const x of [-.29,.29]){put('drum',scarlet,x,.26,.16,.04,.22,.04);put('ball',brass,x,.39,.16,.08,.09,.08);}
    }
    function pillar(x,z,height,color){put('drum',color,x,.16+height/2,z,.021,height,.021);}
    function flag(x,z,h,color){pillar(x,z,h,cream);put('block',color,x+.047,.16+h-.035,z,.1,.057,.013);}
    function windowsAround(x,z,r,y,count){
      for(let n=0;n<count;n++){const a=n*Math.PI*2/count;
        put('block',dark,x+Math.sin(a)*r,y,z+Math.cos(a)*r,.042,.068,.012,a);
        put('block','#ffe2a1',x+Math.sin(a)*(r+.008),y+.006,z+Math.cos(a)*(r+.008),.026,.041,.01,a);
      }
    }
    if(i===7){
      // 玻璃灯室、环形检修道、黄铜窗框和木板码头。
      put('drum',dark,-.10,.62,-.06,.31,.034,.31);
      put('drum','#6dbfc3',-.10,.67,-.06,.186,.096,.186);
      put('drum',brass,-.10,.726,-.06,.25,.018,.25);
      for(let n=0;n<8;n++){const a=n*Math.PI/4;
        put('drum',cream,-.1+Math.cos(a)*.102,.671,-.06+Math.sin(a)*.102,.012,.10,.012);
        put('drum',dark,-.1+Math.cos(a)*.146,.641,-.06+Math.sin(a)*.146,.009,.048,.009);
      }
      put('spire',scarlet,-.10,.785,-.06,.30,.12,.30);put('ball',brass,-.1,.855,-.06,.034,.034,.034);
      windowsAround(-.10,-.06,.12,.40,5);
      for(let n=0;n<8;n++)put('block',n%2?'#be906a':'#9c7058',.015+n*.041,.245,.16,.038,.018,.14);
      for(const x of [.04,.28]){pillar(x,.24,.16,'#725740');put('ball',cream,x,.337,.24,.039,.039,.039);}
      put('ball',scarlet,.24,.17,-.16,.12,.10,.12);put('drum',cream,.24,.18,-.16,.12,.025,.12);
    }else if(i===1){
      for(let n=0;n<7;n++){const x=-.25+n*.044;put('block','#e6fff2',x,.38,.182,.012,.23+(n%3)*.048,.008);put('ball','#faffed',x,.18,.24,.05,.033,.05);}
      for(let n=0;n<4;n++)put('rock','#9ebd75',-.3+n*.12,.56,-.13,.11,.08,.07);
      flag(.21,.16,.24,scarlet);
    }else if(i===2){
      for(let n=0;n<8;n++)put('block',brass,-.24+n*.055,.536,.03,.024,.029,.023,.64);
      for(let n=0;n<3;n++){const x=.05+n*.085;put('block',['#e09a84','#6db3b0','#f3ba76'][n],x,.214,.22,.083,.10,.08);put('block',cream,x,.271,.22,.095,.018,.09);}
    }else if(i===5){
      for(const x of [-.22,.14]){put('block','#a84b40',x,.19,.19,.17,.055,.09);put('block',cream,x,.224,.19,.18,.014,.1);}
      for(let n=0;n<5;n++)put('block',cream,-.06+n*.034,.176,.21,.021,.008,.09);
      flag(.25,-.1,.39,scarlet);
    }else if(i===6){
      for(let n=0;n<6;n++){const x=-.3+n*.043;put('rock','#b4e4e6',x,.21,.13,.043,.13+n%2*.08,.037);}
      put('ring','#d6ae75',.18,.235,.21,.115,.115,.115);flag(-.27,.14,.23,'#db6859');
    }else if(i===0){
      windowsAround(-.05,0,.182,.42,7);put('drum',cream,-.05,.63,0,.37,.016,.33);flag(.28,-.12,.31,'#cb5c54');
    }else if(i===3){
      for(let n=0;n<5;n++){const a=n*Math.PI/4;put('ball',cream,Math.cos(a)*.26,.41+Math.sin(a)*.26,0,.04,.045,.04);}
      for(let n=0;n<4;n++)put('rock','#bb98ad',-.27+n*.038,.21,.19,.053,.10,.045);
    }else if(i===4){
      put('block',dark,0,.63,.13,.56,.018,.025);
      for(let n=0;n<5;n++)put('ball','#f1a985',-.22+n*.105,.18,.24,.045,.058,.045);
    }else if(i===8){
      for(const x of [-.13,.27])put('ring',brass,x,.41,.08,.14,.14,.14);
      for(let n=0;n<4;n++)put('rock','#f4ca88',-.29+n*.04,.19,.15,.044,.075,.04);
    }else{
      for(const x of [-.12,0,.12])put('block',brass,x,.26,.166,.028,.13,.015);
      for(let n=0;n<5;n++)put('block',cream,-.13+n*.065,.44,.142,.036,.066,.012);
      put('drum',brass,0,.73,0,.03,.1,.03);
    }
  });
  for (const [shape, rows] of batches) {
    const mesh = new THREE.InstancedMesh(shapes[shape], material, rows.length);
    rows.forEach(([matrix,color],i)=>{mesh.setMatrixAt(i,matrix);mesh.setColorAt(i,new THREE.Color(color));});
    mesh.computeBoundingSphere();
    planet.add(mesh);
  }
}
