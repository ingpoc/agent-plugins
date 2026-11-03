import {AppBridge,PostMessageTransport} from '@modelcontextprotocol/ext-apps/app-bridge';
const frame=document.getElementById('app');
const bridge=new AppBridge(null,{name:'Observability acceptance host',version:'0.1.1'},{serverTools:{}});
const call=async params=>{const response=await fetch('/tool',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)});if(!response.ok)throw new Error('Host tool request failed');return response.json();};
bridge.oncalltool=params=>call(params);
bridge.onsizechange=({height})=>{frame.style.height=`${Math.max(700,height)}px`;};
bridge.oninitialized=async()=>{await bridge.sendToolInput({arguments:{}});await bridge.sendToolResult(await call({name:'observability_dashboard',arguments:{}}));document.getElementById('status').textContent='MCP Apps initialized · local acceptance host';};
await bridge.connect(new PostMessageTransport(frame.contentWindow,frame.contentWindow));
frame.src='/dashboard';
