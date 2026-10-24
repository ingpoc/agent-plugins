import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
class Element{
  constructor(){this.value='';this.children=[];this.hidden=false;this.textContent='';this.classList={add(){},remove(){}};}
  replaceChildren(...children){this.children=children;}
  append(...children){this.children.push(...children);}
  get firstChild(){return this.children[0];}
}
const elements=new Map();
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const pending=[];
let app;
class App{
  constructor(){app=this;}
  connect(){return Promise.resolve();}
  callServerTool(params){return new Promise(resolve=>pending.push({params,resolve}));}
}
const script=(await readFile(new URL('../ui/app.js',import.meta.url),'utf8')).replace("import {App} from '@modelcontextprotocol/ext-apps';",'');
vm.runInNewContext(script,{App,document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[]},console});
get('project').value='A';const oldRequest=get('project').onchange();
get('decision-result').hidden=false;
get('detail').children=[{textContent:'old project secret detail'}];
get('project').value='B';const newRequest=get('project').onchange();
assert.equal(get('decision-result').hidden,true);
assert.ok(!JSON.stringify(get('detail').children).includes('old project secret detail'));
const issueResult=project=>({structuredContent:{snapshot_id:project,generated_at:project,freshness:'fresh',coverage:{},issues:[{issue_id:project,headline:project,status:'active'}],total:1}});
pending.find(p=>p.params.arguments.project_id==='B').resolve(issueResult('B'));
await new Promise(resolve=>setTimeout(resolve,0));
pending.find(p=>p.params.name==='observability_recommendation').resolve({isError:true,structuredContent:{error:'unsupported_recommendation_scope'}});
await newRequest;
pending.find(p=>p.params.arguments.project_id==='A').resolve(issueResult('A'));
await oldRequest;
assert.ok(get('health').textContent.includes(' · B · '));
assert.ok(JSON.stringify(get('issues').children).includes('"textContent":"B"'));
assert.ok(!JSON.stringify(get('issues').children).includes('"textContent":"A"'));
console.log('PASS project switch clears scoped panels and rejects delayed responses');

get('project').value='A';const pendingA=get('project').onchange();
const pendingCall=pending.at(-1);
get('detail').children=[{textContent:'old host project detail'}];
const hostResult=app.ontoolresult({structuredContent:{...issueResult('B').structuredContent,project_id:'B'}});
assert.ok(!JSON.stringify(get('detail').children).includes('old host project detail'));
await new Promise(resolve=>setTimeout(resolve,0));
pending.at(-1).resolve({isError:true,structuredContent:{error:'unsupported_recommendation_scope'}});
await hostResult;
pendingCall.resolve(issueResult('A'));
await pendingA;
assert.ok(get('health').textContent.includes(' · B · '));
assert.equal(get('project').value,'B');
console.log('PASS host-delivered project switch rejects delayed old-project response');
