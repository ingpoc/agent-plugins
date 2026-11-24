import {App} from '@modelcontextprotocol/ext-apps';

const $ = id => document.getElementById(id);
const http = Boolean(globalThis.DEVOBS_VIEWER);
const app = http ? null : new App({name: 'Developer observability', version: '0.1.2'});
const colors = {
  regressed: '#b3261e', blocked: '#d4583f', in_progress: '#2457a6', untested: '#cfd2ce',
  parked: '#d9a441', resolved: '#25684c', new: '#2457a6', reoccurred: '#9a5b00',
  fixed: '#25684c', worked: '#cfd2ce', inconclusive: '#8d9397'
};
const labels = {
  regressed: 'Regressed', blocked: 'Blocked', in_progress: 'In progress', untested: 'Untested',
  parked: 'Parked', resolved: 'Resolved', new: 'New', reoccurred: 'Failed again',
  fixed: 'Fixed', worked: 'Worked on', inconclusive: 'Inconclusive'
};
const PRESETS = ['Now', 'Last test run', '1 h', '6 h', '24 h', '7 d', 'This PR'];
const PRESET_FROM = {'1 h': 'now-1h', '6 h': 'now-6h', '24 h': 'now-24h', '7 d': 'now-7d'};

let data = {issues: [], rounds: [], runs: [], lists: {projects: [], prs: []}};
let project = 'agent-plugins', pr = 'current', round = null, scope = 'now';
let from = null, to = null, run = null, openGroup = null, showAll = false, drawer = null;
let timeLabel = 'Now', generation = 0, lastFocus = null, openerSelector = null, dragging = null;

function prUnset(value) {
  return !value || value === 'current';
}

function prNumber() {
  const chosen = prUnset(pr) ? (data.pr || pr) : pr;
  return String(chosen || '').replace(/^#/, '');
}

function publicRef(ref) {
  if (!ref) return '';
  const raw = String(ref);
  const check = raw.match(/^check:([^:]+)/);
  if (check) return ` · run ${esc(check[1].split('-')[0])}`;
  return ` · ${esc(raw)}`;
}

function openerFor(id, node) {
  const value = CSS.escape(String(id));
  if (node && node.classList && node.classList.contains('act') && node.dataset.open === String(id)) {
    return `.need .act[data-open="${value}"]`;
  }
  return `.rows .row[data-open="${value}"]`;
}

const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const tool = async (name, args) => {
  const r = await app.callServerTool({name, arguments: args});
  const d = r.structuredContent ?? JSON.parse(r.content?.find(x => x.type === 'text')?.text ?? '{}');
  if (r.isError || d.error) throw Error(d.error);
  return d;
};

function pad(n) { return String(n).padStart(2, '0'); }

function istParts(value) {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(+d)) return null;
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  }).formatToParts(d).map(x => [x.type, x.value]));
  return parts;
}

function formatTime(value, withDate = false) {
  const p = istParts(value);
  if (!p) return '';
  const clock = `${p.hour}:${p.minute}`;
  if (!withDate && isToday(value)) return clock;
  return `${Number(p.day)} ${p.month} ${clock}`;
}

function isToday(value) {
  return new Date(value).toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'})
    === new Date().toLocaleDateString('en-CA', {timeZone: 'Asia/Kolkata'});
}

function textInput(value) {
  const p = istParts(value);
  if (!p) return '';
  const month = String(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].indexOf(p.month) + 1).padStart(2, '0');
  return `${p.year}-${month}-${pad(p.day)} ${p.hour}:${p.minute}`;
}

function parseIstInput(value) {
  const text = String(value || '').trim();
  if (!text) return null;
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!match) return null;
  return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:00+05:30`;
}

function relativeName(fromValue, toValue) {
  if (fromValue === 'now-1h' && toValue === 'now') return '1 h';
  if (fromValue === 'now-6h' && toValue === 'now') return '6 h';
  if (fromValue === 'now-24h' && toValue === 'now') return '24 h';
  if (fromValue === 'now-7d' && toValue === 'now') return '7 d';
  return null;
}

function windowRangeLabel(fromValue, toValue) {
  const named = relativeName(fromValue, toValue);
  if (named) return named;
  if (fromValue && toValue) {
    const start = formatTime(fromValue, !isToday(fromValue));
    const end = toValue === 'now' ? 'now' : formatTime(toValue, !isToday(toValue));
    if (start && end) return `${start}–${end}`;
  }
  return null;
}

function currentPrs() {
  const rows = data.lists?.prs || [];
  const forProject = rows.filter(row => !row.project || row.project === project);
  return forProject.length ? forProject : rows;
}

function currentPrRow() {
  const wanted = String(pr || '').replace(/^#/, '');
  return currentPrs().find(row => String(row.pr) === wanted) || currentPrs()[0];
}

function latestPr() {
  return currentPrs()[0] || null;
}

function applyCascade(change) {
  const lists = data.lists || {projects: [], prs: []};
  if ('project' in change) {
    project = change.project;
    const prs = (lists.prs || []).filter(row => row.project === project);
    const chosen = prs[0] || (lists.prs || [])[0];
    pr = String(chosen?.pr || 'current');
    round = chosen?.latest_round ?? null;
    return;
  }
  if ('pr' in change) {
    pr = String(change.pr).replace(/^#/, '');
    const match = (lists.prs || []).find(row => String(row.pr) === pr);
    if (match?.project) project = match.project;
    round = match?.latest_round ?? null;
    return;
  }
  if ('round' in change) {
    round = change.round === 'current' ? (currentPrRow()?.latest_round ?? null) : Number(change.round);
  }
}

function urlState() {
  const q = new URL(location.href).searchParams;
  if (q.has('project')) project = q.get('project');
  if (q.has('pr')) pr = q.get('pr').replace(/^#/, '');
  if (q.has('round')) {
    round = q.get('round') === 'current' ? 'current' : Number(q.get('round'));
    scope = `round:${round}`;
  }
  if (q.has('run')) {
    run = q.get('run');
    scope = 'window';
    timeLabel = run === 'last' ? 'Last test run' : 'Test run';
  } else if (q.has('from') && q.has('to')) {
    from = q.get('from');
    to = q.get('to');
    scope = 'window';
    timeLabel = relativeName(from, to) || (from === 'pr' ? 'This PR' : 'Custom');
  } else if (q.has('scope')) {
    scope = q.get('scope');
  }
  if (q.has('group')) openGroup = q.get('group');
  if (q.has('drawer')) drawer = q.get('drawer');
}

function updateUrl() {
  const u = new URL(location.href);
  for (const key of ['project', 'pr', 'round', 'scope', 'from', 'to', 'run', 'group', 'drawer']) {
    u.searchParams.delete(key);
  }
  if (project) u.searchParams.set('project', project);
  if (pr) u.searchParams.set('pr', pr);
  if (run) {
    u.searchParams.set('run', run);
  } else if (from && to) {
    u.searchParams.set('from', from);
    u.searchParams.set('to', to);
  } else if (round != null && !Number.isNaN(Number(round))) {
    u.searchParams.set('round', String(round));
  }
  if (openGroup) u.searchParams.set('group', openGroup);
  if (drawer) u.searchParams.set('drawer', drawer);
  history.replaceState(null, '', u);
}

function issuesQuery() {
  const q = new URLSearchParams({project});
  if (pr && pr !== 'current') q.set('pr', pr);
  if (run) q.set('run', run);
  else {
    if (round != null && !Number.isNaN(Number(round))) q.set('round', String(round));
    else q.set('scope', scope);
    if (from && to) {
      q.set('from', from);
      q.set('to', to);
    }
  }
  return q;
}

async function load() {
  const request=++generation;
  try {
    $('error').hidden = true;
    const q = issuesQuery();
    const result = http
      ? await (await fetch(`/api/issues?${q}`)).json()
      : await tool('observability_issues', {
          project_id: project, pr, scope, from_time: from, to_time: to,
          round: round != null ? String(round) : undefined,
          run: run || undefined
        });
    if(request!==generation)return;
    if (result.error) throw Error(result.error);
    data = result;
    if (result.pr && prUnset(pr)) pr = String(result.pr).replace(/^#/, '');
    if (result.round != null && run == null && !(from && to)) round = result.round;
    if (result.run_note && !result.run) timeLabel = result.run_note;
    else if (from && to && !run) timeLabel = windowRangeLabel(from, to) || timeLabel;
    else if (result.run && result.run.recorded) timeLabel = runLabel(result.run);
    render();
  } catch (e) {
    if(request!==generation)return;
    $('error').hidden = false;
    $('error').textContent = `Unable to load issues · ${e.message}`;
  }
}

function runLabel(row, fallback = 'run time not recorded') {
  if (!row) return fallback;
  const start = row.started_at || row.start || row.installed_at;
  const end = row.ended_at || row.end;
  const prLabel = row.pr ? `#${String(row.pr).replace(/^#/, '')}` : (pr ? `#${pr}` : '');
  const roundLabel = row.round != null ? ` R${row.round}` : '';
  if (!start || !end) return `${prLabel}${roundLabel} run · ${fallback}`.trim();
  return `${prLabel}${roundLabel} run · ${formatTime(start)}–${formatTime(end)}`.replace(/^\s+/, '');
}

function lastRecordedRun() {
  const wanted = String(pr || data.pr || '').replace(/^#/, '');
  const runs = [...(data.runs || [])].filter(row => {
    const recorded = (row.started_at || row.installed_at) && (row.ended_at || row.end);
    if (!recorded) return false;
    if (!wanted || wanted === 'current') return true;
    return String(row.pr || '').replace(/^#/, '') === wanted;
  });
  if (!runs.length) return null;
  return runs.sort((a, b) => {
    const ta = new Date(a.started_at || a.installed_at).getTime();
    const tb = new Date(b.started_at || b.installed_at).getTime();
    return ta - tb;
  }).at(-1);
}

function setPreset(name) {
  closePops();
  run = null;
  from = null;
  to = null;
  timeLabel = name;
  if (name === 'Now') {
    scope = round != null ? `round:${round}` : 'now';
    updateUrl();
    load();
    return;
  }
  if (name === 'Last test run') {
    const last = lastRecordedRun();
    run = 'last';
    scope = 'window';
    from = null;
    to = null;
    if (!last || !((last.started_at || last.installed_at) && (last.ended_at || last.end))) {
      const prLabel = `#${prNumber()}`;
      timeLabel = `no ${prLabel} run yet`;
      $('time-label').textContent = timeLabel;
      updateUrl();
      load();
      return;
    }
    timeLabel = runLabel(last);
    updateUrl();
    load();
    return;
  }
  scope = 'window';
  if (name === 'This PR') {
    const opened = data.pull_request?.opened_at;
    if (!opened) {
      timeLabel = 'PR opened time not recorded';
      $('time-label').textContent = timeLabel;
      updateUrl();
      render();
      return;
    }
    from = opened;
    to = 'now';
  } else if (PRESET_FROM[name]) {
    from = PRESET_FROM[name];
    to = 'now';
  }
  updateUrl();
  load();
}

function setWindow() {
  from = parseIstInput($('from').value);
  to = parseIstInput($('to').value);
  if (!from || !to) return;
  run = null;
  scope = 'window';
  timeLabel = windowRangeLabel(from, to) || [textInput(from), textInput(to)].filter(Boolean).join('–') || 'Custom';
  updateUrl();
  load();
}

function setRun(id) {
  const row = (data.runs || []).find(item => String(item.id || item.run_id) === String(id));
  closePops();
  if (!row || !((row.started_at || row.installed_at) && (row.ended_at || row.end))) {
    timeLabel = 'run time not recorded';
    $('time-label').textContent = timeLabel;
    run = null;
    from = null;
    to = null;
    updateUrl();
    render();
    return;
  }
  run = String(id);
  from = null;
  to = null;
  scope = 'window';
  timeLabel = runLabel(row);
  updateUrl();
  load();
}

function closePops() {
  for (const id of ['project-pop', 'pr-pop', 'round-pop', 'time-pop']) {
    const node = $(id);
    if (node) node.hidden = true;
    const button = $(id.replace('-pop', ''));
    if (button) button.setAttribute('aria-expanded', 'false');
  }
}

function toggle(id) {
  const pop = $(id);
  const wasHidden = pop.hidden;
  closePops();
  pop.hidden = !wasHidden ? true : false;
  const button = $(id.replace('-pop', ''));
  if (button) button.setAttribute('aria-expanded', String(!pop.hidden));
}

function breadcrumbRound(selectedRound, selectedRun, runRecord, windowView) {
  // A selected run takes its own round; other window views show a dash.
  if (selectedRun) {
    const value = runRecord && runRecord.round != null ? Number(runRecord.round) : null;
    return value != null && !Number.isNaN(value) ? value : null;
  }
  if (windowView) return null;
  const value = selectedRound != null ? Number(selectedRound) : null;
  return value != null && !Number.isNaN(value) ? value : null;
}

function scopeBar() {
  $('project-label').textContent = (data.lists?.projects || []).find(row => row.id === project)?.label || 'Agent Plugins';
  const row = currentPrRow();
  $('pr-label').textContent = row ? `#${row.pr}` : (pr && pr !== 'current' ? `#${pr}` : '');
  const shown = breadcrumbRound(round, run, data.run, Boolean((from && to) || scope === 'window'));
  $('round-label').textContent = shown != null ? `R${shown}` : 'R—';
  $('time-label').textContent = timeLabel;
}

function isWindow() {
  return Boolean(run || (from && to) || scope === 'window' || data.scope === 'window');
}

function groupByState() {
  // Render the server-computed state / window_reason; never recompute it here.
  const states = isWindow()
    ? ['regressed', 'new', 'reoccurred', 'fixed', 'worked', 'inconclusive']
    : ['regressed', 'blocked', 'in_progress', 'untested', 'parked', 'resolved'];
  return states.map(s => [s, data.issues.filter(i => isWindow() ? i.window_reason === s : i.state === s)])
    .filter(([, list]) => list.length);
}

function needRows() {
  const failures = data.issues
    .filter(i => ['regressed', 'blocked'].includes(i.state))
    .sort((a, b) => (b.state === 'regressed') - (a.state === 'regressed'))
    .slice(0, 2);
  const fresh = data.issues.filter(i => i.state === 'untested').length;
  const rows = failures.map(i => `<div class="need" title="${esc(i.last_evidence?.summary || i.summary)}"><span class="id mono"><span class="dot" style="background:${colors[i.state]}"></span>${esc(i.id)}</span><span class="what">${esc(i.state === 'regressed' ? 'Failed after resolution' : `Blocked · ${i.attempts || 0} failures`)}</span><button class="act" data-open="${esc(i.id)}">Review</button></div>`);
  if (fresh && rows.length < 3) {
    rows.push(`<div class="need"><span class="id mono"><span class="dot" style="background:${colors.untested}"></span>${fresh} untested</span><span class="what">No attempts</span><button class="act" data-group="untested">Show</button></div>`);
  }
  return rows.length ? rows.join('') : '<div class="empty">Nothing needs your attention</div>';
}

function lastEvidence(i) {
  const runScoped = i.check_outcome != null || i.check_at;
  const e = runScoped
    ? {at: i.check_at, kind: i.check_outcome, summary: i.check_reason}
    : (i.last_evidence || {});
  if (!e.at) return ['', 'muted', 'No check on record'];
  const kind = ['pass', 'fixed', 'resolved', 'passed'].includes(e.kind) ? 'pass'
    : ['fail', 'review_failed', 'owner_rejected', 'not_fixed', 'failed'].includes(e.kind) ? 'fail'
    : (e.kind === 'inconclusive' || e.kind === 'blocked' || e.kind === 'stopped') ? 'inconclusive' : 'run';
  const mark = kind === 'pass' ? '✓ ' : kind === 'fail' ? '× ' : kind === 'inconclusive' ? '○ ' : '● ';
  return [`${mark}${formatTime(e.at, !isToday(e.at))}`, kind, `${i.check_reason || e.summary || e.kind} · ${formatTime(e.at, true)}`];
}

function rows(list, key) {
  const ordered = [...list].sort((a, b) => (b.state === 'regressed') - (a.state === 'regressed') || String(b.last_seen_at || '').localeCompare(String(a.last_seen_at || '')));
  const shown = showAll ? ordered : ordered.slice(0, 8);
  return `<div class="rows">${shown.map(i => {
    const [t, c, tip] = lastEvidence(i);
    const carried = i.origin_label && String(i.origin_label).includes('carried')
      ? `<span class="muted origin"> · ${esc(i.origin_label)}</span>` : '';
    return `<button class="row" data-open="${esc(i.id)}" title="${esc(tip)}"><span class="mono">${esc(i.id)}</span><span class="title" title="${esc(i.title)}">${esc(i.title)}${carried}</span><span class="e ${c}">${esc(t)}</span></button>`;
  }).join('')}${ordered.length > 8 && !showAll ? `<button class="more" data-more="${esc(key)}">Show all ${ordered.length}</button>` : ''}</div>`;
}

function headline() {
  const counts = data.counts || {};
  const byState = counts.by_state || {};
  const byReason = counts.by_reason || {};
  const total = data.issues.length;
  const prLabel = `#${prNumber()}`;
  const rLabel = data.round != null ? `R${data.round}` : (round != null ? `R${round}` : '');
  if (data.run_note && !data.run) {
    const other = data.last_other_run ? `last: ${runLabel(data.last_other_run)}` : '';
    return `${esc(data.run_note)}${other ? `<div class="d" style="margin-top:8px;font-size:1rem">${esc(other)}</div>` : ''}`;
  }
  if (isWindow()) {
    if (data.run && (counts.tested != null || byReason.inconclusive || byReason.fixed)) {
      const tested = counts.tested ?? total;
      const fixed = counts.fixed ?? byReason.fixed ?? 0;
      const inconclusive = counts.inconclusive ?? byReason.inconclusive ?? 0;
      const bits = [`${tested} tested`, `${fixed} fixed`, `${inconclusive} inconclusive`];
      if (byReason.regressed) bits.splice(1, 0, `<span class="bad">Regressed ${byReason.regressed}</span>`);
      return `${bits.join('<span class="d">·</span>')}<div class="d" style="margin-top:8px;font-size:1rem">${esc(runLabel(data.run) || timeLabel)}</div>`;
    }
    const bits = [`${total} <span class="d">in window</span>`];
    if (byReason.new) bits.push(`New ${byReason.new}`);
    if (byReason.regressed) bits.push(`<span class="bad">Regressed ${byReason.regressed}</span>`);
    if (byReason.reoccurred) bits.push(`Failed again ${byReason.reoccurred}`);
    if (byReason.fixed) bits.push(`Fixed ${byReason.fixed}`);
    if (byReason.inconclusive) bits.push(`Inconclusive ${byReason.inconclusive}`);
    const quiet = data.quiet_excluded;
    const runBit = data.run ? runLabel(data.run) : timeLabel;
    return `${bits.join('<span class="d">·</span>')}${quiet ? `<span class="d">·</span>${quiet} quiet` : ''}<div class="d" style="margin-top:8px;font-size:1rem">${esc(runBit)}</div>`;
  }
  const blocked = byState.blocked || 0;
  const gate = data.gate || {};
  const verified = gate.verified;
  const threshold = gate.threshold;
  const gateText = typeof threshold === 'number' ? `${verified ?? 0}/${threshold} verified` : (typeof verified === 'number' ? `${verified} verified` : '');
  const last = data.last_fail?.at ? `last fail ${formatTime(data.last_fail.at)}` : '';
  const parts = [prLabel, rLabel, blocked ? `<span class="bad">${blocked} blocked</span>` : null, gateText, last].filter(Boolean);
  return parts.join('<span class="d">·</span>');
}

function renderNow() {
  const groups = groupByState();
  const total = data.issues.length || 1;
  const legendItems = groups.map(([s]) => [s, labels[s] || s]);
  let h = `<div class="headline">${headline()}</div><div class="sbar" role="img" aria-label="Issue status mix">${groups.map(([s, list]) => `<span style="width:${list.length / total * 100}%;background:${colors[s]}" title="${labels[s]} ${list.length}"></span>`).join('')}</div><div class="legend">${legendItems.map(([s, l]) => `<span title="${l}"><i style="background:${colors[s]}"></i>${l}</span>`).join('')}</div>`;
  if (!isWindow()) h += `<h2 class="section-title">Needs you</h2><div class="list">${needRows()}</div>`;
  h += `<h2 class="section-title">Issues</h2><div class="list">`;
  for (const [s, list] of groups) {
    const expanded = openGroup === s;
    h += `<button class="grp" data-toggle="${s}" aria-expanded="${expanded}" title="${esc(labels[s])}"><span class="dot" style="background:${colors[s]}"></span>${labels[s]} <span class="n">${list.length}</span><span class="chev">›</span></button>${expanded ? rows(list, s) : ''}`;
  }
  return h + '</div>';
}

function drawerHTML() {
  const i = data.issues.find(x => x.id === drawer);
  if (!i) return '';
  const f = i.first_seen || {};
  const ev = [...(i.events || [])].sort((a, b) => new Date(a.at) - new Date(b.at) || (a.kind === 'first_seen' ? -1 : 0));
  return `<div class="scrim" data-close></div><aside class="drawer" id="issue-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><button class="x" data-close aria-label="Close">×</button><div class="mono muted">${esc(i.id)}</div><h2 id="drawer-title" tabindex="-1">${esc(i.title)}</h2><dl class="facts"><dt>Status</dt><dd>${esc(labels[i.state] || i.state)}</dd><dt>Area</dt><dd>${esc(i.area || 'Not recorded')}</dd><dt>Origin</dt><dd>${esc(i.origin_label || `${i.origin || ''}${i.origin_pr ? ` · PR #${i.origin_pr}` : ''}${i.carried_to ? ` → #${i.carried_to}` : ''}`)}</dd><dt>First seen</dt><dd>${f.at ? formatTime(f.at, true) : 'Not recorded'}</dd><dt>Last fail</dt><dd>${i.last_seen_at ? formatTime(i.last_seen_at, true) : 'No failure recorded'}</dd><dt>Attempts</dt><dd>${esc(i.attempts || 0)}</dd><dt>Lane</dt><dd>${esc(i.lane?.id || 'No lane')}</dd></dl><h3>What we know</h3><p>${esc(i.summary || 'No summary recorded.')}</p>${i.impact ? `<p>${esc(i.impact)}</p>` : ''}${i.done_when ? `<p>${esc(i.done_when)}</p>` : ''}<h3>Timeline</h3><ul class="tl">${ev.map(e => `<li class="${e.kind === 'pass' ? 'pass-dot' : ['fail', 'review_failed', 'owner_rejected'].includes(e.kind) ? 'fail-dot' : e.kind === 'inconclusive' ? 'inconclusive-dot' : ''}"><time>${esc(formatTime(e.at, true))}</time><span>${esc(e.summary || e.kind)}${publicRef(e.ref)}</span></li>`).join('') || '<li><span class="muted">No events recorded</span></li>'}</ul><details><summary>Raw data</summary><pre>${esc(JSON.stringify(i.raw, null, 2))}</pre></details></aside>`;
}

function recentRuns() {
  const cutoff = Date.now() - 48 * 3600000;
  return (data.runs || []).filter(row => {
    const start = new Date(row.started_at || row.installed_at || 0).getTime();
    return start >= cutoff;
  }).sort((a, b) => new Date(b.started_at || b.installed_at || 0) - new Date(a.started_at || a.installed_at || 0));
}

function setupPopovers() {
  const projects = data.lists?.projects || [{id: project, label: 'Agent Plugins'}];
  $('project-choices').innerHTML = projects.map(row => `<button class="chip" data-project="${esc(row.id)}" aria-current="${row.id === project}">${esc(row.label)}</button>`).join('');
  $('pr-choices').innerHTML = currentPrs().map(row => {
    const detail = row.status === 'merged' ? 'merged' : `${row.open ?? 0} open`;
    const extra = row.cycle != null ? ` · c${row.cycle}` : '';
    return `<button class="chip" data-pr="${esc(row.pr)}" aria-current="${String(row.pr) === String(pr)}"><span>#${esc(row.pr)}${extra}</span><span class="meta">${esc(detail)}</span></button>`;
  }).join('') || '<div class="muted">No pull requests recorded</div>';
  const rounds = (data.rounds || []).filter(row => String(row.pr) === String(pr) && row.round != null)
    .sort((a, b) => Number(b.round) - Number(a.round));
  $('round-choices').innerHTML = rounds.map(row => {
    const live = row.live ? ' live' : '';
    const counts = [row.found ? `found ${row.found}` : null, row.fixed ? `fixed ${row.fixed}` : null, row.regressed ? `regressed ${row.regressed}` : null].filter(Boolean).join(' · ');
    return `<button class="chip" data-round="${esc(row.round)}" aria-current="${Number(row.round) === Number(round)}"><span>R${esc(row.round)}${live}</span><span class="meta">${esc(counts || 'no movement recorded')}</span></button>`;
  }).join('') || '<div class="muted">No rounds recorded</div>';
  $('presets').innerHTML = PRESETS.map(name => `<button data-preset="${name}" aria-current="${timeLabel === name || (name === 'Last test run' && run === 'last')}">${name}</button>`).join('');
  $('from').value = from && !String(from).startsWith('now') ? textInput(from) : '';
  $('to').value = to && to !== 'now' ? textInput(to) : '';
  const snaps = [];
  for (const row of rounds.slice(0, 6)) {
    if (row.started_at && (row.ended_at || row.live)) {
      snaps.push(`<button class="chip" data-snap-from="${esc(row.started_at)}" data-snap-to="${esc(row.ended_at || 'now')}">#${esc(row.pr)} R${esc(row.round)}</button>`);
    }
  }
  for (const row of recentRuns().slice(0, 8)) {
    const recorded = (row.started_at || row.installed_at) && (row.ended_at || row.end);
    const label = recorded ? runLabel(row) : `${row.pr ? '#' + row.pr : ''} run · run time not recorded`.trim();
    if (recorded) snaps.push(`<button class="chip" data-run="${esc(row.id || row.run_id)}">${esc(label)}</button>`);
    else snaps.push(`<button class="chip" type="button" data-run-missing="${esc(row.id || row.run_id || '')}">${esc(label)}</button>`);
  }
  $('snaps').innerHTML = snaps.join('');
  drawHist();
}

function histRange() {
  const end = Date.now();
  return {start: end - 24 * 3600000, end, step: 30 * 60000};
}

function drawHist() {
  const hist = $('hist');
  const {start, end, step} = histRange();
  const bins = Array(48).fill(0);
  for (const issue of data.issues || []) {
    for (const event of issue.events || []) {
      const t = new Date(event.at).getTime();
      if (t >= start && t <= end) bins[Math.min(47, Math.floor((t - start) / step))] += 1;
    }
  }
  const max = Math.max(1, ...bins);
  const span = end - start;
  const bands = recentRuns().map(row => {
    const a = new Date(row.started_at || row.installed_at || 0).getTime();
    const b = new Date(row.ended_at || row.end || 0).getTime();
    if (!a || !b || !span) return null;
    const left = (a - start) / span * 100;
    const right = (b - start) / span * 100;
    if (right <= 0 || left >= 100) return null;
    const clippedLeft = Math.max(0, left);
    const clippedRight = Math.min(100, right);
    return {id: row.id || row.run_id, left: clippedLeft, width: Math.max(0.5, clippedRight - clippedLeft)};
  }).filter(Boolean);
  const windowStart = from && to && !String(from).startsWith('now') ? new Date(from).getTime() : (data.window?.from ? new Date(data.window.from).getTime() : start);
  const windowEnd = from && to && to !== 'now' ? new Date(to).getTime() : (data.window?.to ? new Date(data.window.to).getTime() : end);
  hist.innerHTML = bands.map(band => `<span class="run-band" data-run="${esc(band.id)}" style="position:absolute;left:${band.left}%;width:${band.width}%;top:0;bottom:0;background:#1f232614;cursor:pointer" title="Select this test run"></span>`).join('')
    + bins.map((n, i) => {
      const t = start + i * step;
      const dim = t < windowStart || t > windowEnd ? ' dim' : '';
      return `<i class="${dim}" data-bin="${i}" style="height:${Math.max(6, n / max * 100)}%"></i>`;
    }).join('')
    + `<span class="brush" hidden></span>`;
  const fromSlider = $('range-from'), toSlider = $('range-to');
  const a = Math.max(0, Math.min(47, Math.round((windowStart - start) / step)));
  const b = Math.max(0, Math.min(47, Math.round((windowEnd - start) / step)));
  fromSlider.value = Math.min(a, b);
  toSlider.value = Math.max(a, b);
}

function bucketFromX(clientX) {
  const rect = $('hist').getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  return Math.max(0, Math.min(47, Math.floor(ratio * 48)));
}

function applyBrush(a, b, loadIt = false) {
  if (a > b) [a, b] = [b, a];
  const {start, step} = histRange();
  const snap = 5 * 60000;
  const fromMs = Math.round((start + a * step) / snap) * snap;
  const toMs = Math.round((start + (b + 1) * step) / snap) * snap;
  const fromDate = new Date(fromMs);
  const toDate = new Date(toMs);
  const local = d => {
    const p = istParts(d.toISOString());
    return `${p.year}-${pad(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].indexOf(p.month)+1)}-${pad(p.day)}T${p.hour}:${p.minute}:00+05:30`;
  };
  from = local(fromDate);
  to = local(toDate);
  run = null;
  scope = 'window';
  timeLabel = 'Custom';
  $('from').value = textInput(from);
  $('to').value = textInput(to);
  $('range-from').value = a;
  $('range-to').value = b;
  updateUrl();
  if (loadIt) load();
}

function bindHist() {
  const hist = $('hist');
  hist.onpointerdown = event => {
    if (event.target.closest('[data-run]')) return;
    dragging = {origin: bucketFromX(event.clientX), current: bucketFromX(event.clientX)};
    hist.setPointerCapture(event.pointerId);
    applyBrush(dragging.origin, dragging.current, false);
  };
  hist.onpointermove = event => {
    if (!dragging) return;
    dragging.current = bucketFromX(event.clientX);
    applyBrush(dragging.origin, dragging.current, false);
  };
  hist.onpointerup = event => {
    if (!dragging) return;
    dragging.current = bucketFromX(event.clientX);
    applyBrush(dragging.origin, dragging.current, true);
    dragging = null;
  };
}

function trapFocus(event) {
  const drawerNode = document.getElementById('issue-drawer');
  if (!drawerNode || event.key !== 'Tab') return;
  const nodes = [...drawerNode.querySelectorAll('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])')];
  if (!nodes.length) return;
  const first = nodes[0], last = nodes[nodes.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    last.focus();
    event.preventDefault();
  } else if (!event.shiftKey && document.activeElement === last) {
    first.focus();
    event.preventDefault();
  }
}

function openDrawer(id) {
  lastFocus = document.activeElement;
  openerSelector = openerFor(id, lastFocus);
  drawer = id;
  showAll = showAll;
  updateUrl();
  render();
  requestAnimationFrame(() => {
    const close = document.querySelector('.drawer .x') || document.getElementById('drawer-title');
    close?.focus();
  });
}

function closeDrawer() {
  const returnTo = drawer;
  const selector = openerSelector;
  drawer = null;
  openerSelector = null;
  updateUrl();
  render();
  const row = (selector && document.querySelector(selector))
    || (returnTo ? document.querySelector(`.rows .row[data-open="${returnTo}"]`) : null);
  if (row) row.focus();
  else if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  lastFocus = null;
}

function render() {
  scopeBar();
  $('content').innerHTML = renderNow();
  $('layer').innerHTML = drawer ? drawerHTML() : '';
  setupPopovers();
  bindHist();
}

function scale(delta) {
  const current = Number(getComputedStyle(document.documentElement).getPropertyValue('--scale').trim()) || 1;
  const next = Math.max(0.9375, Math.min(1.375, current + delta));
  document.documentElement.style.setProperty('--scale', next);
  try { localStorage.setItem('devobs-scale', String(next)); } catch {}
}

document.addEventListener('click', event => {
  const band = event.target.closest('[data-run]');
  if (band?.dataset.run) { setRun(band.dataset.run); return; }
  const missing = event.target.closest('[data-run-missing]');
  if (missing) {
    timeLabel = 'run time not recorded';
    $('time-label').textContent = timeLabel;
    return;
  }
  const b = event.target.closest('button');
  if (!b) {
    if (!event.target.closest('.popover')) closePops();
    return;
  }
  if (b.id === 'time') { toggle('time-pop'); return; }
  if (b.id === 'round') { toggle('round-pop'); return; }
  if (b.id === 'pr') { toggle('pr-pop'); return; }
  if (b.id === 'project') { toggle('project-pop'); return; }
  if (b.id === 'smaller') { scale(-0.0625); return; }
  if (b.id === 'larger') { scale(0.0625); return; }
  if (b.dataset.project) {
    applyCascade({project: b.dataset.project});
    run = from = to = null;
    scope = 'round:' + round;
    timeLabel = 'Now';
    closePops();
    updateUrl();
    load();
    return;
  }
  if (b.dataset.pr) {
    applyCascade({pr: b.dataset.pr});
    run = from = to = null;
    scope = 'round:' + round;
    timeLabel = 'Now';
    closePops();
    updateUrl();
    load();
    return;
  }
  if (b.dataset.round) {
    applyCascade({round: b.dataset.round});
    run = from = to = null;
    scope = 'round:' + round;
    timeLabel = 'Now';
    closePops();
    updateUrl();
    load();
    return;
  }
  if (b.dataset.open) { closePops(); openDrawer(b.dataset.open); return; }
  if (b.dataset.close !== undefined) { closeDrawer(); return; }
  if (b.dataset.toggle) {
    openGroup = openGroup === b.dataset.toggle ? null : b.dataset.toggle;
    showAll = false;
    updateUrl();
    render();
    return;
  }
  if (b.dataset.group) {
    openGroup = b.dataset.group;
    updateUrl();
    render();
    document.querySelector(`[data-toggle="${b.dataset.group}"]`)?.scrollIntoView({block: 'center'});
    return;
  }
  if (b.dataset.more) { showAll = true; render(); return; }
  if (b.dataset.preset) { setPreset(b.dataset.preset); return; }
  if (b.dataset.snapFrom) {
    from = b.dataset.snapFrom;
    to = b.dataset.snapTo;
    run = null;
    scope = 'window';
    timeLabel = 'Custom';
    updateUrl();
    load();
  }
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    if (drawer) { closeDrawer(); event.preventDefault(); return; }
    closePops();
  }
  trapFocus(event);
});

$('from').onchange = setWindow;
$('to').onchange = setWindow;
$('range-from').onchange = () => applyBrush(Number($('range-from').value), Number($('range-to').value), true);
$('range-to').onchange = () => applyBrush(Number($('range-from').value), Number($('range-to').value), true);

function init() {
  try {
    const stored = localStorage.getItem('devobs-scale');
    if (stored) document.documentElement.style.setProperty('--scale', stored);
  } catch {}
  urlState();
  $('time-label').textContent = timeLabel;
  if (http) load();
  else {
    app.ontoolresult = result => {
      data = result.structuredContent ?? JSON.parse(result.content?.find(x => x.type === 'text')?.text ?? '{}');
      render();
    };
    app.connect().then(() => load()).catch(e => {
      $('error').hidden = false;
      $('error').textContent = e.message;
    });
  }
}
init();
