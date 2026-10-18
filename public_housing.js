(function(root){
'use strict';
function candidate(record){
 return {...record,type:record.kind,price:record.price_yen==null?null:record.price_yen/10000,url:record.official_url};
}
function check(record,core,filters,origins,distance,now=Date.now()){
 const result=core.evaluateConditions(candidate(record),filters,origins,distance);
 if(record.layer==='recruitment'&&!core.fresh(candidate(record),now))result.failures.push('確認日時・募集状況');
 if(record.availability_status==='ended')result.failures.push('募集終了');
 if(record.layer!=='recruitment')result.unknown.push('現在の募集状況');
 if(filters.insideOnly&&Number.isFinite(record.lat)&&Number.isFinite(record.lon)){
  if(typeof filters.spatialCheck==='function'){
   const within=filters.spatialCheck(record);
   if(within===false)result.failures.push('指定範囲外（概略位置）');
   else if(within==null&&!result.unknown.includes('範囲未判定'))result.unknown.push('範囲未判定');
  }else if(origins.some(origin=>distance(record,origin)>origin.radius))result.failures.push('指定半径外（概略位置）');
 }
 result.match=result.failures.length===0&&(filters.includeUnknown||result.unknown.length===0);
 return result;
}
function mount(hooks){
 const {$,t,setText,core,filters,origins,distance,makeEl,makeButton,makeLink,refresh,focus}=hooks;
 const records=new Map();let generation=0,next=null,loadedQuery=null,busy=false,selectedId=null,summary=null;
 const layers={inventory:'団地台帳',vacancy:'空き住戸数',recruitment:'確認済み募集',municipal:'自治体空き家'};
 const sources={tokyo:'東京都',tsubata:'津幡町',permitted:'許諾済みフィード'};
 const text=(node,value)=>setText(node,typeof value==='function'?value:()=>value);
 const summaryText=()=>summary?t('地域内の公開資料：')+summary.total+t('件 / 成約・終了を除外：')+summary.ended_excluded+t('件。最新の空室確認ではありません。'):'';
 function choose(record){selectedId=record.id;focus(record);$('publicResults').scrollTop=0;}
 function evaluation(record){return check(record,core,filters(),origins(),distance);}
 function visible(){return [...records.values()].filter(record=>{
  if(!$('publicSource_'+record.source).checked||!$('publicLayer_'+record.layer).checked)return false;
  if(record.layer==='recruitment'&&!core.fresh(candidate(record)))return false;
  return !$('publicUseFilters').checked||evaluation(record).match;
 });}
 function price(record){return record.price_yen==null?t('価格未確認'):(record.price_yen/10000).toLocaleString()+t(record.kind==='rent'?'万円/月（家賃のみ）':'万円（売買）');}
 function renderList(){
  const list=$('publicResults'),items=visible().sort((first,second)=>Number(second.id===selectedId)-Number(first.id===selectedId));list.replaceChildren();
  text($('publicCount'),t('公開資料の表示：')+items.length+' / '+records.size+t('件（取得済み）'));
  if(!items.length)list.append(makeEl('p','empty',t('表示は0件です。ソース・レイヤー・条件の未確認項目を確認してください。')));
  for(const record of items.slice(0,100)){
   const card=makeEl('article','external-item public-card'+(record.id===selectedId?' selected':''));card.dataset.publicId=record.id;
   card.append(makeEl('h4','',record.name),makeEl('span','info-tag warn',t(layers[record.layer])),makeEl('p','listing-price',price(record)));
   card.append(makeEl('p','',record.address),makeEl('p','muted',t('間取り・面積：')+(record.layout_text||record.layout||t('未確認'))+' / '+(record.area_text||record.area||t('未確認'))));
   const status=record.availability_status==='confirmed'?'提供元が1時間以内に募集確認':record.availability_status==='reported_open'?'公開時点で受付中・現在は未確認':'現在の募集状況は未確認';
   card.append(makeEl('p','listing-uncertain',t(status)));
   if(record.layer==='vacancy')card.append(makeEl('p','',t('公募用空き住戸：')+(record.vacancy_public??t('未確認'))+' / '+t('事業用：')+(record.vacancy_reserved??t('未確認'))+' / '+t('空き住戸は募集中とは限りません。')));
   card.append(makeEl('p','muted',t('資料基準日：')+(record.data_as_of||t('未確認'))+' / '+t('取得：')+record.fetched_at));
   if(record.source_updated_at)card.append(makeEl('p','muted',t('公開ファイル更新日：')+record.source_updated_at));
   if(record.layer==='inventory')card.append(makeEl('p','muted',t('管理戸数：')+(record.managed_units??t('未確認'))+' / '+t('最寄駅：')+(record.station||t('未確認'))));
   card.append(makeLink(t('取得元CSV・資料'),record.data_url||record.source_url));
   if(record.last_verified_at)card.append(makeEl('p','muted',t('募集確認日時：')+record.last_verified_at));
   const located=Number.isFinite(record.lat)&&Number.isFinite(record.lon);
   card.append(makeEl('p','muted',t('位置精度：')+t(record.geo_precision==='exact'?'提供元の正確な座標':located?'住所の概略位置・距離は参考':'未確認')));
   if(record.geocode_label)card.append(makeEl('p','muted',record.geocode_source+' / '+record.geocode_label));
   const result=evaluation(record);
   if(result.failures.length)card.append(makeEl('p','listing-out',t('条件外：')+result.failures.map(value=>t(value)).join(' / ')));
   if(result.unknown.length)card.append(makeEl('p','listing-uncertain',t('未確認：')+[...new Set(result.unknown)].map(value=>t(value)).join(' / ')));
   const actions=makeEl('div','line-actions');
   actions.append(makeLink(t('公式掲載・公開資料を開く'),record.official_url));
   if(located)actions.append(makeButton(t('地図'),()=>choose(record)));
   else actions.append(makeButton(t('住所を位置検索'),()=>locate([record])));
   card.append(actions,makeEl('p','muted',record.license),makeLink(t('利用条件'),record.license_url));list.append(card);
  }
  if(items.length>100)list.append(makeEl('p','muted',t('一覧は先頭100件です。地域で絞って再取得してください。')));
 }
 async function locate(items){
  if(busy)return;
  busy=true;const token=generation;
  try{
   for(const record of items.slice(0,5)){
    if(token!==generation)break;
    text($('publicStatus'),()=>t('住所の位置を照会中：')+record.address);
    const response=await fetch('/api/public-geocode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:record.source,id:record.id}),signal:AbortSignal.timeout(25000)});
    const data=await response.json();if(!response.ok)throw Error(data.error||response.status);
    if(token!==generation)break;
    Object.assign(record,data);refresh();
   }
   if(token===generation)text($('publicStatus'),()=>summaryText()+' '+t('位置検索完了。概略座標は厳密な条件一致を保証しません。'));
  }catch(error){if(token===generation)text($('publicStatus'),()=>t('位置を取得できません：')+error.message)}finally{busy=false;refresh()}
 }
 async function load(append=false){
  const source=$('publicProvider').value,area=$('publicArea').value.trim(),query=JSON.stringify([source,area]);
  const offset=append&&query===loadedQuery?next:0;if(append&&offset==null)return;
  const token=++generation;
  $('publicLoad').disabled=true;$('publicNext').disabled=true;
  if(!append)for(const [identity,record] of records)if(record.source===source)records.delete(identity);
  refresh();text($('publicStatus'),()=>t('公式の公開データを取得中…'));
  try{
   const response=await fetch('/api/public-housing?'+new URLSearchParams({source,area,offset}),{signal:AbortSignal.timeout(45000)});
   const data=await response.json();if(!response.ok)throw Error(data.error||response.status);
   if(token!==generation)return;
   for(const record of data.records)records.set(record.id,record);
   while(records.size>500)records.delete(records.keys().next().value);
   next=data.next_offset;loadedQuery=query;$('publicSource_'+source).checked=true;
   $('publicAreas').replaceChildren(...data.municipalities.map(value=>{const option=makeEl('option');option.value=value;return option}));
   summary={total:data.total,ended_excluded:data.ended_excluded};text($('publicStatus'),summaryText);
   refresh();
   await locate(data.records.filter(record=>record.geo_precision==='unknown').slice(0,5).map(record=>records.get(record.id)));
  }catch(error){if(token===generation){next=null;text($('publicStatus'),()=>t('取得できません：')+error.message)}}finally{
   if(token===generation){$('publicLoad').disabled=false;$('publicNext').disabled=next==null;refresh()}
  }
 }
 $('publicLoad').onclick=()=>load();$('publicNext').onclick=()=>load(true);
 $('publicGeocode').onclick=()=>locate(visible().filter(record=>record.geo_precision==='unknown').slice(0,5));
 $('publicProvider').onchange=()=>{generation++;next=null;$('publicNext').disabled=true;$('publicLoad').disabled=false;$('publicArea').value=$('publicProvider').value==='tokyo'?'千代田区':'';};
 $('publicArea').oninput=()=>{generation++;next=null;$('publicNext').disabled=true;$('publicLoad').disabled=false;};
 for(const id of ['publicUseFilters',...Object.keys(sources).map(value=>'publicSource_'+value),...Object.keys(layers).map(value=>'publicLayer_'+value)])$(id).addEventListener('change',refresh);
 return {renderList,renderPins(add){
  const groups=new Map();
  for(const record of visible().filter(record=>Number.isFinite(record.lat)&&Number.isFinite(record.lon)).slice(0,500)){
   const position=record.lat+','+record.lon;
   if(!groups.has(position))groups.set(position,[]);
   groups.get(position).push(record);
  }
  for(const group of groups.values()){
   const record=group[0];
   const label=group.length>1?group.length+t('件の公開資料'):record.price_yen==null?t('価格未確認'):(record.price_yen/10000).toLocaleString()+t(record.kind==='rent'?'万/月':'万');
   add({...record,name:group.map(item=>item.name).join(' / ')},'public '+record.layer,label,()=>choose(group[(group.findIndex(item=>item.id===selectedId)+1)%group.length]));
  }
 }};
}
const api={candidate,check,mount};
if(typeof module==='object'&&module.exports)module.exports=api;else root.publicHousing=api;
})(typeof window==='object'?window:globalThis);
