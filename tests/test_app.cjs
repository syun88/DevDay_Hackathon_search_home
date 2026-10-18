const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const core=require('../property_core.js');
const publicHousing=require('../public_housing.js');
const walking=require('../walking.js');
const script=fs.readFileSync(path.join(__dirname,'..','index.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

class Element {
 constructor(){this.children=[];this.style={};this.value='';this.checked=false;this.dataset={};this.listeners={};this.classList={add(){},remove(){},toggle(){}};this.isConnected=true;this._text='';}
 append(...nodes){this.children.push(...nodes);}
 prepend(...nodes){this.children.unshift(...nodes);}
 replaceChildren(...nodes){this.children=nodes;this._text='';}
 get childNodes(){return this.children;}
 get textContent(){return this._text+this.children.map(node=>node.textContent).join('');}
 set textContent(value){this._text=String(value);this.children=[];}
 addEventListener(name,action){(this.listeners[name]??=[]).push(action);}
 setAttribute(){}
 querySelector(){return new Element();}
 getBoundingClientRect(){return {left:390,top:0,width:900,height:700};}
 setPointerCapture(){}
 closest(){return null;}
 focus(){}
 scrollIntoView(){}
 click(){this.onclick?.();}
}

function app({rangeMode='distance',geolocation,secureContext=true}={}){
 const elements=new Map(),frames=[],requests=[];
 const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 for(const id of ['insideOnly','listingOnlyWithin','publicSource_tokyo','publicSource_tsubata','publicSource_permitted','publicLayer_inventory','publicLayer_vacancy','publicLayer_recruitment','publicLayer_municipal'])element(id).checked=true;
 element('publicProvider').value='tokyo';element('publicArea').value='千代田区';
 element('commuteMode').value='foot';
 const storage=new Map();
 const context={
  window:{isSecureContext:secureContext,propertyCore:core,publicHousing,homeWalking:walking,homeI18n:{t:(text,values=[])=>String(text).replace(/\{(\d+)\}/g,(_,index)=>values[index]??''),setText:(node,render)=>{node.textContent=render();}},addEventListener(){}},
  document:{getElementById:element,createElement:()=>new Element(),createDocumentFragment:()=>new Element(),querySelectorAll:()=>[],addEventListener(){}},
  // Existing radius/filter tests start with the straight-line preference.
  localStorage:{getItem:key=>storage.get(key)||(key==='property_map_v2'?JSON.stringify({rangeMode}):null),setItem:(key,value)=>storage.set(key,value)},
  navigator:{geolocation},location:{protocol:'http:'},requestAnimationFrame:callback=>frames.push(callback),setInterval(){},setTimeout:()=>1,clearTimeout(){},
  URL,URLSearchParams,AbortSignal,AbortController,confirm:()=>true,
  fetch:async(url,options)=>{
   requests.push({url,options});
   let body={listings_enabled:true,has_brave_key:false,decisions_enabled:false,feed_enabled:false,provider:'Bing RSS'};
   if(url==='/api/import')body={results:context.records,rejected:0,provider:'feed'};
   if(url==='/api/route')body={status:'ok',duration_seconds:600,distance_meters:600,checked_at:new Date().toISOString(),source:'TEST ONLY route'};
   if(url.startsWith('/api/listings?'))body={results:[],discovery_links:[{url:'https://suumo.jp/test-only',source:'suumo.jp'}],provider:'bing'};
   if(url.startsWith('/api/public-housing?'))body={records:context.publicRecords,municipalities:['千代田区'],total:context.publicRecords.length,ended_excluded:0,next_offset:null};
   if(url.startsWith('/api/walking-area?')){
    const params=new URLSearchParams(url.split('?')[1]);
    body={metric:params.has('distance_km')?'distance':'time',
     geometry:{type:'Polygon',coordinates:[[[139.75,35.67],[139.79,35.67],[139.79,35.70],[139.75,35.70],[139.75,35.67]]]},
     network:{type:'MultiLineString',coordinates:[[[139.767,35.681],[139.768,35.681]]]},
     ...(params.has('distance_km')?{distance_km:Number(params.get('distance_km'))}:{minutes:Number(params.get('minutes'))})};
   }
   if(url==='/api/public-geocode')body={lat:35.681236,lon:139.767125,geo_precision:'approximate'};
   return {ok:true,json:async()=>body};
  },records:[],publicRecords:[],
 };
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'..','public_housing.js'),'utf8'),context);
 vm.runInNewContext(script,context);
 function flush(){while(frames.length)frames.shift()();}
 async function importRecord(overrides={}){
  context.records=[{title:'TEST ONLY, not a real advertisement',description:'TEST ONLY',url:'https://example.com/test-only',source:'Test source',kind:'rent',lat:35.681236,lon:139.767125,geo_precision:'exact',availability_status:'confirmed',last_verified_at:new Date().toISOString(),extracted:{price:7,management_fee_yen:5000,mandatory_fee_yen:0,layout:'1K',area:25,age:5},...overrides}];
  element('feedPermitted').checked=true;
  const target=element('feedFile');target.files=[{size:10,name:'test.json',text:async()=>'[]'}];
  await target.onchange({target});flush();
 }
 function allNodes(root){return [root,...root.children.flatMap(allNodes)];}
 async function settleWalking(){
  for(let count=0;count<200;count++){
   flush();
   if(!element('walkingStatus').textContent.includes('取得中'))return;
   await new Promise(resolve=>setTimeout(resolve,5));
  }
  assert.fail('Walking range did not settle');
 }
 return {element,context,requests,storage,flush,importRecord,allNodes,settleWalking};
}

test('walking distance sends kilometres, preserves independent settings and switches back to time',async()=>{
 const harness=app({rangeMode:'walk-distance'});await harness.settleWalking();
 const requests=()=>harness.requests.filter(request=>request.url.startsWith('/api/walking-area?'));
 let query=new URLSearchParams(requests().at(-1).url.split('?')[1]);
 assert.equal(query.get('distance_km'),'1');assert.equal(query.has('minutes'),false);
 assert.equal(harness.element('radiusLabelA').textContent,'徒歩1km');
 assert.equal(Number(harness.element('radiusA').min),0.1);
 harness.element('radiusA').oninput({target:{value:'0.8'}});await harness.settleWalking();
 assert.equal(harness.element('radiusLabelA').textContent,'徒歩800m');
 harness.element('setB').onclick();
 const map=harness.element('map'),event={target:new Element(),button:0,pointerType:'mouse',pointerId:1,clientX:1000,clientY:250};
 map.listeners.pointerdown[0](event);map.listeners.pointerup[0](event);
 harness.element('restrictB').checked=true;harness.element('restrictB').onchange();await harness.settleWalking();
 harness.element('radiusB').oninput({target:{value:'0.5'}});await harness.settleWalking();
 assert.equal(harness.element('radiusLabelB').textContent,'徒歩500m');
 query=new URLSearchParams(requests().at(-1).url.split('?')[1]);assert.equal(query.get('distance_km'),'0.5');
 harness.element('rangeMode').value='walk';harness.element('rangeMode').onchange();await harness.settleWalking();
 query=new URLSearchParams(requests().at(-1).url.split('?')[1]);
 assert.equal(query.get('minutes'),'15');assert.equal(query.has('distance_km'),false);
 assert.equal(harness.element('radiusLabelA').textContent,'徒歩15分');
 harness.element('rangeMode').value='walk-distance';harness.element('rangeMode').onchange();await harness.settleWalking();
 assert.equal(harness.element('radiusLabelA').textContent,'徒歩800m');
 const saved=JSON.parse(harness.storage.get('property_map_v2'));
 assert.equal(saved.wda,0.8);assert.equal(saved.wdb,0.5);assert.equal(saved.wa,15);assert.equal(saved.ra,3);
 harness.element('rangeMode').value='distance';harness.element('rangeMode').onchange();harness.flush();
 assert.equal(harness.element('radiusLabelA').textContent,'3 km');
});

test('normal map clicks do not mutate the origin; explicit point selection does',()=>{
 const harness=app();harness.flush();
 const before=harness.element('nameA').textContent;
 const map=harness.element('map'),event={target:new Element(),button:0,pointerType:'mouse',pointerId:1,clientX:1000,clientY:250};
 const click=()=>{map.listeners.pointerdown[0](event);map.listeners.pointerup[0](event);harness.flush();};
 click();assert.equal(harness.element('nameA').textContent,before);assert.equal(harness.storage.size,0);
 harness.element('setA').onclick();click();assert.notEqual(harness.element('nameA').textContent,before);assert.ok(harness.storage.has('property_map_v2'));
});

test('imported cards with area and age render, preserving translation callable',async()=>{
 const harness=app();await harness.importRecord();
 const text=harness.element('externalResults').textContent;
 assert.ok(text.includes('TEST ONLY, not a real advertisement'));
 assert.ok(text.includes('25㎡'));assert.ok(text.includes('築5年'));assert.ok(text.includes('月額総額：7.5'));
});

test('monthly price and unknown costs use the same filter on imported cards',async()=>{
 const harness=app();harness.element('type').value='rent';harness.element('maxPrice').value='7.4';
 await harness.importRecord();assert.ok(!harness.element('externalResults').textContent.includes('TEST ONLY, not a real advertisement'));
 harness.element('maxPrice').value='7.5';await harness.importRecord();assert.ok(harness.element('externalResults').textContent.includes('TEST ONLY, not a real advertisement'));
 await harness.importRecord({extracted:{price:7,management_fee_yen:null,mandatory_fee_yen:0}});
 assert.ok(!harness.element('externalResults').textContent.includes('TEST ONLY, not a real advertisement'));
});

test('commute-unknown candidates remain actionable and route duration unlocks the filter',async()=>{
 const harness=app();harness.element('maxCommute').value='15';await harness.importRecord();
 assert.ok(harness.element('externalResults').textContent.includes('通勤時間は未判定です。'));
 const button=harness.allNodes(harness.element('externalResults')).find(node=>node.textContent==='通勤時間を取得');
 assert.ok(button);await button.onclick();harness.flush();
 assert.ok(harness.element('externalResults').textContent.includes('10分（道路経路の推定）'));
 const request=harness.requests.find(request=>request.url==='/api/route');
 assert.equal(JSON.parse(request.options.body).origin.lat,35.681236);
 harness.element('commuteMode').value='bike';harness.element('commuteMode').listeners.input[0]();harness.flush();
 assert.ok(harness.element('externalResults').textContent.includes('通勤時間は未判定です。'));
});

test('expired imports stay hidden even with unknown-condition display enabled',async()=>{
 const harness=app();harness.element('includeUnknown').checked=true;
 await harness.importRecord({last_verified_at:new Date(Date.now()-3600001).toISOString()});
 assert.ok(!harness.element('externalResults').textContent.includes('TEST ONLY, not a real advertisement'));
});

test('saving an imported candidate keeps its fee breakdown and source verification',async()=>{
 const harness=app();await harness.importRecord();
 harness.allNodes(harness.element('externalResults')).find(node=>node.textContent==='候補に取り込む').onclick();
 assert.equal(harness.element('pFee').value,5000);
 harness.element('modalSave').onclick();harness.flush();
 const saved=JSON.parse(harness.storage.get('property_map_v2')).properties[0];
 assert.equal(saved.management_fee_yen,5000);assert.equal(saved.mandatory_fee_yen,0);assert.ok(core.fresh(saved));
 assert.equal(harness.element('matchCount').textContent,'1 / 1');
 harness.element('type').value='rent';harness.element('maxPrice').value='7.4';
 for(const listener of harness.element('maxPrice').listeners.input)listener();harness.flush();
 assert.equal(harness.element('matchCount').textContent,'0 / 1');
});

test('search discovery shows source links without resurrecting stale snippets',async()=>{
 const harness=app();harness.flush();await harness.element('searchListings').onclick();harness.flush();
 assert.ok(harness.element('discoveryLinks').textContent.includes('最新情報は掲載元で確認：suumo.jp'),harness.element('listingsStatus').textContent+' '+JSON.stringify(harness.requests));
 assert.ok(harness.element('listingsStatus').textContent.includes('募集は0件'));
});

test('public records load, geocode and render pins without claiming current availability',async()=>{
 const harness=app();harness.context.publicRecords=[{id:'tokyo:test-only',source:'tokyo',name:'TEST ONLY public inventory',address:'TEST ONLY',layer:'inventory',kind:'rent',price_yen:null,geo_precision:'unknown',data_as_of:'2025-03-31',fetched_at:new Date().toISOString(),official_url:'https://example.com/test-only',license:'TEST ONLY',license_url:'https://example.com/permission',availability_status:'unknown'}];
 await harness.element('publicLoad').onclick();harness.flush();
 assert.ok(harness.element('publicResults').textContent.includes('現在の募集状況は未確認'));
 assert.ok(harness.element('publicResults').textContent.includes('2025-03-31'));
 assert.ok(harness.allNodes(harness.element('pins')).some(node=>node.className?.includes('public inventory')));
 harness.element('publicSource_tokyo').checked=false;harness.element('publicSource_tokyo').listeners.change[0]();harness.flush();
 assert.ok(!harness.element('publicResults').textContent.includes('TEST ONLY public inventory'));
 assert.ok(!harness.allNodes(harness.element('pins')).some(node=>node.className?.includes('public inventory')));
 harness.element('publicSource_tokyo').checked=true;harness.element('publicUseFilters').checked=true;
 harness.element('publicUseFilters').listeners.change[0]();harness.flush();
 assert.ok(!harness.element('publicResults').textContent.includes('TEST ONLY public inventory'));
 harness.element('includeUnknown').checked=true;harness.element('includeUnknown').listeners.input[0]();harness.flush();
 assert.ok(harness.element('publicResults').textContent.includes('TEST ONLY public inventory'));
});

test('public filtering rejects far approximate pins, expired offers and budget mismatches',()=>{
 const record={kind:'sale',price_yen:3200000,layer:'municipal',availability_status:'reported_open',geo_precision:'approximate',lat:36,lon:136};
 const filters={includeUnknown:true,insideOnly:true,type:'sale',maxPrice:'320'};
 const origins=[{lat:35,lon:139,radius:3}];
 assert.equal(publicHousing.check(record,core,filters,origins,()=>400).match,false);
 assert.equal(publicHousing.check(record,core,filters,origins,()=>2).match,true);
 assert.equal(publicHousing.check(record,core,{...filters,maxPrice:'319'},origins,()=>2).match,false);
 const old={...record,layer:'recruitment',availability_status:'confirmed',official_url:'https://example.com/test-only',last_verified_at:new Date(Date.now()-3600001).toISOString()};
 assert.equal(publicHousing.check(old,core,filters,origins,()=>2).match,false);
 const rent={...record,kind:'rent',price_yen:90000,management_fee_yen:null,mandatory_fee_yen:null};
 assert.equal(publicHousing.check(rent,core,{...filters,type:'rent',maxPrice:'8'},origins,()=>2).match,false);
});

test('coincident public locations remain individually selectable from a grouped pin',async()=>{
 const harness=app();
 const record={source:'tokyo',address:'TEST ONLY',layer:'inventory',kind:'rent',price_yen:null,geo_precision:'approximate',lat:35.681236,lon:139.767125,data_as_of:'2025-03-31',fetched_at:new Date().toISOString(),official_url:'https://example.com/test-only',license:'TEST ONLY',license_url:'https://example.com/permission',availability_status:'unknown'};
 harness.context.publicRecords=[{...record,id:'tokyo:test-1',name:'TEST ONLY one'},{...record,id:'tokyo:test-2',name:'TEST ONLY two'}];
 await harness.element('publicLoad').onclick();harness.flush();
 const pin=()=>harness.allNodes(harness.element('pins')).find(node=>node.className?.includes('public inventory'));
 assert.equal(pin().textContent,'2件の公開資料');
 pin().onclick({stopPropagation(){}});harness.flush();
 assert.ok(harness.element('publicResults').children[0].textContent.startsWith('TEST ONLY one'));
 pin().onclick({stopPropagation(){}});harness.flush();
 assert.ok(harness.element('publicResults').children[0].textContent.startsWith('TEST ONLY two'));
});


test('current location is requested only on click and updates point A, its map pin and walking range',async()=>{
 const calls=[];
 const harness=app({rangeMode:'walk',geolocation:{getCurrentPosition:(...args)=>calls.push(args)}});
 await harness.settleWalking();assert.equal(calls.length,0);
 const button=harness.element('locateMe');
 button.click();button.click();
 assert.equal(calls.length,1);assert.equal(button.disabled,true);
 assert.match(harness.element('currentLocationStatus').textContent,/取得中/);
 assert.equal(harness.storage.has('property_map_v2'),false);
 calls[0][0]({coords:{latitude:34.6937,longitude:135.5023}});
 harness.flush();
 assert.ok(harness.element('pins').children.some(pin=>pin.title==='現在地'&&pin.style.left==='450px'&&pin.style.top==='350px'));
 await harness.settleWalking();
 const saved=JSON.parse(harness.storage.get('property_map_v2'));
 assert.equal(saved.a.lat,34.6937);assert.equal(saved.a.lon,135.5023);
 assert.equal(saved.a.name,'現在地');assert.equal(saved.b,null);
 assert.equal(harness.element('nameA').textContent,'現在地');
 assert.equal(button.disabled,false);assert.equal(button.textContent,'現在地');
 assert.match(harness.element('currentLocationStatus').textContent,/地点Aに設定しました/);
 const query=new URLSearchParams(harness.requests.filter(r=>r.url.startsWith('/api/walking-area?')).at(-1).url.split('?')[1]);
 assert.equal(query.get('lat'),'34.6937');assert.equal(query.get('lon'),'135.5023');
});

for(const [code,message] of [[1,'サイト設定'],[2,'端末の位置情報設定'],[3,'タイムアウト'],[0,'住所検索']]){
 test(`geolocation error ${code} preserves the origin and allows retry`,()=>{
  const calls=[];
  const harness=app({geolocation:{getCurrentPosition:(...args)=>calls.push(args)}});
  const before=harness.element('nameA').textContent,button=harness.element('locateMe');
  button.click();calls[0][1]({code});harness.flush();
  assert.equal(harness.element('nameA').textContent,before);
  assert.equal(harness.storage.has('property_map_v2'),false);
  assert.equal(harness.element('currentLocationStatus').hidden,false);
  assert.ok(harness.element('currentLocationStatus').textContent.includes(message));
  assert.equal(button.disabled,false);
  button.click();assert.equal(calls.length,2);
  calls[1][0]({coords:{latitude:35,longitude:139}});harness.flush();
  assert.equal(JSON.parse(harness.storage.get('property_map_v2')).a.lat,35);
 });
}

test('unsupported and insecure browsers explain how to proceed without requesting location',()=>{
 let calls=0;
 const unsupported=app();unsupported.element('locateMe').click();
 assert.match(unsupported.element('currentLocationStatus').textContent,/対応していません/);
 const insecure=app({secureContext:false,geolocation:{getCurrentPosition:()=>calls++}});
 insecure.element('locateMe').click();
 assert.match(insecure.element('currentLocationStatus').textContent,/HTTPSまたはlocalhost/);
 assert.equal(calls,0);assert.equal(insecure.storage.has('property_map_v2'),false);
});

test('invalid geolocation coordinates and synchronous API failures leave point A intact',()=>{
 for(const coords of [{latitude:NaN,longitude:139},{latitude:90,longitude:139},{latitude:35,longitude:181}]){
  const harness=app({geolocation:{getCurrentPosition:success=>success({coords})}});
  harness.element('locateMe').click();
  assert.match(harness.element('currentLocationStatus').textContent,/不正な座標/);
  assert.equal(harness.storage.has('property_map_v2'),false);
  assert.equal(harness.element('locateMe').disabled,false);
 }
 const harness=app({geolocation:{getCurrentPosition:()=>{throw Error('test only')}}});
 harness.element('locateMe').click();
 assert.match(harness.element('currentLocationStatus').textContent,/取得できません/);
 assert.equal(harness.element('locateMe').disabled,false);
 assert.equal(harness.storage.has('property_map_v2'),false);
});
