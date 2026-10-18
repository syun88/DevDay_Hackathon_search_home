/* Run with: node --test tests/session_client.test.cjs (no npm dependencies). */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../session.js'),'utf8');
function setup(fetch) {
  const context = {window:{},EventTarget,Event,structuredClone,AbortSignal,fetch,
    // Tests explicitly flush rather than waiting for debounce timers.
    setTimeout:()=>0,clearTimeout:()=>{}};
  vm.runInNewContext(source,context);
  return new context.window.SearchSession();
}
const snapshot = (revision=0,criteria={layout:'',pets:false},draft='') =>
  ({revision,schema_version:1,criteria,draft_message:draft});
const response = (data,status=200) => ({ok:status===200,status,json:async()=>data});
const tick = () => new Promise(resolve=>setImmediate(resolve));

test('serializes edits made during an in-flight save without losing newer fields',async()=>{
  let complete;
  const requests=[];
  const session=setup(async(url,options)=>{
    if(!options.method)return response(snapshot());
    requests.push(JSON.parse(options.body));
    if(requests.length===1)return new Promise(resolve=>{complete=resolve});
    return response(snapshot(2,{layout:'1K',pets:true},'new draft'));
  });
  await session.load();session.patch({layout:'1K'});
  const first=session.flush();
  session.patch({pets:true},'new draft');
  complete(response(snapshot(1,{layout:'1K',pets:false})));
  await first;await tick();
  assert.equal(requests[0].expected_revision,0);
  assert.equal(requests[1].expected_revision,1);
  assert.deepEqual(requests[1].criteria,{pets:true});
  assert.equal(session.criteria.pets,true);
  assert.equal(session.draft,'new draft');
  assert.equal(session.status,'saved');
});

test('failed saves retain edits and retry sends the same revision',async()=>{
  let fail=true;
  const session=setup(async(url,options)=>{
    if(!options.method)return response(snapshot());
    if(fail)throw Error('offline');
    assert.equal(JSON.parse(options.body).expected_revision,0);
    return response(snapshot(1,{layout:'2K',pets:false}));
  });
  await session.load();session.patch({layout:'2K'});await session.flush();
  assert.equal(session.status,'error');assert.equal(session.criteria.layout,'2K');
  fail=false;await session.retry();assert.equal(session.status,'saved');
});

test('conflicts preserve local edits and require explicit reload',async()=>{
  const session=setup(async(url,options)=>options.method
    ? response({session:snapshot(1,{layout:'2K',pets:false})},409):response(snapshot()));
  await session.load();session.patch({layout:'1K'});await session.flush();
  assert.equal(session.status,'conflict');assert.equal(session.criteria.layout,'1K');
  await session.flush();assert.equal(session.status,'conflict');
  await session.load();assert.equal(session.criteria.layout,'');assert.equal(session.status,'saved');
});

test('unavailable persistence retains local criteria without claiming they were saved',async()=>{
  const session=setup(async()=>{throw Error('offline')});
  session.criteria={layout:'',pets:false};await session.load();session.patch({layout:'1K'});
  assert.equal(session.status,'unavailable');assert.equal(session.criteria.layout,'1K');
});
