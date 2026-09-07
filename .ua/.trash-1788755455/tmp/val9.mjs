import fs from 'fs';
const ext=['app/config.py','benchmarks/reports.py','deliverables/SCOPE-DHBG1.md','deliverables/SPEC-DHBG1.md','deliverables/MODULEMAP-DHBG1.md','deliverables/ARCH-DHBG1.md','deliverables/WBS-DHBG1.md','deliverables/adr/ADR-001-modular-monolith.md','deliverables/adr/ADR-002-sqlite-provenance.md','deliverables/adr/ADR-003-bounded-ai-rerank.md','deliverables/adr/ADR-004-formula-preserving-export.md','deliverables/adr/ADR-005-network-port-3000.md','app/main.py'];
for(const f of ext) if(!fs.existsSync('E:/Desktop/DH-company/'+f)) console.log('MISSING FILE ON DISK:',f);
let tn=0,te=0;
for(const k of [1,2]){
  const d=JSON.parse(fs.readFileSync(`E:/Desktop/DH-company/.ua/intermediate/batch-9-part-${k}.json`,'utf8'));
  tn+=d.nodes.length; te+=d.edges.length;
  const ids=new Set(d.nodes.map(n=>n.id));
  const okExt=new Set(ext.flatMap(f=>['file:'+f,'document:'+f]));
  let bad=0;
  for(const e of d.edges){
    if(!ids.has(e.source)&&!okExt.has(e.source)){console.log('BAD SRC',e.source);bad++;}
    if(!ids.has(e.target)&&!okExt.has(e.target)){console.log('BAD TGT',e.target);bad++;}
    if(e.source===e.target){console.log('SELF',e.source);bad++;}
  }
  const seen=new Set();
  for(const n of d.nodes){
    if(seen.has(n.id)){console.log('DUP',n.id);bad++;} seen.add(n.id);
    if(!n.summary||!n.tags?.length||!n.complexity){console.log('MISSING',n.id);bad++;}
  }
  console.log(`part${k}: nodes=${d.nodes.length} edges=${d.edges.length} ${bad===0?'VALID':'ISSUES='+bad}`);
}
console.log('TOTAL nodes',tn,'edges',te);
