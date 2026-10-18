const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const {parseHTML}=require('linkedom');
const source=name=>fs.readFileSync(path.join(__dirname,'..',name),'utf8');

function workspace({mobile=false,agent=false}={}){
 const {window,document}=parseHTML(source('index.html'));
 document.activeElement=null;
 window.HTMLElement.prototype.focus=function(){document.activeElement=this;};
 window.HTMLElement.prototype.scrollIntoView=function(){};
 window.HTMLElement.prototype.reportValidity=()=>true;
 window.HTMLElement.prototype.getClientRects=function(){return this.closest('[hidden]')?[]:[{}];};
 Object.defineProperty(window.HTMLSelectElement.prototype,'value',{
  configurable:true,get(){return [...this.querySelectorAll('option')].find(option=>option.selected)?.getAttribute('value')??this.querySelector('option')?.getAttribute('value')??'';},
  set(value){for(const option of this.querySelectorAll('option'))option.toggleAttribute('selected',option.getAttribute('value')===value);}
 });
 window.HTMLSelectElement.prototype.add=function(option){this.append(option);};
 for(const input of document.querySelectorAll('input')){input.checked=input.hasAttribute('checked');input.validity={valid:true};input.reportValidity=()=>true;}
 window.homeI18n={t:(text,values=[])=>text.replace(/\{(\d+)\}/g,(_,index)=>values[index]??''),setText:(node,render)=>{node.textContent=render();}};
 class Session{
  constructor(){this.criteria={};this.draft='';this.status='saved';this.pending={};this.saving=false;this.handlers=[];this.snapshot=null;}
  addEventListener(type,handler){this.handlers.push(handler);}
  emit(){for(const handler of this.handlers)handler();}
  load(){this.snapshot={chat_id:'TEST ONLY',revision:0,criteria:this.criteria,conversation:[],memory:{},saved_candidates:[],result_sets:[],history:[],chat:{status:'idle'}};this.emit();}
  patch(criteria,draft=this.draft){this.criteria={...this.criteria,...criteria};this.draft=draft;this.snapshot={...this.snapshot,revision:this.snapshot.revision+1,criteria:this.criteria};this.emit();}
  async flush(){}
 }
 window.SearchSession=Session;window.mountHomeChat=()=>{};
 let imports=0,searches=0;
 document.getElementById('importBtn').onclick=()=>imports++;
 document.getElementById('searchListings').onclick=()=>searches++;
 const requests=[];
 const context={window,document,ResizeObserver:class{observe(){}},matchMedia:()=>({matches:mobile}),
  localStorage:{getItem:()=>null,setItem(){}},sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  fetch:async url=>{requests.push(url);return {ok:true,json:async()=>url==='/api/health'?{agent_enabled:agent}:{chats:[]}};},
  setTimeout:()=>1,clearTimeout(){},AbortSignal,URL,crypto:require('node:crypto').webcrypto,
  Option:function(text,value){const option=document.createElement('option');option.textContent=text;option.value=value;return option;},
 };
 const map={a:{name:'東京駅',lat:35.681236,lon:139.767125},b:null,ra:3,rb:5};
 const adapter={getMapState:()=>map,applyMapState(){},onAnchorChanged(){},onRadiusChanged(){},onLayoutChange(){},applyAgentResults(){}};
 for(const file of ['shell.js','workspace.js'])vm.runInNewContext(source(file),context);
 window.mountSearchWorkspace(adapter);
 return {window,document,context,adapter,session:window.searchWorkspace.session,requests,
  element:id=>document.getElementById(id),imports:()=>imports,searches:()=>searches};
}

test('real markup mounts once without losing relocated controls or existing actions',()=>{
 const app=workspace();
 assert.equal(app.document.querySelectorAll('.shell-rail button').length,4);
 assert.equal(app.element('stateRevision'),null);assert.equal(app.element('chatNew'),null);
 assert.ok(app.document.querySelector('.condition-actions #searchListings'));
 assert.ok(app.document.querySelector('#publicSettings #publicLoad'));
 assert.ok(app.document.querySelector('#manualSavedSettings #csvFile'));
 app.element('importBtn').click();assert.equal(app.imports(),1);
 const ids=[...app.document.querySelectorAll('[id]')].map(node=>node.id);
 assert.equal(new Set(ids).size,ids.length);
 for(const control of ['chatDraft','maxPrice','queryA','queryB','feedFile','publicProvider','chatSend','results','chatUndo'])assert.ok(app.element(control),control);
});

test('optional tools are collapsed while active criteria remain visible and editable',()=>{
 const app=workspace();
 assert.equal(app.element('coreSettings').open,true);
 for(const id of ['publicSettings','mapToolsSettings','integrationSettings','manualSavedSettings'])assert.equal(app.element(id).open,false);
 assert.equal(app.element('criteriaChips').children.length,3);
 app.session.patch({pets:true,layout:'1K',property_type:'rent'});
 assert.equal(app.element('criteriaChips').children.length,6);
 app.document.querySelector('[data-criterion=layout]').click();
 app.element('criteriaEditValue').value='2DK';app.element('criteriaEditor').onsubmit({preventDefault(){}});
 assert.equal(app.session.criteria.layout,'2DK');
 assert.equal(app.element('layout').value,'2DK');
 app.session.patch({pets:false,layout:'',property_type:''});
 assert.equal(app.element('criteriaChips').children.length,3);
});

test('results and saved tabs show different panes and support keyboard navigation',()=>{
 const app=workspace();app.element('railResults').click();
 assert.equal(app.element('resultsDrawer').hidden,false);
 assert.equal(app.element('searchResultsPane').hidden,false);
 assert.equal(app.element('savedResultsPane').hidden,true);
 app.element('resultTab0').onkeydown({key:'ArrowRight',preventDefault(){}});
 assert.equal(app.element('searchResultsPane').hidden,true);
 assert.equal(app.element('savedResultsPane').hidden,false);
 assert.equal(app.element('resultTab1').getAttribute('aria-selected'),'true');
 assert.equal(app.document.activeElement?.id,'resultTab1');
});

test('drawers restore focus, reveal a requested hidden group and exit for map picking',()=>{
 const app=workspace();app.element('editConditions').focus();app.element('editConditions').click();
 assert.equal(app.document.querySelector('.settings-drawer').hidden,false);
 assert.equal(app.document.querySelector('.mapcol').inert,true);
 app.document.querySelector('.settings-drawer .drawer-close').click();
 assert.equal(app.document.activeElement?.id,'editConditions');
 assert.equal(app.document.querySelector('.mapcol').inert,false);
 app.window.searchShell.openSettings('listingKeywords');
 assert.equal(app.element('integrationSettings').open,true);
 app.element('setA').click();
 assert.equal(app.document.querySelector('.settings-drawer').hidden,true);
 assert.equal(app.document.activeElement?.id,'resetView');
 app.window.searchShell.openSources();assert.equal(app.element('publicSettings').open,true);
});

test('selecting a public-data card inside settings preserves the outside focus return',()=>{
 const app=workspace();app.element('editConditions').focus();app.element('editConditions').click();
 app.element('publicLoad').focus();app.window.searchShell.openSources();
 app.document.querySelector('.settings-drawer .drawer-close').click();
 assert.equal(app.document.activeElement?.id,'editConditions');
});

test('missing AI configuration leaves a clear manual-search path without a provider call',async()=>{
 const app=workspace();vm.runInNewContext(source('chat.js'),app.context);app.window.mountHomeChat({session:app.session,adapter:app.adapter});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.element('chatSend').disabled,true);
 assert.equal(app.element('chatError').hidden,true);
 assert.match(app.element('chatAvailability').textContent,/未接続/);
 app.element('searchListings').click();assert.equal(app.searches(),1);
 assert.equal(app.element('resultsDrawer').hidden,false);
 assert.ok(!app.requests.includes('/api/chat'));
});

test('empty chat drafts cannot be sent and changing a draft does not submit it',async()=>{
 const app=workspace({agent:true});vm.runInNewContext(source('chat.js'),app.context);app.window.mountHomeChat({session:app.session,adapter:app.adapter});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(app.element('chatSend').disabled,true);
 app.element('chatDraft').value='TEST ONLY draft';app.element('chatDraft').dispatchEvent(new app.window.Event('input'));
 assert.equal(app.element('chatSend').disabled,false);
 assert.equal(app.session.draft,'TEST ONLY draft');
 assert.ok(!app.requests.includes('/api/chat'));
 app.element('chatDraft').value=' ';app.element('chatDraft').dispatchEvent(new app.window.Event('input'));
 assert.equal(app.element('chatSend').disabled,true);
});

test('mobile history is dismissible and does not leave background controls inert',()=>{
 const app=workspace({mobile:true});app.element('historyToggle').click();
 assert.equal(app.element('chatHistoryPanel').hidden,false);
 assert.equal(app.element('chatHistoryPanel').getAttribute('role'),'dialog');
 assert.equal(app.document.querySelector('.mapcol').inert,true);
 const event=new app.window.Event('keydown',{bubbles:true});event.key='Escape';app.document.dispatchEvent(event);
 assert.equal(app.element('chatHistoryPanel').hidden,true);
 assert.equal(app.document.querySelector('.mapcol').inert,false);
 assert.equal(app.document.activeElement?.id,'historyToggle');
});
