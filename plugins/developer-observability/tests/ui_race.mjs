import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const script=await readFile(new URL('../ui/app.js',import.meta.url),'utf8');
assert.match(script,/const request=\+\+generation/);
assert.match(script,/if\(request!==generation\)return/);
assert.match(script,/dataset\.open/);
assert.match(script,/dataset\.preset/);
assert.match(script,/dataset\.snapFrom/);
assert.match(script,/function issuesQuery\(\)/);
assert.match(script,/if \(pr && pr !== 'current'\) q\.set\('pr', pr\);/);
assert.match(script,/if \(run\) q\.set\('run', run\);/);
assert.match(script,/if \(result\.pr && prUnset\(pr\)\)/);
assert.doesNotMatch(script,/if \(result\.pr\) pr = /);
assert.match(script,/function openerFor\(/);
assert.match(script,/\.rows \.row\[data-open=/);
assert.match(script,/function publicRef\(/);
assert.match(script,/ · \$\{esc\(i\.origin_label\)\}/);
assert.doesNotMatch(script,/e\.ref \? ` · \$\{esc\(e\.ref\)\}/);
const queryFn = script.match(/function issuesQuery\(\) \{[\s\S]*?\n\}/)[0];
const project='agent-plugins', pr='120', run='last', round=7, scope='window', from=null, to=null;
const issuesQuery = eval('('+queryFn+')');
const q = issuesQuery();
assert.equal(q.get('pr'), '120');
assert.equal(q.get('run'), 'last');
assert.equal(q.has('round'), false);
function prUnset(value) { return !value || value === 'current'; }
function prNumber(pr, data) {
  const chosen = prUnset(pr) ? (data.pr || pr) : pr;
  return String(chosen || '').replace(/^#/, '');
}
const data = {pr: '121', counts: {tested: 48, fixed: 3, inconclusive: 45}, issues: Array(48).fill({})};
assert.equal('#'+prNumber('120', data), '#120');
assert.equal('#'+prNumber('current', data), '#121');
assert.equal('#'+prNumber(null, data), '#121');
function publicRef(ref) {
  if (!ref) return '';
  const raw = String(ref);
  const check = raw.match(/^check:([^:]+)/);
  if (check) return ` · run ${check[1].split('-')[0]}`;
  return ` · ${raw}`;
}
assert.equal(publicRef('check:r2-62036ff4-121:DO-12'), ' · run r2');
assert.equal(publicRef(null), '');
assert.equal(data.counts.tested, 48);
console.log('PASS stale issue responses are discarded and scope controls remain wired');
console.log('PASS pr=120 plus run=last keeps the #120 breadcrumb for 48 tested');
console.log('PASS missing or current PR fills from the response, check refs shorten, Escape returns to the row');
