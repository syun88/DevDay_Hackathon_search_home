(function(root){
'use strict';
const MAX_AGE_MS=60*60*1000;
function number(value){
 if(!['number','string'].includes(typeof value)||(typeof value==='string'&&!value.trim()))return null;
 const parsed=Number(value);
 return Number.isFinite(parsed)&&parsed>=0?parsed:null;
}
function fresh(record,now=Date.now()){
 const verified=Date.parse(record.last_verified_at);
 return /(?:Z|[+-]\d{2}:\d{2})$/i.test(record.last_verified_at)&&safeUrl(record.url)&&/^https:/i.test(record.url)&&record.availability_status==='confirmed'&&Number.isFinite(verified)&&verified<=now&&now-verified<=MAX_AGE_MS;
}
function safeUrl(value){
 try{const parsed=new URL(value);return ['https:','http:'].includes(parsed.protocol)&&Boolean(parsed.hostname)&&!parsed.username&&!parsed.password;}catch{return false}
}
function monthlyPrice(record){
 const price=number(record.price);
 if(record.type==='sale')return price;
 if(record.type!=='rent')return null;
 const management=number(record.management_fee_yen),mandatory=number(record.mandatory_fee_yen);
 return price==null||management==null||mandatory==null?null:(Math.round(price*10000)+management+mandatory)/10000;
}
function evaluateConditions(record,filters,origins,distance){
 const failures=[],unknown=[];
 for(const name of ['maxPrice','minArea','maxAge','maxCommute'])if(filters[name]!=null&&filters[name]!==''&&number(filters[name])==null)failures.push('数値条件が不正');
 if(filters.type&&record.type!==filters.type)failures.push('種別');
 const price=number(filters.maxPrice);
 if(price!=null){
  const total=monthlyPrice(record);
  if(!filters.type||total==null)unknown.push('月額総額・価格');
  else if(total>price)failures.push('価格条件外');
 }
 for(const [field,filter,minimum,label] of [['area','minArea',true,'面積'],['age','maxAge',false,'築年数']]){
  const limit=number(filters[filter]);
  if(limit==null)continue;
  const value=number(record[field]);
  if(value==null)unknown.push(label);
  else if(minimum?value<limit:value>limit)failures.push(label+'条件外');
 }
 if(filters.layout){if(!record.layout)unknown.push('間取り');else if(record.layout!==filters.layout)failures.push('間取り条件外');}
 for(const field of ['pets','parking'])if(filters[field]){
  const label=field==='pets'?'ペット可':'駐車場';
  if(record[field]==null)unknown.push(label);else if(record[field]!==true)failures.push(label);
 }
 if(filters.insideOnly){
  if(record.geo_precision!=='exact'||!Number.isFinite(record.lat)||!Number.isFinite(record.lon))unknown.push('位置');
  else if(origins.some(origin=>distance(record,origin)>origin.radius))failures.push('指定半径外');
 }
 const commute=number(filters.maxCommute);
 if(commute!=null){
  const routes=record.commute||[];
  if(routes.length!==origins.length||routes.some(route=>!route||!Number.isFinite(route.duration_seconds)))unknown.push('通勤時間');
  else if(routes.some(route=>route.duration_seconds>commute*60))failures.push('通勤時間条件外');
 }
 return {failures,unknown,match:failures.length===0&&(filters.includeUnknown||unknown.length===0)};
}
function evaluate(record,filters,origins,distance,now=Date.now()){
 const result=evaluateConditions(record,filters,origins,distance);
 if(!fresh(record,now)){result.failures.unshift('確認日時・募集状況');result.match=false;}
 return result;
}
const api={number,fresh,safeUrl,monthlyPrice,evaluate,evaluateConditions,MAX_AGE_MS};
if(typeof module==='object'&&module.exports)module.exports=api;
else root.propertyCore=api;
})(typeof window==='object'?window:globalThis);
