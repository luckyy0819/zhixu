/* Convert native V8 coverage with pinned v8-to-istanbul; third-party vendor is reported separately. */
const fs=require('node:fs'),path=require('node:path'),convert=require('v8-to-istanbul');
const root=path.resolve(__dirname,'../..'),out=path.join(root,'docs/engineering/audit/AI-DEV/2026-11-25-p0.2');
(async()=>{
 const merged={};
 for(const name of fs.readdirSync(path.join(out,'js-coverage-final'))){
  for(const result of JSON.parse(fs.readFileSync(path.join(out,'js-coverage-final',name))).result){
   for(const target of ['app.js','client-core.js']){
    const absolute=path.join(root,target).replaceAll('\\','/');
    if(result.url!==absolute&&!result.url.endsWith('/'+target))continue;
    if(result.url.includes('node_modules')||result.url.includes('/vendor/'))continue;
    const item=convert(absolute,0,{source:fs.readFileSync(absolute,'utf8')});await item.load();item.applyCoverage(result.functions);
    for(const [file,data] of Object.entries(item.toIstanbul())){
     if(!merged[file])merged[file]=data;
     else{for(const key of Object.keys(data.s))merged[file].s[key]=Math.max(merged[file].s[key]||0,data.s[key]);}
    }
   }
  }
 }
 const files={};let hit=0,total=0;
 for(const target of ['app.js','client-core.js']){
  const entry=Object.entries(merged).find(([file])=>file.replaceAll('\\','/').endsWith('/'+target));
  if(!entry)throw new Error('Missing production JS coverage: '+target);
  const data=entry[1],h=Object.values(data.s).filter(n=>n>0).length,t=Object.keys(data.s).length;
  files[target]={hit:h,total:t,percent:h/t*100};hit+=h;total+=t;
 }
 const report={kind:'V8 converted statement coverage (line maps)',files,hit,total,percent:hit/total*100,vendor:{files:['vendor/marked.js','vendor/purify.js'],classification:'pinned third-party, excluded from first-party quality denominator; npm audit recorded'},branches:'raw V8 ranges retained; no branch threshold claim'};
 fs.writeFileSync(path.join(out,'js-istanbul.json'),JSON.stringify(merged));fs.writeFileSync(path.join(out,'js-summary.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
})();
