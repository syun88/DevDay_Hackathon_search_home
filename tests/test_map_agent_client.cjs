// State-machine regressions for map_agent.js: resets, rejected reports, stale acks and change causes.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'..','map_agent.js'),'utf8');

class Node {
 constructor(id=''){this.id=id;this.children=[];this.listeners={};this.hidden=false;this.dataset={};this.style={};this.textContent='';this.classList={toggle(){},add(){}};}
 append(...n){this.children.push(...n);} replaceChildren(...n){this.children=n;} setAttribute(){} remove(){}
 addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);} querySelectorAll(){return [];}
 getBoundingClientRect(){return {width:800,height:600};} closest(selector){return selector.includes('#map')&&this.inMap?this:null;}
}

function setup(snapshot){
 const timers=[],requests=[],responses=[];
 const map=new Node('map'),column=new Node('mapcol');map.parentElement=column;
 const records={public:[...(snapshot.public_records||[])]};
 const moves=[];let center={lat:35.68,lon:139.76},zoom=12;
 const adapter={mapElement:()=>map,pins:()=>new Node(),screen:()=>({x:10,y:10}),refresh(){},view:()=>({center:{...center},zoom}),
  move(p,z){center={lat:p.lat,lon:p.lon};if(z!=null)zoom=z;moves.push([p,z]);},fit:items=>items.length>0,
  publicState:()=>({records:records.public,visible:records.public,selectedId:null}),replacePublic(items){records.public=[...items];},selectPublic:()=>true};
 const session=new EventTarget();session.snapshot=snapshot;
 const context={window:{homeI18n:{t:x=>x},addEventListener(){}},document:{createElement:()=>new Node(),getElementById:()=>null,documentElement:{lang:'ja'}},
  setTimeout:(fn,delay)=>{timers.push(fn);return timers.length;},clearTimeout(){},AbortSignal:{timeout:()=>undefined},Intl,Number,JSON,Date,
  fetch:async(url,options)=>{const body=JSON.parse(options.body);requests.push(body);const next=responses.shift()||{status:200,body:{chat_id:body.chat_id,map_view:{...session.snapshot.map_view,revision:(session.snapshot.map_view.revision||0)+1},map_command:session.snapshot.map_command,public_records:session.snapshot.public_records||[],public_version:session.snapshot.public_version||0}};
   return {ok:next.status<300,status:next.status,json:async()=>next.body};}};
 vm.runInNewContext(source,context);
 context.window.mountMapAgent({session,adapter});
 const agent=context.window.homeMapAgent;
 async function tick(rounds=1){for(let r=0;r<rounds;r++){const pending=timers.splice(0);for(const fn of pending)await fn();await new Promise(res=>setImmediate(res));}}
 function update(next){session.snapshot=next;session.dispatchEvent(new Event('change'));}
 function gesture(){const target=new Node();target.inMap=true;for(const fn of column.listeners.pointerdown||[])fn({target});}
 return {agent,session,requests,responses,tick,update,gesture,records,moves,map};
}

const base=()=>({chat_id:'chat_1',revision:3,map_view:{revision:4,user_revision:1,center:{lat:35.68,lon:139.76},zoom:12},map_command:null,
 result_sets:[{id:'results_1',criteria:{},items:[{id:'listing_a',lat:35.69,lon:139.7,url:'https://example.com/a',discovery_only:true}]}],public_records:[],public_version:2});

test('a same-chat reset drops the old selection, result set and ack instead of re-sending them',async()=>{
 const h=setup(base());h.update(base());
 h.agent.selectListing('results_1','listing_a');await h.tick();
 assert.equal(h.requests.at(-1).selected_listing_id,'listing_a');
 h.update({...base(),revision:5,result_sets:[],map_view:{revision:6,user_revision:2,center:{lat:35.68,lon:139.76},zoom:12},public_version:3});
 const state=h.agent._state();
 assert.equal(state.selected,null);assert.equal(state.resultId,null);assert.equal(state.ack,null);
 h.gesture();h.agent.onRender();await h.tick();
 const last=h.requests.at(-1);
 assert.equal(last.selected_listing_id,null);assert.equal(last.result_set_id,null);assert.equal(last.ack,undefined);
});

test('rejected reports stop after one pruned retry and never block chat',async()=>{
 const h=setup(base());h.update(base());
 h.responses.push({status:400,body:{error:'Invalid session update'}},{status:400,body:{error:'Invalid session update'}});
 h.gesture();h.agent.selectListing('results_1','listing_a');
 await h.tick(6);
 assert.equal(h.requests.length,2);  // One retry, then it stops; no endless 400 loop.
 assert.equal(h.agent._state().rejected,2);
 assert.equal(await h.agent.flush(),true);  // Resolves instead of throwing map_sync_failed.
});

test('an ack for a command that no longer exists is dropped',async()=>{
 const h=setup(base());
 h.update({...base(),map_command:{id:'cmd_1',status:'pending',action:'zoom',zoom:15,expected_revision:4,expected_user_revision:1}});
 assert.equal(JSON.stringify(h.agent._state().ack),JSON.stringify({id:'cmd_1',status:'applied'}));
 h.update({...base(),map_command:null});
 assert.equal(h.agent._state().ack,null);
});

test('applying agent commands reports an app change; a real gesture reports a user change',async()=>{
 const h=setup(base());h.update(base());await h.tick();
 h.update({...base(),map_command:{id:'cmd_1',status:'pending',action:'zoom',zoom:15,expected_revision:4,expected_user_revision:1}});
 await h.tick();
 const applied=h.requests.at(-1);
 assert.equal(applied.cause,'app');assert.equal(JSON.stringify(applied.ack),JSON.stringify({id:'cmd_1',status:'applied'}));
 h.gesture();h.moves.length=0;h.agent.onRender();await h.tick();
 assert.equal(h.requests.at(-1).cause,'user');
});

test('a command is stale only after a real user change, not after app-only revisions',async()=>{
 const h=setup(base());h.update(base());await h.tick();
 // The view revision moved on (app changes) but the user revision did not: apply it.
 h.update({...base(),map_view:{revision:9,user_revision:1,center:{lat:35.68,lon:139.76},zoom:12},map_command:{id:'cmd_2',status:'pending',action:'zoom',zoom:14,expected_revision:4,expected_user_revision:1}});
 assert.equal(h.agent._state().ack.status,'applied');
 h.update({...base(),map_view:{revision:10,user_revision:2,center:{lat:35.68,lon:139.76},zoom:12},map_command:{id:'cmd_3',status:'pending',action:'zoom',zoom:13,expected_revision:9,expected_user_revision:1}});
 assert.equal(h.agent._state().ack.status,'stale_view');
});

test('the browser adopts a newer public record set returned by the server',async()=>{
 const h=setup(base());h.update(base());
 h.records.public=[{id:'tokyo:old',source:'tokyo',lat:35.6,lon:139.7}];
 h.responses.push({status:200,body:{chat_id:'chat_1',map_view:{revision:5,user_revision:2},map_command:null,public_records:[{id:'tokyo:new',source:'tokyo',lat:35.61,lon:139.71}],public_version:3}});
 h.gesture();h.agent.onRender();await h.tick();
 assert.equal(h.requests[0].public_version,2);  // Sent with the version the browser had adopted.
 assert.deepEqual(h.records.public.map(r=>r.id),['tokyo:new']);
 assert.equal(h.session.snapshot.public_version,3);
});
