const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const core=require('../property_core.js');
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
 click(){this.onclick?.();}
}

function app(){
 const elements=new Map(),frames=[],requests=[];
 const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 for(const id of ['insideOnly','listingOnlyWithin'])element(id).checked=true;
 element('commuteMode').value='foot';
 const storage=new Map();
 const context={
  window:{propertyCore:core,homeI18n:{t:(text,values=[])=>String(text).replace(/\{(\d+)\}/g,(_,index)=>values[index]??''),setText:(node,render)=>{node.textContent=render();}},addEventListener(){}},
  document:{getElementById:element,createElement:()=>new Element(),createDocumentFragment:()=>new Element(),querySelectorAll:()=>[],addEventListener(){}},
  localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
  location:{protocol:'http:'},requestAnimationFrame:callback=>frames.push(callback),setInterval(){},setTimeout:()=>1,clearTimeout(){},
  URL,URLSearchParams,AbortSignal,AbortController,confirm:()=>true,
  fetch:async(url,options)=>{
   requests.push({url,options});
   let body={listings_enabled:true,has_brave_key:false,decisions_enabled:false,feed_enabled:false,provider:'Bing RSS'};
   if(url==='/api/import')body={results:context.records,rejected:0,provider:'feed'};
   if(url==='/api/route')body={status:'ok',duration_seconds:600,distance_meters:600,checked_at:new Date().toISOString(),source:'TEST ONLY route'};
   if(url.startsWith('/api/listings?'))body={results:[],discovery_links:[{url:'https://suumo.jp/test-only',source:'suumo.jp'}],provider:'bing'};
   return {ok:true,json:async()=>body};
  },records:[],
 };
 vm.runInNewContext(script,context);
 function flush(){while(frames.length)frames.shift()();}
 async function importRecord(overrides={}){
  context.records=[{title:'TEST ONLY, not a real advertisement',description:'TEST ONLY',url:'https://example.com/test-only',source:'Test source',kind:'rent',lat:35.681236,lon:139.767125,geo_precision:'exact',availability_status:'confirmed',last_verified_at:new Date().toISOString(),extracted:{price:7,management_fee_yen:5000,mandatory_fee_yen:0,layout:'1K',area:25,age:5},...overrides}];
  element('feedPermitted').checked=true;
  const target=element('feedFile');target.files=[{size:10,name:'test.json',text:async()=>'[]'}];
  await target.onchange({target});flush();
 }
 function allNodes(root){return [root,...root.children.flatMap(allNodes)];}
 return {element,context,requests,storage,flush,importRecord,allNodes};
}

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
