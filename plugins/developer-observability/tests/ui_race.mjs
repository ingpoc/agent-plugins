import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
class Element{
  constructor(){this.value='';this.children=[];this.hidden=false;this.textContent='';this.classList={add(){},remove(){}};}
  replaceChildren(...children){this.children=children;}
  append(...children){this.children.push(...children);}
  setAttribute(name,value){this[name]=value;}
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

const timelineHost=app.ontoolresult({structuredContent:{project_id:'test-project',snapshot_id:'timeline',generated_at:'2026-10-24T12:00:00Z',freshness:'fresh',coverage:{},issues:[{issue_id:'connection',headline:'Connection dropped',status:'active'}],total:1}});
await new Promise(resolve=>setTimeout(resolve,0));
pending.at(-1).resolve({isError:true,structuredContent:{error:'unsupported_recommendation_scope'}});
await timelineHost;
const inspectRequest=get('issues').children[0].onclick();
pending.at(-1).resolve({structuredContent:{snapshot_id:'timeline',generated_at:'2026-10-24T12:00:00Z',freshness:'fresh',coverage:{},issue:{headline:'Connection dropped'},observed:'Repeated connection drops',suggested_investigation:'Inspect connection events',checked_outcomes:{confirmed_recovery_count:0,reported_recovery_count:0,provenance:'fixture'},acceptance_status:'not_assessed',evidence:{timeline:[...Array.from({length:65},()=>({event_at:'unknown',event_type:'untimed event',outcome:'failure',evidence:{}})),{event_at:'2026-10-24T12:00:03.000Z',received_at:'2026-10-24T12:01:00Z',event_type:'connection_closed',outcome:'failure',evidence:{}},{event_at:'2026-10-24T12:00:01.000Z',received_at:'2026-10-24T12:00:02Z',event_type:'connection_opened',outcome:'failure',evidence:{}},{event_at:'unknown',received_at:'2026-10-24T12:00:02.000Z',event_type:'reconnect_attempt',outcome:'failure',evidence:{}}],timeline_total:70,timeline_truncated:true,occurrences:{failed_attempt_count:70,failure_attempt_times:['2026-10-24T12:00:01Z'],failure_times_complete:true,recovery_count:0,recovery_times:[],recovery_times_complete:true,count_scope:'snapshot history'}}}});
await inspectRequest;
const visible=node=>[node.textContent??'',...(node.children??[]).map(visible)].join('\n');
const timelineText=visible(get('detail'));
assert.ok(timelineText.includes('Occurrence timeline'));
assert.ok(timelineText.includes('Showing the latest 64 of 70 recorded events.'));
assert.ok(timelineText.includes('failure timestamps complete'));
assert.ok(timelineText.includes('Received time'));
assert.ok(timelineText.indexOf('Time unavailable')<timelineText.indexOf('connection opened'));
assert.ok(timelineText.indexOf('connection opened')<timelineText.indexOf('reconnect attempt'));
assert.ok(timelineText.indexOf('reconnect attempt')<timelineText.indexOf('connection closed'));
console.log('PASS issue timeline sorts event times and labels received-time fallback and partial coverage');
