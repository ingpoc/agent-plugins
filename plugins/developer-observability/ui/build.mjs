import {build} from 'esbuild';
import {readFile, writeFile} from 'node:fs/promises';
const result = await build({entryPoints:['ui/app.js'],bundle:true,write:false,format:'esm',minify:true,target:'es2022',legalComments:'inline'});
const template = await readFile('ui/template.html','utf8');
await writeFile('ui/dashboard.html',template.replace('<!--APP-->',()=>`<script type="module">${result.outputFiles[0].text.replaceAll('</script','<\\/script')}</script>`));
