/* Connect the existing map/public-layer interfaces to the agent's owned state. */
(() => {
'use strict';
window.mountMapAgent=({session,adapter})=>{
 const {t}=window.homeI18n;const $=id=>document.getElementById(id);
 let activeChat=null,selected=null,resultId=null,applying=false,dirty=false,loading=false,reportTimer=null,seen='',publicToken='',commandId=null,lastReport='',ack=null;
 const panel=document.createElement('section');panel.className='map-home-details';panel.hidden=true;panel.setAttribute('aria-label',t('物件の詳細'));panel.setAttribute('aria-live','polite');adapter.mapElement().parentElement.append(panel);
 const status=document.createElement('div');status.className='map-agent-status';status.setAttribute('role','status');adapter.mapElement().parentElement.append(status);
 const el=(tag,text)=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;return node;};
 const latest=()=>session.snapshot?.result_sets?.at(-1);
 const records=()=>resultId==='public'?(adapter.publicState()?.visible||[]):(session.snapshot?.result_sets?.find(r=>r.id===resultId)||latest())?.items||[];
 const current=()=>resultId==='public'?(adapter.publicState()?.records||[]).find(i=>i.id===selected):records().find(i=>i.id===selected);
 function inside(item){if(!Number.isFinite(item.lat)||!Number.isFinite(item.lon))return false;const p=adapter.screen(item),r=adapter.mapElement().getBoundingClientRect();return p.x>=0&&p.y>=0&&p.x<=r.width&&p.y<=r.height;}
 function view(){
  if(!resultId&&(adapter.publicState()?.records.length))resultId='public';
  const features=[...(adapter.publicState()?.visible||[]).filter(inside).map(i=>({result_set_id:'public',listing_id:i.id})),...(latest()?.items||[]).filter(inside).map(i=>({result_set_id:latest().id,listing_id:i.id}))].slice(0,100);
  return {visible_features:features,reference_features:adapter.referenceFeatures?.()||[],...adapter.view(),selected_listing_id:selected,result_set_id:resultId,visible_listing_ids:records().filter(inside).slice(0,30).map(i=>i.id)};}
 function schedule(){clearTimeout(reportTimer);reportTimer=setTimeout(report,250);}
 function onRender(){if(!session.snapshot)return;const publicIds=JSON.stringify((adapter.publicState()?.records||[]).map(r=>[r.id,r.lat,r.lon]));if(publicIds!==onRender.publicIds){onRender.publicIds=publicIds;lastReport='';schedule();}const fingerprint=JSON.stringify(view());if(fingerprint!==seen){seen=fingerprint;if(!applying)dirty=true;schedule();}}
 async function report(){
  if(loading||!session.snapshot)return;
  const chat=activeChat,v=view(),fingerprint=JSON.stringify(v);if(fingerprint===lastReport&&!ack){dirty=false;return;}
  loading=true;
  const sentAck=ack;
  try{
   const response=await fetch('/api/map',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:chat,expected_revision:session.snapshot.map_view?.revision||0,...v,public_records:(adapter.publicState()?.records||[]).map(r=>({id:r.id,source:r.source})),...(sentAck?{ack:sentAck}:{})}),signal:AbortSignal.timeout(15000)});
   const data=await response.json();if(chat!==activeChat)return;
   if(response.ok){lastReport=fingerprint;if(ack===sentAck)ack=null;dirty=JSON.stringify(view())!==fingerprint;
    // Map writes have their own revision; never replace newer criteria or a draft.
    if(data.chat_id===chat){session.snapshot.map_view=data.map_view;session.snapshot.map_command=data.map_command;}
    status.textContent='';
   }else if(response.status===409){if(data.map_view)session.snapshot.map_view=data.map_view;dirty=true;schedule();}
   else throw Error('report');
  }catch{status.textContent=t('地図の状態を同期できません。再試行します。');schedule();}
  finally{loading=false;if(dirty||ack)schedule();}
 }
 function details(){
  const item=current();panel.replaceChildren();panel.hidden=!item;if(!item)return;
  const close=el('button','×');close.className='close-home';close.setAttribute('aria-label',t('閉じる'));close.onclick=()=>{selected=null;details();adapter.refresh();onRender();};
  panel.append(close,el('h3',item.name||item.title));
  const approximate=item.geo_precision!=='exact';panel.append(el('p',t(approximate?'住所の概略位置です。建物の正確な位置ではありません。':'提供元の座標。所在地は掲載元で確認してください。')));
  panel.append(el('p',t(item.layer?'公開資料です。現在の募集を保証しません。':'空室・全費用は未確認です。')));
  if(item.web_result){const kinds={listing_detail:'個別の物件ページ',building_page:'建物のページ',listing_search_page:'検索・一覧ページ',official_info:'公的情報のページ',other:'その他のページ'};
   panel.append(el('p',t('ウェブ検索で見つかったページ（{0}・{1}）の要約です。掲載元で最新の内容を確認してください。',[t(kinds[item.page_kind]||'その他のページ'),item.source])));if(item.snippet)panel.append(el('p',item.snippet));}
  const labels={price:'家賃（万円/月）',management_fee_yen:'管理費（円/月）',mandatory_fee_yen:'必須月額費用（円/月）',area:'面積（㎡）',layout:'間取り',address:'住所',floor:'階',age:'築年数（年）',age_text:'築年',transport:'交通',deposit:'敷金',key_money:'礼金',pets:'ペット可',parking:'駐車場',price_yen:'価格（円）',data_as_of:'資料基準日',license:'利用条件',kind:'物件種別',layout_text:'間取りの範囲',area_text:'面積の範囲（㎡）',managed_units:'管理戸数',station:'最寄駅',construction_year_text:'建設年度',vacancy_public:'公募用空き住戸',vacancy_reserved:'事業用空き住戸',geocode_label:'位置検索の一致住所',source_updated_at:'公開ファイル更新日',availability_status:'募集状態'};
  const rows=[];
  const add=(key,value,label)=>{if(!rows.some(r=>r[0]===key))rows.push([key,value,label]);};
  if(item.layer){for(const key of ['address','kind','price_yen','layout_text','area_text','managed_units','station','construction_year_text','vacancy_public','vacancy_reserved','geocode_label','data_as_of','source_updated_at','availability_status','license'])add(key,item[key],labels[key]);}
  for(const [key,value] of Object.entries(item.extracted||{}))if(labels[key])add(key,value,labels[key]);
  // Permitted source detail tables keep their original headings; internal snake_case keys stay hidden.
  for(const [key,value] of Object.entries(item.source_fields||{}))if(!/^[a-z0-9_]+$/.test(key)&&key.length<=40)add(key,value,key);
  const format=value=>{
    if(value==null||value===''||value==='unknown')return t('未確認');
    if(value===true)return t('はい');if(value===false)return t('いいえ');
    if(value==='rent')return t('賃貸');if(value==='sale')return t('売買');
    if(typeof value==='string'&&/^\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:?\d{2})?)?$/.test(value)){const date=new Date(value);if(!Number.isNaN(date.getTime()))return new Intl.DateTimeFormat(document.documentElement.lang==='en'?'en':'ja-JP',{year:'numeric',month:'short',day:'numeric',...(value.length===10?{timeZone:'UTC'}:{})}).format(date);}
    if(typeof value==='number')return value.toLocaleString(document.documentElement.lang==='en'?'en':'ja-JP');
    return String(value).slice(0,300);
  };
  const dl=el('dl');for(const [,value,label] of rows)dl.append(el('dt',t(label)),el('dd',format(value)));panel.append(dl);
  const url=item.official_url||item.url;if(url&&/^https:\/\//.test(url)){const a=el('a',t('掲載元・公式資料を開く'));a.href=url;a.target='_blank';a.rel='noopener noreferrer';panel.append(a);}
  const ask=el('button',t('この物件について聞く'));ask.onclick=async()=>{await report();window.homeChat?.send(document.documentElement.lang==='en'?'Read the details of the home currently selected on the map and explain its known features and unknowns.':'地図で選択中の物件の詳細を確認して、分かっている条件と不明点を教えてください。');};panel.append(ask);
 }
 function selectPublic(record){resultId='public';selected=record.id;details();onRender();}
 function selectListing(set,id){resultId=set;selected=id;const item=current();if(!item)return false;if(Number.isFinite(item.lat)&&Number.isFinite(item.lon))adapter.move(item,Math.max(adapter.view().zoom,15));else status.textContent=t('位置未確認です。会話で地図への表示を依頼してください。');details();adapter.refresh();onRender();return true;}
 function renderPins(){
  if(!session.snapshot)return;
  for(const node of adapter.pins().querySelectorAll('[data-public-ids]'))node.classList.toggle('agent-selected',JSON.parse(node.dataset.publicIds).includes(selected));
  const items=(latest()?.items||[]).filter(i=>i.discovery_only&&Number.isFinite(i.lat)&&Number.isFinite(i.lon));
  const groups=new Map();for(const item of items){const key=item.lat+','+item.lon;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);}
  for(const group of groups.values()){
   const first=group[0],p=adapter.screen(first),r=adapter.mapElement().getBoundingClientRect();if(p.x<0||p.y<0||p.x>r.width||p.y>r.height)continue;
   const node=el('button',group.length>1?group.length+' '+t('候補'):t('概略')+' · '+(first.extracted?.price!=null?'¥'+Math.round(first.extracted.price*10000).toLocaleString('ja-JP'):t('価格未確認')));
   node.className='marker agent-lead'+(group.some(i=>i.id===selected)?' agent-selected':'');node.style.left=p.x+'px';node.style.top=p.y+'px';node.dataset.listingId=first.id;node.title=group.map(i=>i.title).join(' / ')+' · '+t('住所の概略位置');node.onclick=e=>{e.stopPropagation();selectListing(latest().id,group[(group.findIndex(i=>i.id===selected)+1)%group.length].id);};adapter.pins().append(node);
  }
 }
 function reconcile(){
  const s=session.snapshot;if(!s)return;
  if(activeChat!==s.chat_id){activeChat=s.chat_id;commandId=null;publicToken='';selected=s.map_view?.selected_listing_id||null;resultId=s.map_view?.result_set_id||latest()?.id||null;dirty=false;lastReport='';seen='';adapter.replacePublic(s.public_records||[]);if(s.map_view?.revision)adapter.move(s.map_view.center,s.map_view.zoom);}
  const token=JSON.stringify(s.public_records||[]);if(token!==publicToken){publicToken=token;adapter.replacePublic(s.public_records||[]);}
  const c=s.map_command;
  if(c?.status==='pending'&&c.id!==commandId){
   commandId=c.id;
   if(dirty||c.expected_revision!==(s.map_view?.revision||0)){ack={id:c.id,status:'stale_view'};schedule();return;}
   applying=true;let ok=true;
   try{
    if(c.action==='show_results'){resultId=c.result_set_id;selected=null;ok=adapter.fit(records().filter(i=>Number.isFinite(i.lat)&&Number.isFinite(i.lon)));}
    else if(c.action==='focus_listing'){resultId=c.result_set_id;if(resultId==='public')ok=adapter.selectPublic(c.listing_id);else ok=selectListing(resultId,c.listing_id);if(c.zoom!=null)adapter.move(adapter.view().center,c.zoom);}
    else if(c.action==='zoom')adapter.move(adapter.view().center,c.zoom??Math.min(18,adapter.view().zoom+1));
    else if(c.action==='show_place')adapter.move(c.place,c.zoom??15);
    ack={id:c.id,status:ok?'applied':'unavailable'};seen=JSON.stringify(view());dirty=false;details();schedule();
   }finally{applying=false;}
  }
  details();adapter.refresh();
 }
 session.addEventListener('change',reconcile);window.addEventListener('home-language-change',details);
 async function flush(){const until=Date.now()+12000;while(loading&&Date.now()<until)await new Promise(r=>setTimeout(r,50));await report();if(dirty)throw Error('map_sync_failed');}
 window.homeMapAgent={onRender,renderPins,selectPublic,selectListing,view,flush};
};
})();
