import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const script=await readFile(new URL('../ui/app.js',import.meta.url),'utf8');
assert.match(script,/const request=\+\+generation/);
assert.match(script,/if\(request!==generation\)return/);
assert.match(script,/dataset\.open/);
assert.match(script,/dataset\.preset/);
assert.match(script,/dataset\.snapFrom/);
console.log('PASS stale issue responses are discarded and scope controls remain wired');
