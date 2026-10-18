const assert=require('node:assert/strict');
const {test}=require('node:test');
const core=require('../property_core.js');
const now=Date.parse('2026-10-18T05:00:00Z');
const record={type:'rent',price:7,management_fee_yen:5000,mandatory_fee_yen:1000,area:25,age:0,layout:'1K',pets:null,parking:true,lat:35,lon:139,geo_precision:'exact',url:'https://example.com/test-only',availability_status:'confirmed',last_verified_at:'2026-10-18T04:59:00Z'};
const origins=[{lat:35,lon:139,radius:3}];
const check=(overrides={},filters={},distance=()=>0)=>core.evaluate({...record,...overrides},filters,origins,distance,now);

test('monthly budget includes management and mandatory fees, including its boundary',()=>{
 assert.equal(core.monthlyPrice(record),7.6);
 assert.equal(check({}, {type:'rent',maxPrice:7.6}).match,true);
 assert.equal(check({}, {type:'rent',maxPrice:7.59}).match,false);
 assert.equal(check({mandatory_fee_yen:null},{type:'rent',maxPrice:8}).match,false);
 assert.equal(check({management_fee_yen:null},{type:'rent',maxPrice:8}).match,false);
 assert.equal(check({type:'sale',price:3000},{type:'sale',maxPrice:3000}).match,true);
 assert.equal(check({price:0.1,management_fee_yen:2000,mandatory_fee_yen:0},{type:'rent',maxPrice:0.3}).match,true);
});
test('unknown values are separate from nonmatches',()=>{
 assert.deepEqual(check({area:null},{minArea:20}).unknown,['面積']);
 assert.equal(check({area:null},{minArea:20}).match,false);
 assert.equal(check({area:null},{minArea:20,includeUnknown:true}).match,true);
 assert.equal(check({area:10},{minArea:20,includeUnknown:true}).match,false);
 assert.equal(check({}, {pets:true}).match,false);
 assert.equal(check({pets:false},{pets:true,includeUnknown:true}).match,false);
});
test('zero age and zero fees are known values; malformed values do not pass',()=>{
 assert.equal(check({}, {maxAge:0}).match,true);
 assert.equal(check({age:1},{maxAge:0}).match,false);
 for(const value of [null,'',true,NaN,Infinity,-1,[],{}])assert.equal(core.number(value),null);
 assert.equal(core.number(0),0);
 assert.equal(check({}, {maxPrice:-1}).match,false);
});
test('expired, ended, future and missing verification never count as current',()=>{
 for(const overrides of [{last_verified_at:null},{last_verified_at:'2026-10-18T03:59:59Z'},{last_verified_at:'2026-10-18T05:00:01Z'},{last_verified_at:'2026-10-18T04:59:00'},{availability_status:'ended'}])assert.equal(check(overrides,{includeUnknown:true}).match,false);
 assert.equal(check({last_verified_at:'2026-10-18T04:00:00Z'}).match,true);
});
test('source links cannot use executable schemes or embedded credentials',()=>{
 for(const url of ['javascript:alert(1)','data:text/html,test','https://user:secret@example.com/','https://example.com:bad/'])assert.equal(core.safeUrl(url),false);
 assert.equal(check({url:'http://example.com/'},{includeUnknown:true}).match,false);
});
test('approximate positions cannot pass strict distance conditions',()=>{
 assert.equal(check({geo_precision:'approximate'},{insideOnly:true}).match,false);
 assert.equal(check({}, {insideOnly:true},()=>3).match,true);
 assert.equal(check({}, {insideOnly:true},()=>3.01).match,false);
});
test('commute limits use route duration in seconds and require every selected origin',()=>{
 assert.equal(check({}, {maxCommute:30}).match,false);
 assert.equal(check({commute:[{duration_seconds:1800}]},{maxCommute:30}).match,true);
 assert.equal(check({commute:[{duration_seconds:1801}]},{maxCommute:30}).match,false);
 assert.equal(check({commute:[{duration_seconds:null}]},{maxCommute:30}).match,false);
});
