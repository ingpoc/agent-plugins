import {App} from '@modelcontextprotocol/ext-apps';
const app = new App({name:'Developer observability',version:'0.1.2'});
const $ = id => document.getElementById(id);
let snapshot, cursor, selected, generation=0, detailGeneration=0;
const text = (tag,value,cls) => {const el=document.createElement(tag);el.textContent=value??'Unknown';if(cls)el.className=cls;return el;};
function health(data){snapshot=data.snapshot_id;$('health').textContent=`${data.freshness==='fresh'?'Fresh snapshot':data.freshness==='stale'?'Stale snapshot — refresh collector before acting':'Freshness unknown'} · ${data.generated_at??'Time unavailable'} · Coverage: ${Object.entries(data.coverage??{}).map(([k,v])=>`${k} ${v}`).join(', ')}`;}
async function call(name,args){$('error').textContent='';const result=await app.callServerTool({name,arguments:args});const data=result.structuredContent??JSON.parse(result.content.find(c=>c.type==='text').text);if(result.isError||data.error)throw new Error(data.error??'Request failed');return data;}
async function run(fn){let request;try{const pending=fn();request=generation;await pending;}catch(e){if(request===undefined||request===generation)$('error').textContent=e.message;}}
function reset(){cursor=null;selected=null;snapshot=null;detailGeneration++;$('detail').replaceChildren(text('h2','Investigation detail'),text('p','Select an observation.','muted'));$('decision-result').hidden=true;$('decision-result').textContent='';$('decision-state').value='';$('recommendations').replaceChildren(text('p','Loading this project…','muted'));$('evaluate').hidden=true;$('issues').replaceChildren();$('more').hidden=true;$('count').textContent='';$('error').textContent='';}
function projects(rows){const value=$('project').value;$('project').replaceChildren(text('option','Choose a project'));$('project').firstChild.value='';for(const row of rows){const option=text('option',`${row.label} (${row.issue_count})`);option.value=row.project_id;$('project').append(option);}$('project').value=value;}
function list(data,append=false){health(data);cursor=data.next_cursor;$('more').hidden=!cursor;$('count').textContent=`${data.total??0}`;if(!append)$('issues').replaceChildren();if(!data.issues?.length&&!append)$('issues').append(text('p','No matching observations. Coverage may be incomplete.','muted'));for(const issue of data.issues??[]){const button=document.createElement('button');button.className='issue';button.append(text('span',issue.status,`badge ${issue.status==='active'?'active':''}`),text('strong',issue.headline),text('small',`${issue.source} · ${issue.failed_attempts} failed attempts · ${issue.evidence_count} evidence references`));button.onclick=()=>run(async()=>{selected=issue.issue_id;document.querySelectorAll('.issue').forEach(x=>x.classList.remove('selected'));button.classList.add('selected');await inspect();});$('issues').append(button);}}
function occurrenceTimeline(evidence={}) {
 const occurrences=evidence.occurrences??{};
 const supplied=Array.isArray(evidence.timeline)?evidence.timeline:[];
 const failureTimes=Array.isArray(occurrences.failure_attempt_times)?occurrences.failure_attempt_times:[];
 const usesFailureTimes=supplied.length===0&&failureTimes.length>0;
 const events=supplied.length?supplied:failureTimes.map(event_at=>({event_at,event_type:'failed attempt',outcome:'failure'}));
 const rows=events.map((event,index)=>{
  const eventAt=typeof event?.event_at==='string'&&Number.isFinite(Date.parse(event.event_at))?new Date(event.event_at):null;
  const receivedAt=typeof event?.received_at==='string'&&Number.isFinite(Date.parse(event.received_at))?new Date(event.received_at):null;
  return {event,index,date:eventAt??receivedAt,dateValue:eventAt?event.event_at:receivedAt?event.received_at:null,timeSource:eventAt?'Event time':receivedAt?'Received time':'Time unavailable'};
 }).sort((a,b)=>(a.date?.getTime()??-Infinity)-(b.date?.getTime()??-Infinity)||a.index-b.index).slice(-64);
 const reportedTotal=Number.isInteger(evidence.timeline_total)?Math.max(evidence.timeline_total,events.length):events.length;
 const total=usesFailureTimes?Math.max(reportedTotal,Number.isInteger(occurrences.failed_attempt_count)?occurrences.failed_attempt_count:failureTimes.length):reportedTotal;
 const incomplete=usesFailureTimes?occurrences.failure_times_complete!==true||total>rows.length:evidence.timeline_truncated===true||total>rows.length;
 const panel=document.createElement('section');
 panel.append(text('h3','Occurrence timeline'));
 panel.append(text('p',incomplete&&total>rows.length?`Showing the latest ${rows.length} of ${total} recorded events.`:incomplete?`${rows.length} recent event records; the timeline is marked incomplete.`:`Showing all ${rows.length} available event records.`,'muted'));
 const failureCount=Number.isInteger(occurrences.failed_attempt_count)?`${occurrences.failed_attempt_count} failed attempts`:'failed-attempt count unavailable';
 const timestampState=occurrences.failure_times_complete===true?'complete':failureTimes.length?'partial':'unavailable';
 const recoveryCount=Number.isInteger(occurrences.recovery_count)?`${occurrences.recovery_count} confirmed recoveries`:'confirmed-recovery count unavailable';
 panel.append(text('p',`Snapshot history: ${failureCount}; failure timestamps ${timestampState}; ${recoveryCount}. Times use ${Intl.DateTimeFormat().resolvedOptions().timeZone}.`,'muted'));
 if(!rows.length){panel.append(text('p','No timestamped event records are available for this observation.','muted'));return panel;}
 const list=document.createElement('ol');
 list.className='timeline';
 list.tabIndex=0;
 list.setAttribute('aria-label','Issue events in chronological order. Scroll to review earlier events.');
 const format=new Intl.DateTimeFormat(undefined,{year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3,timeZoneName:'short'});
 for(const row of rows){
  const event=row.event??{};
  const item=document.createElement('li');
  item.className=event.outcome==='failure'?'failure':event.outcome==='recovery'?'recovery':'';
  const at=document.createElement('div');
  at.className='event-time';
  at.append(text('small',row.timeSource,'muted'));
  if(row.date){const time=text('time',format.format(row.date));time.dateTime=row.dateValue;at.append(time);}else at.append(text('span','Time unavailable','muted'));
  const type=typeof event.event_type==='string'?event.event_type.replaceAll('_',' '):'recorded event';
  const outcome=event.outcome==='failure'?'Failure':event.outcome==='recovery'?'Recovery':'Recorded event';
  item.append(at,text('strong',`${outcome} · ${type}`));
  list.append(item);
 }
 panel.append(list);
 return panel;
}
async function inspect(evidenceCursor){const request=generation, detailRequest=++detailGeneration;const d=await call('observability_inspect_issue',{project_id:$('project').value,issue_id:selected,cursor:evidenceCursor??null});if(request!==generation||detailRequest!==detailGeneration)return;health(d);const box=$('detail');box.replaceChildren(text('h2',d.issue.headline));for(const [heading,value]of [['Observed',d.observed],['Suggested investigation',d.suggested_investigation],['Checked outcomes',`${d.checked_outcomes.confirmed_recovery_count} linked recoveries · ${d.checked_outcomes.reported_recovery_count} reported recoveries. ${d.checked_outcomes.provenance}`],['Repair acceptance',d.acceptance_status]])box.append(text('h3',heading),text('p',value));box.append(occurrenceTimeline(d.evidence));const details=document.createElement('details');details.append(text('summary','Redacted recorded evidence'),text('pre',JSON.stringify(d.evidence,null,2)));if(d.next_cursor){const more=text('button','Next evidence page');more.onclick=()=>run(()=>inspect(d.next_cursor));details.append(more);}box.append(details);}
async function recommendations(operation='read'){const request=generation;const result=await app.callServerTool({name:'observability_recommendation',arguments:{project_id:$('project').value,operation}});const data=result.structuredContent??JSON.parse(result.content.find(x=>x.type==='text').text);if(request!==generation)return;const box=$('recommendations');box.replaceChildren();$('evaluate').hidden=data.error==='unsupported_recommendation_scope';if(data.error){box.append(text('p',data.error==='unsupported_recommendation_scope'?'This project has rule-based investigation leads in each observation. The Decider recommendation pilot currently covers Context Ledger.':data.error,'muted'));return;}if(!data.recommendations.length)box.append(text('p','No saved recommendation for this scope.','muted'));for(const r of data.recommendations){box.append(text('p',r.action),text('small',`${r.status} · ${r.actionable?'Actionable investigation':'Review required'} · Acceptance: ${r.acceptance_status}`),text('p',r.completion_check,'muted'));if(r.decision.confidence!=null)box.append(text('p',`${r.decision.low_confidence?'Uncertain choice':'Choice confidence'}: ${Math.round(r.decision.confidence*100)}%`));}if(operation==='propose')box.append(text('p',data.note,'muted'));}
async function refresh(){const request=++generation;reset();const p=$('project').value;if(!p){const d=await call('observability_projects',{});if(request!==generation)return;projects(d.projects);health(d);return;}const d=await call('observability_issues',{project_id:p,status:$('status').value});if(request!==generation)return;list(d);await recommendations();}
$('project').onchange=()=>run(refresh);$('status').onchange=()=>run(refresh);$('refresh').onclick=()=>run(refresh);$('more').onclick=()=>run(async()=>{const request=generation;$('more').disabled=true;try{const d=await call('observability_issues',{project_id:$('project').value,status:$('status').value,cursor});if(request===generation)list(d,true);}finally{$('more').disabled=false;}});$('evaluate').onclick=()=>run(()=>recommendations('propose'));
$('decide').onclick=()=>run(async()=>{if(!$('project').value)throw new Error('Choose a project first');const request=generation;$('decide').disabled=true;try{const d=await call('observability_decide',{project_id:$('project').value,snapshot_id:snapshot,state:$('decision-state').value,question:JSON.parse($('decision-question').value)});if(request!==generation)return;$('decision-result').hidden=false;$('decision-result').textContent=JSON.stringify(d,null,2);}finally{$('decide').disabled=false;}});
app.ontoolresult=result=>{return run(async()=>{generation++;reset();const d=result.structuredContent??JSON.parse(result.content?.find(x=>x.type==='text')?.text??'{}');if(d.error)throw new Error(d.error);if(d.projects)projects(d.projects);if(d.project_id)$('project').value=d.project_id;health(d);if(d.issues){list(d);await recommendations();}});};
app.connect().catch(e=>{$('health').textContent='Waiting for an MCP Apps host.';$('error').textContent=e.message;});
