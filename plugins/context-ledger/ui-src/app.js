(function(){
"use strict";
let NOW = Date.now();
const D = (s)=>Date.parse(s+"+05:30"); /* IST wall time -> epoch ms */
const DAY = 864e5;
const $ = (s,r=document)=>r.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
/* DOM builder: text always via textContent (stored ledger text is untrusted). */
function h(tag, attrs, ...kids){
  const el = document.createElement(tag);
  if(attrs) for(const k in attrs){const v=attrs[k]; if(v==null||v===false) continue;
    if(k==="class") el.className=v; else if(k.startsWith("on")) el.addEventListener(k.slice(2),v); else if(k==="text") el.textContent=v; else el.setAttribute(k, v===true?"":v);}
  for(const c of kids.flat()){ if(c==null||c===false) continue; el.append(c.nodeType?c:document.createTextNode(String(c))); }
  return el;
}
function s(tag, attrs, ...kids){
  const el = document.createElementNS(SVGNS, tag);
  if(attrs) for(const k in attrs){const v=attrs[k]; if(v==null) continue; if(k.startsWith("on")) el.addEventListener(k.slice(2),v); else el.setAttribute(k,v);}
  for(const c of kids.flat()){ if(c==null) continue; el.append(c.nodeType?c:document.createTextNode(String(c))); }
  return el;
}
const fDay = new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",day:"2-digit",month:"short"});
const fTime = new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",hour12:false});
const fDayLong = new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",weekday:"short",day:"2-digit",month:"short"});
const dayStr=t=>fDay.format(t), timeStr=t=>fTime.format(t), stamp=t=>fDay.format(t)+" "+fTime.format(t)+" IST";
const istKey=t=>new Date(t+5.5*3600e3).toISOString().slice(0,10);
const pct=x=>x==null?"–":Math.round(x*100)+"%";
const OUT = {helped:{label:"helped",cls:"k-ok",badge:"ok"},neutral:{label:"unmarked",cls:"k-neu",badge:""},not_helped:{label:"not helped",cls:"k-nh",badge:"nh"},misled:{label:"misled",cls:"k-mis",badge:"mis"}};
const OUTKEYS=["helped","neutral","not_helped","misled"];

/* ---------- bridge: MCP App host (postMessage JSON-RPC) or the Mini localhost page (fetch) ---------- */
const EMBED = window.parent!==window;
const REQUEST_MS = 8000;
let rpcId=1; const pending=new Map();
function post(m){ parent.postMessage(m,"*"); }
function rpc(method,params){ const id=rpcId++; return new Promise((res,rej)=>{ const t=setTimeout(()=>{ if(pending.delete(id)) rej(new Error("TIMEOUT")); },REQUEST_MS); pending.set(id,{res:v=>{clearTimeout(t);res(v);},rej:e=>{clearTimeout(t);rej(e);}}); post({jsonrpc:"2.0",id,method,params}); }); }
function notify(method,params){ post({jsonrpc:"2.0",method,params:params||{}}); }
function unwrap(p){ if(!p||p.ok!==true) throw new Error((p&&p.error&&p.error.code)||"UNAVAILABLE"); return p.data; }
function payloadOf(r){ if(!r) return null; if(r.structuredContent) return r.structuredContent; const t=r.content&&r.content[0]&&r.content[0].text; try{ return t?JSON.parse(t):null; }catch(e){ return null; } }
async function api(name,args){
  args=args||{};
  if(EMBED){ let last; for(let i=0;i<2;i++){ try{ return unwrap(payloadOf(await rpc("tools/call",{name:"ledger_"+name,arguments:args}))); }catch(e){ last=e; if(e.message!=="TIMEOUT") break; } } throw last; }
  const r=await fetch("/api/"+name+"?a="+encodeURIComponent(JSON.stringify(args)),{cache:"no-store"});
  return unwrap(await r.json());
}
async function pages(name,args,cap){ let cur=0, items=[], total=0; for(let i=0;i<cap&&cur!=null;i++){ const d=await api(name,Object.assign({},args,{cursor:cur})); items=items.concat(d.items); total=d.total; cur=d.next_cursor; } return {items,total,partial:cur!=null}; }
if(EMBED) window.addEventListener("message",ev=>{ if(ev.source!==parent) return; const m=ev.data; if(!m||m.jsonrpc!=="2.0") return;
  if(m.id!=null&&m.method==null&&pending.has(m.id)){ const w=pending.get(m.id); pending.delete(m.id); m.error?w.rej(new Error(m.error.message||"UNAVAILABLE")):w.res(m.result); return; }
  if(m.method==="ui/resource-teardown"&&m.id!=null) post({jsonrpc:"2.0",id:m.id,result:{}}); });

/* ---------- real ledger data -> view model ---------- */
const sid=id=>id?id.slice(0,8):"–";
const OKIND={helped:"helped",unmarked:"neutral",not_helped:"not_helped",misled:"misled"};
const TIER={trusted:{label:"trusted",badge:"ok",title:"3+ helped outcomes, failure rate 10% or less"},user_decision:{label:"user decision",badge:"acc",title:"Guru's attested decision; ranked above hints"},hint:{label:"hint",badge:"",title:"Not yet trusted: fewer than 3 helped outcomes or more than 10% failures"}};
let DECISIONS=[], EVENTS=[], RAW=[], AGENTS=[], STATS=null, ENTS=[], LOADED=false;
const byId={};
const WEEKS=8, wkStart=i=>NOW-(WEEKS-i)*7*DAY;
function ingest(cards,raw,stats){
  NOW=Date.now(); STATS=stats; DECISIONS=cards.map(c=>({id:c.id,sid:sid(c.id),d:c.d,why:c.why||"",type:c.type,entity:(c.entities||[])[0]||"",entities:c.entities||[],
    rule:c.rule||null,override:c.exception||null,overrideBy:c.override_by||null,approver:c.approver||null,approval:c.approval,by:c.by||"unknown",at:c.at,t:Date.parse(c.at),
    prec:c.cites||[],citedByIds:c.cited_by||[],crit:c.crit||null,context:c.context||null,state:c.state||null,tier:c.tier||"hint",lifecycle:c.lifecycle,
    confScore:c.conf.score,succ:c.conf.ok,nConf:c.conf.n,failConf:c.conf.fail,fixes:c.fixes||null,fixedBy:c.fixed_by||null,replacedBy:c.replaced_by||null}));
  for(const k in byId) delete byId[k];
  DECISIONS.forEach(x=>{ byId[x.id]=x; });
  DECISIONS.forEach(x=>{ x.prec=x.prec.filter(id=>byId[id]); x.citedBy=x.citedByIds.filter(id=>byId[id]);
    const nxt=x.fixedBy&&byId[x.fixedBy.id]?{id:x.fixedBy.id,link:x.fixedBy.kind}:(x.replacedBy&&byId[x.replacedBy]?{id:x.replacedBy,link:"supersedes"}:null);
    x.next=nxt; x.prevOf=x.fixes&&byId[x.fixes.id]?{id:x.fixes.id,link:x.fixes.kind}:null;
    x.status=x.lifecycle==="superseded"?"superseded":"active"; });
  RAW=raw.map(e=>({k:e.k,ids:e.ids||[],agent:e.a||"unnamed",t:Date.parse(e.t),task:e.task||null,outcome:e.o?OKIND[e.o]:null,reason:e.r||null,success:!!e.ok,q:e.q||null,hits:e.hits,tool:e.tool}));
  EVENTS=[]; let seq=0;
  RAW.forEach(e=>{ (e.ids.length?e.ids:[null]).forEach(id=>EVENTS.push({k:e.k,id,agent:e.agent,t:e.t,task:e.task,outcome:e.outcome,reason:e.reason,success:e.success,q:e.q,n:seq++})); });
  EVENTS.sort((a,b)=>b.t-a.t);
  const ag=new Set(); RAW.forEach(e=>ag.add(e.agent)); DECISIONS.forEach(x=>ag.add(x.by)); AGENTS=[...ag].sort();
  const en=new Set(); DECISIONS.forEach(x=>x.entities.forEach(e=>en.add(e))); ENTS=[...en].sort();
  DECISIONS.forEach(x=>{
    const ev=EVENTS.filter(e=>e.id===x.id); x.ev=ev;
    x.lookups=ev.filter(e=>e.k==="lookup").length; x.applied=ev.filter(e=>e.k==="applied").length;
    const outs=ev.filter(e=>e.k==="outcome"); x.outs=outs; x.mix={helped:0,neutral:0,not_helped:0,misled:0}; outs.forEach(o=>x.mix[o.outcome]++);
    x.nOut=outs.length; const marked=x.mix.helped+x.mix.not_helped+x.mix.misled; x.helpful=marked?x.mix.helped/marked:null; x.marked=marked;
    x.conf=x.confScore; /* the store's confidence: successes / (successes + failures) over task-tied outcomes */
    const lastFail=outs.find(o=>o.outcome==="misled"||o.outcome==="not_helped");
    x.fail=lastFail?{outcome:lastFail.outcome,reason:lastFail.reason||"No reason recorded.",by:lastFail.agent,t:lastFail.t}:null;
    x.trend=Array.from({length:WEEKS},(_,i)=>{const o=outs.filter(e=>e.t>=wkStart(i)&&e.t<wkStart(i+1)&&e.outcome!=="neutral");return o.length?o.filter(e=>e.outcome==="helped").length/o.length:null;});
    x.lastUsed=ev.length?ev[0].t:x.t;
    x.agents=new Set([x.by,...ev.map(e=>e.agent)]);
  });
  LOADED=true;
}

/* ---------- state ---------- */
const st={qIds:null,q:"",type:"",entity:"",bot:"",date:"",from:"",to:"",outcome:"",status:"",sort:"rank",sel:null,trail:[],open:false,tab:"decisions",actAgent:"",actKind:"",actLimit:40,oSort:"nOut",oDir:-1};
let filtered=[];
const narrow=()=>window.matchMedia("(max-width:759.98px)").matches;

/* ---------- small marks ---------- */
function rankMark(x){
  const one=x.type==="user_decision";
  return h("span",{class:"rank "+(one?"r1":"r2"),title:one?"Rank I · user decision (highest)":"Rank II · agent conclusion"},
    h("b",{"aria-hidden":"true",text:one?"I":"II"}),
    s("svg",{viewBox:"0 0 10 10","aria-hidden":"true"},s("rect",{class:"sq",x:1,y:1,width:8,height:8,rx:1.5})),
    h("span",{class:"vh",text:one?"Rank one, user decision.":"Rank two, agent conclusion."}));
}
function citator(x,w=62){
  const outs=x.outs.slice(0,12).reverse(); const H=18,mid=9;
  const g=s("svg",{width:w,height:H,viewBox:`0 0 ${w} ${H}`,"aria-hidden":"true"});
  g.append(s("line",{x1:0,x2:w,y1:mid+.5,y2:mid+.5,stroke:"var(--rule)","stroke-width":1}));
  const step=w/12; outs.forEach((o,i)=>{const cx=w-(outs.length-i)*step+step/2;
    const spec={helped:[mid-8,mid,"var(--pos)"],neutral:[mid-3,mid,"var(--neutral-mark)"],not_helped:[mid,mid+5,"var(--caution)"],misled:[mid,mid+9,"var(--neg)"]}[o.outcome];
    g.append(s("line",{x1:cx,x2:cx,y1:spec[0],y2:spec[1],stroke:spec[2],"stroke-width":2}));});
  return g;
}
function spark(vals,w=96,hh=22,opts={}){
  const g=s("svg",{width:w,height:hh,viewBox:`0 0 ${w} ${hh}`,"aria-hidden":"true"});
  const pts=vals.map((v,i)=>v==null?null:[2+i*(w-4)/(vals.length-1), hh-2-v*(hh-4)]);
  let d="",pen=false; pts.forEach(p=>{ if(!p){pen=false;return;} d+=(pen?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1); pen=true; });
  g.append(s("line",{x1:0,x2:w,y1:hh-1.5,y2:hh-1.5,stroke:"var(--rule-soft)"}));
  if(d) g.append(s("path",{d,fill:"none",stroke:opts.stroke||"var(--ink-muted)","stroke-width":1.5,"stroke-linejoin":"round"}));
  pts.forEach(p=>{ if(p) g.append(s("circle",{cx:p[0],cy:p[1],r:1.3,fill:opts.stroke||"var(--ink-muted)"})); });
  const last=[...pts].reverse().find(Boolean); if(last) g.append(s("circle",{cx:last[0],cy:last[1],r:2.4,fill:"var(--accent)"}));
  return g;
}
function mixbar(x,w=100){
  const g=s("svg",{width:w,height:10,viewBox:`0 0 ${w} 10`,role:"img","aria-label":OUTKEYS.map(k=>x.mix[k]+" "+OUT[k].label).join(", ")});
  if(!x.nOut){g.append(s("rect",{x:0,y:4,width:w,height:1,fill:"var(--rule)"}));return g;}
  let off=0; const col={helped:"var(--pos)",neutral:"var(--neutral-mark)",not_helped:"var(--caution)",misled:"var(--neg)"};
  OUTKEYS.forEach(k=>{const ww=x.mix[k]/x.nOut*w; if(ww>0){g.append(s("rect",{x:off,y:1,width:Math.max(ww-1,1),height:8,fill:col[k]}));} off+=ww;});
  return g;
}
function highlight(text,q){
  if(!q) return document.createTextNode(text);
  const f=document.createDocumentFragment(); const lt=text.toLowerCase(); let i=0;
  const terms=q.toLowerCase().split(/\s+/).filter(Boolean);
  while(i<text.length){ let best=-1,len=0; for(const t of terms){const j=lt.indexOf(t,i); if(j>=0&&(best<0||j<best)){best=j;len=t.length;}}
    if(best<0){f.append(text.slice(i));break;} f.append(text.slice(i,best)); f.append(h("mark",{text:text.slice(best,best+len)})); i=best+len; }
  return f;
}

/* ---------- filters ---------- */
function fillSelects(){
  const ent=$("#f-entity"); ent.replaceChildren(h("option",{value:"",text:"Any entity"})); ENTS.forEach(e=>ent.append(h("option",{value:e,text:e})));
  const bot=$("#f-bot"); bot.replaceChildren(h("option",{value:"",text:"Any agent"})); AGENTS.forEach(a=>bot.append(h("option",{value:a,text:a})));
}
function applyFilters(){
  const terms=st.q.toLowerCase().split(/\s+/).filter(Boolean);
  if(!LOADED){ $("#resultCount").textContent="Loading the ledger…"; return; }
  let from=null,to=null; if(st.date&&st.date!=="custom") from=NOW-(+st.date)*DAY; if(st.date==="custom"){ if(st.from) from=D(st.from+"T00:00"); if(st.to) to=D(st.to+"T23:59"); }
  filtered=DECISIONS.filter(x=>{
    if(st.type&&x.type!==st.type) return false;
    if(st.entity&&!x.entities.includes(st.entity)) return false;
    if(st.bot&&!x.agents.has(st.bot)) return false;
    if(from&&x.t<from) return false; if(to&&x.t>to) return false;
    if(st.status==="trusted"||st.status==="hint"){ if(x.tier!==st.status) return false; } else if(st.status&&x.status!==st.status) return false;
    if(st.outcome){ const m=x.mix; if(st.outcome==="none"?x.nOut>0:st.outcome==="failed"?(m.misled+m.not_helped)===0:m[st.outcome]===0) return false; }
    if(terms.length&&st.qIds&&!st.qIds.has(x.id)) return false; /* server search: find's own BM25 + relevance floor */
    return true;
  });
  const by={rank:(a,b)=>(a.type===b.type?0:a.type==="user_decision"?-1:1)||(a.status===b.status?0:a.status==="active"?-1:1)||b.t-a.t,
    recent:(a,b)=>b.t-a.t, search:(a,b)=>st.qRank.get(a.id)-st.qRank.get(b.id), helpful:(a,b)=>(b.helpful??-1)-(a.helpful??-1)||b.nOut-a.nOut, used:(a,b)=>b.lookups-a.lookups};
  filtered.sort(by[st.q&&st.qRank&&st.sort==="rank"?"search":st.sort]);
  const nf=["type","entity","bot","date","outcome","status"].filter(k=>st[k]).length;
  $("#fcount").textContent=nf?"("+nf+")":"";
  $("#clearBtn").hidden=!(nf||st.q);
  $("#resultCount").textContent=filtered.length+" of "+DECISIONS.length+" decisions";
  if(!filtered.find(x=>x.id===st.sel) && !narrow()) { if(filtered[0]) select(filtered[0].id,{reset:true,silent:true}); }
  renderList();
}
function renderList(){
  const ul=$("#list"); ul.replaceChildren();
  if(!filtered.length){ ul.append(h("li",{class:"empty"},h("strong",{text:DECISIONS.length?"No decisions match.":"The ledger has no decisions yet."}),DECISIONS.length?"Try fewer words, or clear the filters.":"Saved decisions appear here.")); return; }
  filtered.forEach(x=>{
    const cur=x.id===st.sel;
    const badges=[]; if(x.status==="superseded") badges.push(h("span",{class:"badge sup",text:"superseded"}));
    if(x.mix.misled) badges.push(h("span",{class:"badge mis",text:"misled ×"+x.mix.misled}));
    if(x.mix.not_helped) badges.push(h("span",{class:"badge nh",text:"not helped ×"+x.mix.not_helped}));
    const b=h("button",{class:"row"+(x.status==="superseded"?" superseded":""),type:"button","data-id":x.id,"aria-current":cur?"true":"false",tabindex:cur||(!st.sel&&x===filtered[0])?"0":"-1",
      onclick:()=>{select(x.id,{reset:true}); openDetail();}},
      rankMark(x),
      h("span",{class:"dtext"},highlight(x.d,st.q)),
      h("span",{class:"cit",title:x.marked?`${x.mix.helped} of ${x.marked} marked outcomes helped`:"No helpful marks yet"},citator(x),h("span",{text:x.marked?pct(x.helpful):"n/a"})),
      h("span",{class:"why"},h("i",{text:"why"}),highlight(x.why,st.q)),
      h("span",{class:"meta"},h("span",{class:"id",text:x.sid}),x.entity?h("span",{text:x.entity}):null,h("span",{text:dayStr(x.t)}),h("span",{text:x.conf!=null?"conf "+x.conf.toFixed(2)+" n"+x.nConf:"no outcomes yet"}),x.tier!=="hint"?h("span",{class:"badge "+TIER[x.tier].badge,title:TIER[x.tier].title,text:TIER[x.tier].label}):null,...badges));
    ul.append(h("li",null,b));
  });
}

/* ---------- selection + detail ---------- */
function select(id,{reset=false,push=false,silent=false}={}){
  st.sel=id;
  if(reset) st.trail=[id]; else if(push){ st.trail.push(id); if(st.trail.length>12) st.trail.shift(); }
  document.querySelectorAll(".row").forEach(r=>{const on=r.dataset.id===id; r.setAttribute("aria-current",on?"true":"false"); r.tabIndex=on?0:-1;});
  renderDetail();
  if(!silent){ const r=document.querySelector(`.row[data-id="${id}"]`); if(r) r.scrollIntoView({block:"nearest"}); }
}
function go(id){ if(!byId[id]) return; if(st.tab!=="decisions") setTab("decisions"); select(id,{push:true}); openDetail(true); }
function openDetail(focus=true){
  if(narrow()){ $("#detail").classList.add("open"); st.open=true; $(".listcol").inert=true; $("#detail").removeAttribute("aria-hidden"); }
  if(focus){ const hd=$("#dbody h2"); if(hd) hd.focus({preventScroll:true}); $("#dbody").scrollTop=0; }
}
function closeDetail(){ if(!st.open) return; $("#detail").classList.remove("open"); st.open=false; $(".listcol").inert=false; const r=document.querySelector(`.row[data-id="${st.sel}"]`); if(r) r.focus(); }
function back(){ if(st.trail.length>1){ st.trail.pop(); select(st.trail[st.trail.length-1]); openDetail(true);} else closeDetail(); }

function plink(id,extra){ const x=byId[id]; return h("button",{class:"plink",type:"button",onclick:()=>go(id),title:x.d},h("span",{class:"mono",text:x.sid}),h("span",{text:x.d}),extra||null); }
function chainOf(x){ let root=x; while(root.prevOf) root=byId[root.prevOf.id]; const seq=[root]; let c=root; while(c.next){ c=byId[c.next.id]; seq.push(c);} return seq.length>1?seq:null; }
const ICON={
  x:()=>s("svg",{viewBox:"0 0 16 16",class:"ico","aria-hidden":"true"},s("circle",{cx:8,cy:8,r:5.5,stroke:"var(--ink-faint)"}),s("path",{d:"M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4",stroke:"var(--ink-faint)"})),
  fail:(k)=>s("svg",{viewBox:"0 0 16 16","aria-hidden":"true"},s("path",{d:"M8 2.2 14 13H2z",fill:"none",stroke:k==="misled"?"var(--neg)":"var(--caution)","stroke-width":1.6,"stroke-linejoin":"round"}),s("path",{d:"M8 6.5v3",stroke:k==="misled"?"var(--neg)":"var(--caution)","stroke-width":1.6,"stroke-linecap":"round"}),s("circle",{cx:8,cy:11.3,r:.9,fill:k==="misled"?"var(--neg)":"var(--caution)"})),
  ok:()=>s("svg",{viewBox:"0 0 16 16",class:"ico","aria-hidden":"true"},s("circle",{cx:8,cy:8,r:5.5,stroke:"var(--accent)"}),s("path",{d:"M5.5 8.2l1.8 1.8 3.3-3.6",stroke:"var(--accent)"})),
  down:()=>s("svg",{viewBox:"0 0 10 22",width:10,height:22,"aria-hidden":"true"},s("path",{d:"M5 0v19M1.5 15.5 5 19.5l3.5-4",fill:"none",stroke:"var(--ink-muted)","stroke-width":1.3,"stroke-linecap":"round","stroke-linejoin":"round"}))
};
function renderChain(x){
  const seq=chainOf(x); if(!seq) return null;
  const ol=h("ol",{class:"chain"});
  seq.forEach((n,i)=>{
    const last=i===seq.length-1, here=n.id===x.id;
    ol.append(h("li",null,h("button",{type:"button",class:"cnode "+(last?"fix":"old")+(here?" here":""),onclick:()=>here?null:go(n.id),"aria-current":here?"true":null},
      h("span",{class:"gut"},last?ICON.ok():ICON.x()),
      h("span",null,
        h("span",{class:"k"},h("span",{text:n.sid}),h("span",{text:last?"working decision":"tried"}),h("span",{text:dayStr(n.t)}),here?h("span",{class:"youare",text:"viewing"}):null),
        h("span",{class:"t",text:n.d}),
        h("span",{class:"w"},"why: ",n.why)))));
    if(!last){
      const f=n.fail, o=f?OUT[f.outcome]:null;
      ol.append(h("li",{class:"clink"},h("span",{class:"line"}),h("span")));
      if(f) ol.append(h("li",null,h("button",{type:"button",class:"cnode fail",onclick:()=>{ go(n.id); setTimeout(()=>{const t=$("#sec-out"); if(t) t.scrollIntoView({block:"start"});},30);},title:"Open the failed decision's outcome record"},
        h("span",{class:"gut"},ICON.fail(f.outcome)),
        h("span",null,h("span",{class:"k"},h("span",{class:"badge "+o.badge,text:o.label}),h("span",{text:"reported by "+f.by+" · "+stamp(f.t)})),
          h("span",{class:"t",text:f.reason})))));
      ol.append(h("li",{class:"clink arrow"},ICON.down(),h("span",{class:"lt",text:"superseded by"})));
    }
  });
  return ol;
}
function renderGraph(x,box){
  const W=Math.max(300,Math.floor(box.clientWidth||600));
  const left=[...x.prec.map(id=>({id,rel:"cites"})),...(x.prevOf?[{id:x.prevOf.id,rel:x.prevOf.link,sup:true}]:[])];
  const right=[...x.citedBy.map(id=>({id,rel:"cited by"})),...(x.next?[{id:x.next.id,rel:x.next.link,sup:true}]:[])];
  const cap=4, L=left.slice(0,cap), R=right.slice(0,cap);
  const gap=W<480?14:28, nw=Math.min(210,Math.floor((W-2*gap)/3)), nh=46, vg=10;
  const rows=Math.max(1,L.length,R.length), Hh=24+rows*(nh+vg)+ (left.length>cap||right.length>cap?16:0);
  const svg=s("svg",{class:"graph",viewBox:`0 0 ${W} ${Hh}`,width:W,height:Hh,role:"group","aria-label":`Precedent neighbourhood of ${x.sid}`});
  const cx=(W-nw)/2, colY=n=>i=>24+(rows-n)*(nh+vg)/2+i*(nh+vg);
  svg.append(s("text",{class:"ax",x:0,y:12},"Cites"),s("text",{class:"ax",x:cx,y:12},"This decision"),s("text",{class:"ax",x:W-nw,y:12},"Cited by"));
  const chars=Math.max(8,Math.floor((nw-18)/6.3));
  const cy=24+(rows-1)*(nh+vg)/2;
  function node(id,X,Y,cls,clickable){
    const d=byId[id]; const g=s("g",{class:"node "+cls+(d.status==="superseded"?" dim":""),transform:`translate(${X},${Y})`});
    if(clickable){ g.setAttribute("tabindex","0"); g.setAttribute("role","link"); g.setAttribute("aria-label",`${d.sid}: ${d.d}`);
      g.addEventListener("click",()=>go(id)); g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();go(id);}}); }
    g.append(s("title",null,`${d.sid} · ${d.d}`),s("rect",{width:nw,height:nh,rx:6}));
    g.append(s("rect",{class:d.type==="user_decision"?"sq1":"sq2",x:9,y:10,width:7,height:7,rx:1.2}));
    g.append(s("text",{class:"id",x:22,y:17},d.sid+(d.type==="user_decision"?" · I":" · II")));
    const lab=d.d.length>chars?d.d.slice(0,chars-1).trimEnd()+"…":d.d;
    g.append(s("text",{class:"lbl",x:9,y:35},lab));
    return g;
  }
  const edges=s("g"); svg.append(edges);
  L.forEach((n,i)=>{const y=colY(L.length)(i); edges.append(s("path",{class:"edge"+(n.sup?" sup":""),d:`M${nw} ${y+nh/2} C ${nw+gap} ${y+nh/2}, ${cx-gap} ${cy+nh/2}, ${cx} ${cy+nh/2}`})); svg.append(node(n.id,0,y,"",true));});
  R.forEach((n,i)=>{const y=colY(R.length)(i); edges.append(s("path",{class:"edge"+(n.sup?" sup":""),d:`M${cx+nw} ${cy+nh/2} C ${cx+nw+gap} ${cy+nh/2}, ${W-nw-gap} ${y+nh/2}, ${W-nw} ${y+nh/2}`})); svg.append(node(n.id,W-nw,y,"",true));});
  if(!L.length) svg.append(s("text",{class:"ax",x:0,y:cy+nh/2+4},"none"));
  if(!R.length) svg.append(s("text",{class:"ax",x:W-nw,y:cy+nh/2+4},"none"));
  svg.append(node(x.id,cx,cy,"center",false));
  if(left.length>cap) svg.append(s("text",{class:"ax",x:0,y:Hh-2},`+${left.length-cap} more`));
  if(right.length>cap) svg.append(s("text",{class:"ax",x:W-nw,y:Hh-2},`+${right.length-cap} more`));
  box.replaceChildren(svg);
}
function evRow(e){
  const x=byId[e.id];
  const task=h("b",{class:"mono",text:e.task?sid(e.task):"no task id"});
  const what=e.k==="outcome"?[h("span",{class:OUT[e.outcome].cls,text:OUT[e.outcome].label}),e.reason?" — "+e.reason:""]:e.k==="applied"?["applied in ",task]:[e.q?"found by “"+e.q+"” · ":"opened · ",task];
  return h("li",{class:"ev"},h("time",{text:dayStr(e.t)+" "+timeStr(e.t)}),h("span",{class:"who",text:e.agent}),h("span",{class:"what"},...what));
}
function renderDetail(){
  const box=$("#dbody"); const x=byId[st.sel];
  const crumbs=$("#crumbs"); crumbs.replaceChildren();
  if(!x){ box.replaceChildren(h("div",{class:"placeholder"},h("strong",{text:LOADED?"Select a decision":"Loading the ledger…"}),"Its full trace, precedents and outcomes appear here.")); return; }
  const tr=st.trail.length?st.trail:[x.id]; const show=tr.slice(-4);
  if(tr.length>4) crumbs.append(h("li",null,h("span",{class:"mono",style:"color:var(--ink-faint);padding:0 4px",text:"…"})));
  show.forEach((id,i)=>{ const last=i===show.length-1; crumbs.append(h("li",null,last?h("span",{"aria-current":"page",text:sid(id)}):h("button",{type:"button",title:byId[id].d,onclick:()=>{ st.trail=st.trail.slice(0,st.trail.lastIndexOf(id)+1); select(id); openDetail(true);} ,text:sid(id)}))); });
  const one=x.type==="user_decision";
  const top=h("div",{class:"dtop"},rankMark(x),h("span",{class:"id",text:x.sid}),h("span",{text:one?"user decision · rank I":x.type==="legacy"?"legacy record · rank II":"agent conclusion · rank II"}),
    x.status==="superseded"?h("span",{class:"badge sup",text:x.next?"superseded by "+sid(x.next.id):"superseded"}):h("span",{class:"badge acc",text:"active"}),
    h("span",{class:"badge "+TIER[x.tier].badge,title:TIER[x.tier].title,text:"tier: "+TIER[x.tier].label}),
    x.mix.misled?h("span",{class:"badge mis",text:"misled ×"+x.mix.misled}):null, x.mix.not_helped?h("span",{class:"badge nh",text:"not helped ×"+x.mix.not_helped}):null);
  top.querySelector(".rank").style.cssText="flex-direction:row;padding:0";
  const wrap=h("div",{class:"dinner"+(x.status==="superseded"?" d-sup":"")},top,h("h2",{tabindex:"-1",text:x.d}),
    h("dl",{class:"because"},h("dt",{text:"Why"}),h("dd",{text:x.why})));
  const chain=renderChain(x);
  if(chain) wrap.append(h("section",{class:"sec"},h("h3",null,"Tried, failed, fixed",h("small",{text:chainOf(x).length+" decisions"})),chain));
  const none=t=>h("span",{class:"none",text:t});
  const conf=x.conf==null?none("no task-tied outcomes yet"):h("span",{class:"confbar"},confBar(x.conf),h("span",{class:"mono",text:x.conf.toFixed(2)+"  ("+x.succ+" ok / "+x.failConf+" not, n="+x.nConf+")"}));
  wrap.append(h("section",{class:"sec"},h("h3",null,"Decision trace",h("small",{text:"recorded "+stamp(x.t)})),
    h("dl",{class:"trace"},
      h("dt",{text:"Decided"}),h("dd",{text:x.d}),
      h("dt",{text:"Rule applied"}),h("dd",null,x.rule||none("none recorded")),
      h("dt",{text:"Exception / override"}),h("dd",null,x.override||none("none recorded")),
      x.context?h("dt",{text:"Context"}):null, x.context?h("dd",{text:x.context}):null,
      x.prevOf?h("dt",{text:"Supersedes"}):null, x.prevOf?h("dd",null,plink(x.prevOf.id)):null,
      x.next?h("dt",{text:"Superseded by"}):null, x.next?h("dd",null,plink(x.next.id)):null,
      h("dt",{text:"Override by"}),h("dd",null,x.overrideBy||none("none")),
      h("dt",{text:"Approved by"}),h("dd",null,x.approver||none(one?"not recorded":"not approved · agent conclusion")),
      h("dt",{text:"Recorded by"}),h("dd",{text:x.by}),
      h("dt",{text:"Entity"}),h("dd",null,x.entities.length?h("span",{class:"mono",text:x.entities.join(", ")}):none("none")),
      x.crit?h("dt",{text:"Criticality"}):null, x.crit?h("dd",{class:"mono",text:x.crit+" of 5"}):null,
      h("dt",{text:"Trust tier"}),h("dd",null,h("span",{class:"badge "+TIER[x.tier].badge,text:TIER[x.tier].label}),"  ",h("span",{class:"none",text:TIER[x.tier].title})),
      h("dt",{text:"Confidence"}),h("dd",null,conf),
      h("dt",{text:"Helpfulness"}),h("dd",null,x.marked?h("span",{class:"mono",text:pct(x.helpful)+"  ("+x.mix.helped+" of "+x.marked+" marked helped)"}):none("no helpful marks yet")))));
  const gbox=h("div",{class:"gbox"});
  const plist=h("ul",{class:"plist"});
  x.prec.forEach(id=>plist.append(h("li",null,h("span",{class:"rel",text:"cites"}),h("span",null,plink(id)))));
  x.citedBy.forEach(id=>plist.append(h("li",null,h("span",{class:"rel",text:"cited by"}),h("span",null,plink(id)))));
  if(!plist.children.length) plist.append(h("li",null,h("span",{class:"rel",text:"—"}),h("span",{class:"none",text:"No precedents either way."})));
  wrap.append(h("section",{class:"sec"},h("h3",null,"Precedents",h("small",{text:x.prec.length+" cited · cited by "+x.citedBy.length})),gbox,h("div",{style:"height:12px"}),plist));
  const sec=h("section",{class:"sec",id:"sec-out"},h("h3",null,"Use and outcomes",h("small",{text:"all time · trend last 8 weeks"})),
    h("div",{class:"stats"},h("span",null,h("b",{text:x.lookups}),"lookups"),h("span",null,h("b",{text:x.applied}),"applied"),h("span",null,h("b",{text:x.nOut}),"outcomes"),
      h("span",{style:"display:inline-flex;gap:8px;align-items:center"},spark(x.trend,96,22),h("span",{class:"mono",style:"font-size:12px",text:"helped / week"}))),
    x.ev.length?h("ul",{class:"evlist"},x.ev.slice(0,10).map(evRow)):h("p",{class:"none",text:"No lookups, applications or outcomes yet."}));
  wrap.append(sec);
  box.replaceChildren(wrap);
  requestAnimationFrame(()=>renderGraph(x,gbox));
}
function confBar(v){ const w=120; return s("svg",{width:w,height:8,viewBox:`0 0 ${w} 8`,"aria-hidden":"true"},s("rect",{x:0,y:3,width:w,height:2,fill:"var(--rule)"}),s("rect",{x:0,y:1,width:Math.max(2,v*w),height:6,fill:"var(--accent)"})); }

/* ---------- activity ---------- */
function renderActivity(){
  const root=$("#activity"); root.replaceChildren(h("h2",{class:"vh",text:"Activity by agent"}));
  const days=28, t0=NOW-days*DAY;
  const agentSel=h("select",{id:"a-agent",onchange:e=>{st.actAgent=e.target.value;st.actLimit=40;renderActivity();}},h("option",{value:"",text:"All agents"}),AGENTS.map(a=>h("option",{value:a,text:a,selected:st.actAgent===a})));
  const kindSel=h("select",{id:"a-kind",onchange:e=>{st.actKind=e.target.value;st.actLimit=40;renderActivity();}},[["","Everything"],["lookup","Lookups"],["applied","Applied"],["outcome","Outcomes"],["failed","Misled / not helped"]].map(([v,t])=>h("option",{value:v,text:t,selected:st.actKind===v})));
  root.append(h("p",{class:"lede",text:"Which agent looked up or applied which decision, and what they reported back. Last 28 days, times in IST."}),
    h("div",{class:"bar"},h("div",{class:"field"},h("label",{for:"a-agent",text:"Agent"}),agentSel),h("div",{class:"field"},h("label",{for:"a-kind",text:"Event"}),kindSel)));
  const W=Math.max(320,root.clientWidth||900);
  const agents=(st.actAgent?[st.actAgent]:AGENTS).filter(a=>RAW.some(e=>e.agent===a&&e.t>=t0));
  if(W>=560&&agents.length){
    const lw=86, rw=W>=760?150:0, pw=W-lw-rw-8, lh=34, Hh=agents.length*lh+26;
    const xs=t=>lw+(t-t0)/(NOW-t0)*pw;
    const svg=s("svg",{class:"lanes",viewBox:`0 0 ${W} ${Hh}`,width:W,height:Hh,role:"img","aria-label":"Activity swimlanes per agent; the list below has the same events"});
    for(let d=0;d<=days;d+=7){ const x=xs(NOW-(days-d)*DAY); svg.append(s("line",{class:"rule",x1:x,x2:x,y1:0,y2:Hh-18}),s("text",{class:"tick",x:x,y:Hh-4,"text-anchor":d===days?"end":"middle"},d===days?"now":dayStr(NOW-(days-d)*DAY))); }
    agents.forEach((a,i)=>{
      const y=i*lh; const evs=RAW.filter(e=>e.agent===a&&e.t>=t0);
      svg.append(s("text",{x:0,y:y+lh/2+4},a),s("line",{class:"base",x1:lw,x2:lw+pw,y1:y+lh-6,y2:y+lh-6}));
      evs.forEach(e=>{ const x=xs(e.t); const d0=byId[e.ids[0]]; const title=s("title",null,`${stamp(e.t)} · ${e.agent} · ${e.k}${e.outcome?" "+OUT[e.outcome].label:""} · ${d0?d0.sid+" "+d0.d:e.q?"“"+e.q+"”, no hits":"no hits"}`); let m;
        if(e.k==="lookup") m=s("line",{class:"lk",x1:x,x2:x,y1:y+lh-10,y2:y+lh-6});
        else if(e.k==="applied") m=s("circle",{class:"ap",cx:x,cy:y+lh-15,r:2.2});
        else { const cy=y+8; m= e.outcome==="misled"?s("path",{class:"o-misled",d:`M${x} ${cy-4}L${x+4} ${cy}L${x} ${cy+4}L${x-4} ${cy}Z`}): e.outcome==="not_helped"?s("rect",{class:"o-not_helped",x:x-3,y:cy-3,width:6,height:6}): s("circle",{class:"o-"+e.outcome,cx:x,cy,r:3}); }
        m.append(title); if(d0){ m.style.cursor="pointer"; m.addEventListener("click",()=>go(d0.id)); } svg.append(m); });
      if(rw){ const lk=evs.filter(e=>e.k==="lookup").length, ap=evs.filter(e=>e.k==="applied").length, oc=evs.filter(e=>e.k==="outcome"), hp=oc.length?oc.filter(o=>o.outcome==="helped").length/oc.length:null;
        svg.append(s("text",{class:"num",x:W,y:y+lh/2+4,"text-anchor":"end"},`${lk} lk · ${ap} ap · ${pct(hp)}`)); }
    });
    root.append(svg);
    const L=(svgEl,t)=>h("span",null,svgEl,t);
    root.append(h("div",{class:"legend"},
      L(s("svg",{width:10,height:10},s("line",{x1:5,x2:5,y1:3,y2:9,stroke:"var(--ink-faint)"})),"looked up"),
      L(s("svg",{width:10,height:10},s("circle",{cx:5,cy:5,r:2.4,fill:"var(--accent)"})),"applied"),
      L(s("svg",{width:10,height:10},s("circle",{cx:5,cy:5,r:3.4,fill:"var(--pos)"})),"helped"),
      L(s("svg",{width:10,height:10},s("circle",{cx:5,cy:5,r:3.2,fill:"none",stroke:"var(--neutral-mark)","stroke-width":1.3})),"neutral"),
      L(s("svg",{width:10,height:10},s("rect",{x:2,y:2,width:6,height:6,fill:"var(--caution)"})),"not helped"),
      L(s("svg",{width:10,height:10},s("path",{d:"M5 1L9 5L5 9L1 5Z",fill:"var(--neg)"})),"misled"),
      rw?h("span",{class:"mono",text:"lk lookups · ap applied · % helped"}):null));
  }
  let evs=RAW.filter(e=>e.t>=t0&&(!st.actAgent||e.agent===st.actAgent));
  if(st.actKind==="failed") evs=evs.filter(e=>e.outcome==="misled"||e.outcome==="not_helped"); else if(st.actKind) evs=evs.filter(e=>e.k===st.actKind);
  const shown=evs.slice(0,st.actLimit); let lastDay="", ul=null;
  root.append(h("h3",{class:"h",text:"Event log"}),h("p",{class:"lede",style:"margin-bottom:0",text:evs.length+" events"+(st.actAgent?" by "+st.actAgent:"")+". Select one to open its decision."}));
  shown.forEach(e=>{ const dk=istKey(e.t); if(dk!==lastDay){ lastDay=dk; const g=h("div",{class:"daygroup"},h("h4",{text:fDayLong.format(e.t)})); ul=h("ul",{class:"alog"}); g.append(ul); root.append(g);}
    const x=byId[e.ids[0]]; const kind=e.k==="outcome"?h("span",{class:"kind "+OUT[e.outcome].cls,text:OUT[e.outcome].label}):h("span",{class:"kind",style:e.k==="applied"?"color:var(--accent-ink)":"color:var(--ink-muted)",text:e.k==="applied"?"applied":e.ids.length?"looked up":"no hits"});
    const what=e.k==="lookup"?[e.q?"“"+e.q+"”":(e.tool==="get"?"opened a trace":"lookup"),x?h("span",{style:"display:block"},h("b",{text:x.sid})," ",x.d,e.ids.length>1?h("span",{class:"none",text:"  +"+(e.ids.length-1)+" more"}):null):null]:[h("b",{text:x?x.sid:"–"})," ",x?x.d:"",e.reason?h("span",{style:"display:block",class:OUT[e.outcome].cls,text:e.reason}):null];
    ul.append(h("li",null,h("button",{type:"button",onclick:()=>{ if(x) go(x.id); }},h("time",{text:timeStr(e.t)}),h("span",{class:"who",text:e.agent}),h("span",{class:"what"},...what),kind)));
  });
  if(!shown.length) root.append(h("p",{class:"empty"},h("strong",{text:RAW.length?"No events for this filter.":"No ledger activity yet."}),RAW.length?"Pick another agent or event type.":"Lookups, applications and outcomes appear here as agents use the ledger."));
  if(evs.length>shown.length) root.append(h("button",{class:"btn more",type:"button",onclick:()=>{st.actLimit+=40;renderActivity();},text:`Show ${Math.min(40,evs.length-shown.length)} more`}));
  root.append(h("footer",{class:"fp",text:"Live ledger data, read-only. Lookups come from access_log; applied and outcome events from the trace. Viewer reads are never logged."}));
}

/* ---------- outcomes + A/B ---------- */
function renderAB(root){
  const S=STATS; root.append(h("h3",{class:"h",text:"A/B trial: find on odd IST days, off on even days"}));
  if(!S){ root.append(h("p",{class:"empty"},h("strong",{text:"A/B numbers unavailable."}),"The stats reader did not answer.")); return; }
  const armTxt=a=>a==="on"?"lookups on":a==="off"?"lookups off":"outside the trial";
  root.append(h("p",{class:"lede",text:`${S.window[0]} to ${S.window[1]} IST. Today (${S.today}) is ${armTxt(S.today_arm)}. Numbers come from the per-task log that ledger_loop.py close writes, read by ledger_stats.py; tokens are estimates (chars ÷ 4).`}));
  if(!S.ab){ root.append(h("p",{class:"empty"},h("strong",{text:"No A/B report on this machine."}),S.ab_note||"")); return; }
  const on=S.ab.on, off=S.ab.off, kpi=(v,sm,l,p)=>h("div",{class:"kpi"+(p?" primary":"")},h("div",{class:"v"},v,sm?h("small",{text:sm}):null),h("div",{class:"l",text:l}));
  const f1=x=>x.toFixed(1), na=(n,v)=>n?v:"–";
  root.append(h("div",{class:"kpis"},
    kpi(na(on.gated,on.helpful_per15.toFixed(2)),"per 15 · n="+on.gated,"Helpful-hit rate, gated tasks (on)",true),
    kpi(na(on.gated,String(on.tok_median)),"p90 "+na(on.gated,String(on.tok_p90)),"Overhead tokens, median (on, est)"),
    kpi(na(on.gated,f1(on.fail_open_pct)+"%"),on.gated+" gated","Fail-open (on)"),
    kpi(on.trusted_precision==null?"–":Math.round(on.trusted_precision)+"%",on.trusted_applied+" applied","Trusted-tier precision (on)"),
    kpi(na(off.gated,off.helpful_per15.toFixed(2)),"per 15 · n="+off.gated,"Helpful-hit rate (off)")));
  const tb=h("tbody"); S.days.forEach(d=>{ const today=d.date===S.today; tb.append(h("tr",null,
    h("td",{class:"dec"},h("span",{class:"mono",text:d.date+(today?" · today":"")})),
    h("td",null,h("span",{class:"badge "+(d.arm==="on"?"acc":""),text:d.arm==="on"?"on · lookups":"off · no lookups"})),
    h("td",{class:"num",text:d.tasks}),h("td",{class:"num",text:d.gated}),h("td",{class:"num",text:d.hit_tasks}),h("td",{class:"num hide-n",text:d.fail_open}))); });
  root.append(h("table",{class:"otab"},h("caption",{class:"vh",text:"A/B arm and task counts per IST day"}),
    h("thead",null,h("tr",null,["Day (IST)","Arm","Tasks","Gated","Helpful hits","Fail-open"].map((l,i)=>h("th",{scope:"col",class:i>=2?"num"+(i===5?" hide-n":""):"",text:l})))),tb));
  const tiers=S.tiers||{}; root.append(h("p",{class:"fineprint",text:`Trust tiers now: ${tiers.trusted||0} trusted, ${tiers.user_decision||0} user decision, ${tiers.hint||0} hint. Trusted = 3 or more helped outcomes and a failure rate of 10% or less; find may still demote one in a context where a fix supersedes it.`}));
}
function renderOutcomes(){
  const root=$("#outcomes"); root.replaceChildren(h("h2",{class:"vh",text:"Did it help: outcomes"}));
  renderAB(root);
  const outs=EVENTS.filter(e=>e.k==="outcome"); const n=outs.length; const c=k=>outs.filter(o=>o.outcome===k).length;
  const marked=c("helped")+c("not_helped")+c("misled");
  const lk=EVENTS.filter(e=>e.k==="lookup"&&e.id); const appliedTasks=new Set(EVENTS.filter(e=>e.k==="applied").map(e=>e.task+e.id));
  const notApplied=lk.length?lk.filter(e=>!appliedTasks.has(e.task+e.id)).length/lk.length:null;
  root.append(h("h3",{class:"h",text:"Outcomes, all time"}));
  root.append(h("p",{class:"lede",text:"What agents reported after applying a decision. Helpfulness = helped ÷ outcomes with a helpful mark. Confidence = task successes ÷ task-tied outcomes. They are different measures; unmarked outcomes predate the helpful mark."}));
  const kpi=(v,sm,l,p)=>h("div",{class:"kpi"+(p?" primary":"")},h("div",{class:"v"},v,sm?h("small",{text:sm}):null),h("div",{class:"l",text:l}));
  root.append(h("div",{class:"kpis"},kpi(marked?pct(c("helped")/marked):"–","n="+marked,"Helpful rate, marked outcomes",true),kpi(String(c("misled")),marked?pct(c("misled")/marked):null,"Misled"),kpi(String(c("not_helped")),marked?pct(c("not_helped")/marked):null,"Not helped"),kpi(pct(notApplied),lk.length+" lookups","Looked up, never applied"),kpi(String(DECISIONS.filter(x=>x.prevOf).length),null,"Fixed by a new decision")));
  if(!n){ root.append(h("p",{class:"empty"},h("strong",{text:"No outcomes reported yet."}),"They appear when an agent closes a task that applied a decision.")); root.append(h("footer",{class:"fp",text:"Live ledger data, read-only."})); return; }
  const agentsOut=AGENTS.filter(a=>outs.some(o=>o.agent===a));
  root.append(h("h3",{class:"h",text:"Helpful rate by agent, weekly"}),h("p",{class:"lede",style:"margin:0",text:"Same 0–100% scale in every panel, marked outcomes only. Grey bars underneath: outcomes reported that week (count)."}));
  const mult=h("div",{class:"multiples"}); const maxN=Math.max(1,...["All",...agentsOut].flatMap(a=>Array.from({length:WEEKS},(_,i)=>outs.filter(o=>(a==="All"||o.agent===a)&&o.t>=wkStart(i)&&o.t<wkStart(i+1)).length)));
  ["All",...agentsOut].forEach(a=>{
    const o=outs.filter(e=>a==="All"||e.agent===a); const wk=Array.from({length:WEEKS},(_,i)=>o.filter(e=>e.t>=wkStart(i)&&e.t<wkStart(i+1)));
    const rates=wk.map(w=>{const m=w.filter(e=>e.outcome!=="neutral"); return m.length?m.filter(e=>e.outcome==="helped").length/m.length:null;});
    const Wd=180,Hd=70,ch=46; const svg=s("svg",{viewBox:`0 0 ${Wd} ${Hd}`,role:"img","aria-label":`${a}: weekly helpful rate ${rates.map(pct).join(", ")}`});
    svg.append(s("line",{class:"ref",x1:0,x2:Wd,y1:2,y2:2}),s("line",{class:"ref",x1:0,x2:Wd,y1:2+ch/2,y2:2+ch/2}),s("line",{class:"ref",x1:0,x2:Wd,y1:2+ch,y2:2+ch}));
    const xx=i=>8+i*(Wd-16)/(WEEKS-1);
    wk.forEach((w,i)=>{ const bh=w.length/maxN*14; svg.append(s("rect",{class:"nb",x:xx(i)-4,y:Hd-bh-2,width:8,height:bh})); });
    let d="",pen=false; rates.forEach((r,i)=>{ if(r==null){pen=false;return;} d+=(pen?"L":"M")+xx(i).toFixed(1)+" "+(2+ch-r*ch).toFixed(1); pen=true; });
    if(d) svg.append(s("path",{class:"ln",d}));
    const li=rates.map((r,i)=>r==null?-1:i).filter(i=>i>=0).pop(); if(li!=null&&li>=0) svg.append(s("circle",{class:"end",cx:xx(li),cy:2+ch-rates[li]*ch,r:2.5}));
    const m=o.filter(e=>e.outcome!=="neutral"), hp=m.length?m.filter(e=>e.outcome==="helped").length/m.length:null;
    mult.append(h("div",{class:"sm"},h("h5",null,a==="All"?"All agents":a,h("span",{text:pct(hp)+" · n"+o.length})),svg));
  });
  root.append(mult, h("div",{class:"legend",style:"margin-top:6px"},h("span",{class:"mono",text:"x: 8 weeks to "+dayStr(NOW)+" · y: 0–100% helped"})));
  root.append(h("h3",{class:"h",text:"Per decision"}));
  const cols=[["d","Decision",""],["nOut","n","num hide-n"],["mix","Mix",""],["helpful","Helpful","num"],["conf","Conf.","num hide-n"],["trend","8 wk","trend"]];
  const rows=DECISIONS.filter(x=>x.nOut).slice().sort((a,b)=>{ const k=st.oSort; const va=k==="d"?a.id:a[k]??-1, vb=k==="d"?b.id:b[k]??-1; return (va>vb?1:va<vb?-1:0)*st.oDir; });
  const thead=h("tr",null,cols.map(([k,l,c])=>{ const sortable=!["mix","trend"].includes(k); const th=h("th",{class:c,scope:"col","aria-sort":st.oSort===k?(st.oDir>0?"ascending":"descending"):null});
    th.append(sortable?h("button",{type:"button",onclick:()=>{ if(st.oSort===k) st.oDir*=-1; else {st.oSort=k; st.oDir=k==="d"?1:-1;} renderOutcomes(); }},l,st.oSort===k?(st.oDir>0?" ↑":" ↓"):""):l); return th; }));
  const tb=h("tbody"); rows.forEach(x=>tb.append(h("tr",null,
    h("td",{class:"dec"},h("button",{type:"button",onclick:()=>go(x.id)},h("span",{class:"mono",text:x.sid+(x.status==="superseded"?" · superseded":"")+(x.tier==="trusted"?" · trusted":"")}),h("span",{class:"tt",text:x.d}))),
    h("td",{class:"num hide-n",text:x.nOut}),h("td",{class:"mix"},mixbar(x,W_mix())),h("td",{class:"num",text:pct(x.helpful)}),h("td",{class:"num hide-n",text:x.conf==null?"–":x.conf.toFixed(2)}),h("td",{class:"trend"},spark(x.trend,72,20)))));
  root.append(h("table",{class:"otab"},h("caption",{class:"vh",text:"Outcomes per decision"}),h("thead",null,thead),tb));
  root.append(h("div",{class:"legend"},
    ...OUTKEYS.map(k=>h("span",null,s("svg",{width:10,height:10},s("rect",{x:0,y:1,width:10,height:8,fill:{helped:"var(--pos)",neutral:"var(--neutral-mark)",not_helped:"var(--caution)",misled:"var(--neg)"}[k]})),OUT[k].label))));
  root.append(h("p",{class:"fineprint",text:"Mix shows each decision's outcomes as shares of 100%, in a fixed order. Decisions with no reported outcomes are left out of the table."}),h("footer",{class:"fp",text:"Live ledger data, read-only."}));
}
const W_mix=()=>window.innerWidth<480?64:100;

/* ---------- tabs, theme, keyboard ---------- */
function setTab(t){
  st.tab=t; ["decisions","activity","outcomes"].forEach(k=>{ const on=k===t; const tb=$("#t-"+k); tb.setAttribute("aria-selected",on); tb.tabIndex=on?0:-1; $("#p-"+k).hidden=!on; });
  if(t==="activity") renderActivity(); if(t==="outcomes") renderOutcomes();
  if(typeof resize==="function") resize();
}
const themes=["auto","light","dark"];
function setTheme(t){ const r=document.documentElement; if(t==="auto") r.removeAttribute("data-theme"); else r.setAttribute("data-theme",t);
  $("#themeLabel").textContent=t[0].toUpperCase()+t.slice(1); $("#themeBtn").setAttribute("aria-label",`Theme: ${t==="auto"?"follows system":t}. Switch theme`); st.theme=t; }
function isTyping(e){ const t=e.target; return t&&(t.tagName==="INPUT"||t.tagName==="SELECT"||t.tagName==="TEXTAREA"); }
function moveSel(d){ if(!filtered.length) return; let i=filtered.findIndex(x=>x.id===st.sel); i=i<0?0:Math.min(filtered.length-1,Math.max(0,i+d)); select(filtered[i].id,{reset:true}); }
document.addEventListener("keydown",e=>{
  if((e.key==="k"||e.key==="K")&&(e.metaKey||e.ctrlKey)){ e.preventDefault(); focusSearch(); return; }
  if(e.key==="/"&&!isTyping(e)){ e.preventDefault(); focusSearch(); return; }
  if(e.key==="Escape"){ if(st.open){ e.preventDefault(); closeDetail(); return; } if(e.target.id==="q"&&st.q){ e.target.value=""; st.q=""; runSearch(); return; } if(isTyping(e)) e.target.blur(); return; }
  if(st.tab!=="decisions") return;
  const inList=e.target.closest&&e.target.closest("#list"), inSearch=e.target.id==="q";
  if((inList||inSearch)&&(e.key==="ArrowDown"||e.key==="ArrowUp")){ e.preventDefault(); moveSel(e.key==="ArrowDown"?1:-1); if(inList){const r=document.querySelector(`.row[data-id="${st.sel}"]`); r&&r.focus();} return; }
  if(inList&&(e.key==="Home"||e.key==="End")){ e.preventDefault(); select((e.key==="Home"?filtered[0]:filtered[filtered.length-1]).id,{reset:true}); document.querySelector(`.row[data-id="${st.sel}"]`).focus(); return; }
  if(inSearch&&e.key==="Enter"){ e.preventDefault(); if(!st.sel&&filtered[0]) select(filtered[0].id,{reset:true}); openDetail(true); return; }
  if(e.altKey&&e.key==="ArrowLeft"){ e.preventDefault(); back(); }
});
function focusSearch(){ if(st.tab!=="decisions") setTab("decisions"); if(st.open) closeDetail(); const q=$("#q"); q.focus(); q.select(); }
$("#t-decisions").parentElement.addEventListener("keydown",e=>{ if(e.key!=="ArrowRight"&&e.key!=="ArrowLeft") return; const order=["decisions","activity","outcomes"]; let i=order.indexOf(st.tab)+(e.key==="ArrowRight"?1:-1); i=(i+3)%3; setTab(order[i]); $("#t-"+order[i]).focus(); });
["decisions","activity","outcomes"].forEach(k=>$("#t-"+k).addEventListener("click",()=>setTab(k)));
$("#themeBtn").addEventListener("click",()=>setTheme(themes[(themes.indexOf(st.theme)+1)%3]));
$("#backBtn").addEventListener("click",back);
$("#filterToggle").addEventListener("click",()=>{ const f=$("#filters"); const on=!f.classList.contains("show"); f.classList.toggle("show",on); $("#filterToggle").setAttribute("aria-expanded",on); });
let qTimer=0, qGen=0;
async function runSearch(){ const q=st.q, g=++qGen; if(!q){ st.qIds=null; st.qRank=null; applyFilters(); return; }
  try{ const r=await pages("query",{q},6); if(g!==qGen) return; st.qIds=new Set(r.items.map(c=>c.id)); st.qRank=new Map(r.items.map((c,i)=>[c.id,i])); }
  catch(err){ if(g!==qGen) return; st.qIds=new Set(); st.qRank=new Map(); $("#resultCount").textContent="Search failed: "+err.message; return; }
  applyFilters(); }
$("#q").addEventListener("input",e=>{ st.q=e.target.value.trim(); clearTimeout(qTimer); qTimer=setTimeout(runSearch,180); });
[["f-type","type"],["f-entity","entity"],["f-bot","bot"],["f-outcome","outcome"],["f-status","status"],["f-sort","sort"],["f-from","from"],["f-to","to"]].forEach(([id,k])=>$("#"+id).addEventListener("change",e=>{st[k]=e.target.value;applyFilters();}));
$("#f-date").addEventListener("change",e=>{ st.date=e.target.value; $("#customRange").hidden=st.date!=="custom"; applyFilters(); });
$("#clearBtn").addEventListener("click",()=>{ Object.assign(st,{q:"",qIds:null,qRank:null,type:"",entity:"",bot:"",date:"",from:"",to:"",outcome:"",status:""}); ["q","f-type","f-entity","f-bot","f-date","f-outcome","f-status","f-from","f-to"].forEach(id=>$("#"+id).value=""); $("#customRange").hidden=true; applyFilters(); $("#q").focus(); });
let rz; window.addEventListener("resize",()=>{ clearTimeout(rz); rz=setTimeout(()=>{ if(!narrow()&&st.open){ $("#detail").classList.remove("open"); st.open=false; $(".listcol").inert=false; } if(st.tab==="activity") renderActivity(); else if(st.tab==="outcomes") renderOutcomes(); else if(st.sel){ const g=$(".gbox"); g&&renderGraph(byId[st.sel],g);} },120); });

/* ---------- boot ---------- */
setTheme("auto");
const hp=new URLSearchParams(location.hash.slice(1));
if(hp.get("theme")) setTheme(hp.get("theme"));
function resize(){ if(EMBED) notify("ui/notifications/size-changed",{width:document.documentElement.scrollWidth,height:Math.max(640,Math.min(900,window.innerHeight||640))}); }
function failBoot(err){
  $("#resultCount").textContent="";
  const ul=$("#list"); ul.replaceChildren(h("li",{class:"empty"},h("strong",{text:"Couldn't read the ledger."}),"Error "+(err&&err.message||"UNAVAILABLE")+". ",h("button",{class:"linkbtn",type:"button",onclick:load,text:"Retry"})));
}
let loadGen=0, PARTIAL="";
async function load(){
  const g=++loadGen;
  $("#resultCount").textContent="Loading the ledger…";
  try{
    const [q,a,stats]=await Promise.all([pages("query",{},200),pages("activity",{},200),api("stats",{}).catch(()=>null)]);
    if(g!==loadGen) return; /* a newer load (Retry) owns the view */
    PARTIAL=q.partial||a.partial?` (first ${q.items.length} of ${q.total} decisions, ${a.items.length} of ${a.total} events)`:"";
    ingest(q.items,a.items,stats);
  }catch(err){ if(g===loadGen) failBoot(err); return; }
  fillSelects();
  $("#ledgerCount").textContent=`${DECISIONS.length} decisions · ${RAW.length} events · as of ${stamp(NOW)}${PARTIAL}`;
  $("#n-dec").textContent=DECISIONS.length; $("#n-act").textContent=RAW.filter(e=>e.t>=NOW-28*DAY).length; $("#n-out").textContent=EVENTS.filter(e=>e.k==="outcome").length;
  if(hp.get("status")) { st.status=hp.get("status"); $("#f-status").value=st.status; }
  applyFilters();
  if(hp.get("q")) { st.q=hp.get("q"); $("#q").value=st.q; await runSearch(); }
  const want=hp.get("d")&&DECISIONS.find(x=>x.id.startsWith(hp.get("d")));
  if(want){ select(want.id,{reset:true}); if(hp.get("open")) openDetail(false); }
  if(hp.get("tab")) setTab(hp.get("tab")); else if(st.tab!=="decisions") setTab(st.tab);
  resize();
}
if(EMBED){
  rpc("ui/initialize",{protocolVersion:"2026-01-26",appInfo:{name:"context-ledger",version:"0.1.28"},appCapabilities:{availableDisplayModes:["inline","fullscreen"]}})
    .then(()=>{ notify("ui/notifications/initialized"); resize(); load(); })
    .catch(()=>{ $("#resultCount").textContent="This view opens in a host that supports MCP Apps."; });
} else load();
window.__ledger={st,go,setTab,setTheme,load};
})();
