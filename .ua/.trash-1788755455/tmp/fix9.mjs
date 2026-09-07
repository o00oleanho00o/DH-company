import fs from 'fs';
const p='E:/Desktop/DH-company/.ua/intermediate/batch-9-part-1.json';
const d=JSON.parse(fs.readFileSync(p,'utf8'));
const fns=d.nodes.filter(n=>n.type==='function'&&n.filePath==='app/static/app.js');
const contains=fns.map(n=>({source:'file:app/static/app.js',target:n.id,type:'contains',direction:'forward',weight:1.0}));
d.edges=[...contains,...d.edges];
fs.writeFileSync(p,JSON.stringify(d,null,2)+'\n');
console.log('nodes',d.nodes.length,'edges',d.edges.length,'fnNodes',fns.length);
const ids=new Set(d.nodes.map(n=>n.id));
const ext=new Set(['file:app/main.py']);
let bad=0;
for(const e of d.edges){
  if(!ids.has(e.source)){console.log('BAD SRC',e.source);bad++;}
  if(!ids.has(e.target)&&!ext.has(e.target)){console.log('BAD TGT',e.target);bad++;}
  if(e.source===e.target){console.log('SELF',e.source);bad++;}
}
const seen=new Set();
for(const n of d.nodes){
  if(seen.has(n.id)){console.log('DUP',n.id);bad++;} seen.add(n.id);
  if(!n.summary||!n.tags||!n.tags.length||!n.complexity||!n.type||!n.name){console.log('MISSING',n.id);bad++;}
  if(n.tags.length<3||n.tags.length>5){console.log('TAGCOUNT',n.id,n.tags.length);}
}
console.log(bad===0?'PART1 VALID':'PART1 ISSUES='+bad);
