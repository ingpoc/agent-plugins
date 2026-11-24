const IST = 'Asia/Kolkata';
const TEXT_SCALES = [0.875, 1, 1.125, 1.25, 1.375];

function istTime(iso, withDate = false) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const opts = withDate
    ? {timeZone: IST, day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit'}
    : {timeZone: IST, hour: 'numeric', minute: '2-digit'};
  return new Intl.DateTimeFormat('en-IN', opts).format(date) + ' IST';
}

function relative(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  const abs = Math.abs(seconds);
  const units = [['day', 86400], ['hour', 3600], ['minute', 60], ['second', 1]];
  for (const [name, size] of units) {
    if (abs >= size || name === 'second') {
      const count = Math.round(abs / size);
      const rtf = new Intl.RelativeTimeFormat('en', {numeric: 'auto'});
      return rtf.format(seconds >= 0 ? -count : count, name);
    }
  }
  return '';
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function statusClass(status) {
  const value = String(status || '').toLowerCase();
  if (['verified', 'verified_fixed', 'passed', 'fixed', 'done'].includes(value)) return 'ok';
  if (['blocked', 'failed', 'error'].includes(value)) return 'bad';
  if (['deferred', 'parked'].includes(value)) return 'idle';
  if (['review', 'in_progress', 'running', 'active', 'untested'].includes(value)) return 'run';
  return 'idle';
}

function shortName(group) {
  const raw = group.label || group.name || group.id || '';
  return String(raw).replace(/^R\d+-/, '');
}

function durationLabel(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  const minutes = ms / 60000;
  if (minutes < 60) return `${minutes < 10 ? minutes.toFixed(1) : Math.round(minutes)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

function formatTokens(n) {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function currentScale() {
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--text-scale').trim();
  const value = Number.parseFloat(raw);
  return TEXT_SCALES.includes(value) ? value : 1;
}

function applyScale(scale, output) {
  const next = TEXT_SCALES.includes(scale) ? scale : 1;
  document.documentElement.style.setProperty('--text-scale', String(next));
  document.documentElement.style.fontSize = `calc(16px * ${next})`;
  try { localStorage.setItem('devobs-text-scale', String(next)); } catch {}
  if (output) output.textContent = `${Math.round(next * 100)}%`;
}

export function appliedFilterCount(kindFilter, selectedRound) {
  // The selected round is the view, not a filter.
  void selectedRound;
  return (kindFilter && kindFilter !== 'all') ? 1 : 0;
}

export function displayStatus(group) {
  return String((group && (group.view_status || group.status)) || '').toLowerCase() || 'unknown';
}

export function publicText(value) {
  if (typeof value !== 'string' || !value) return value;
  return value
    .replace(/\[([^\]]+)\]\((?:file:\/\/)?(\/?(?:Users|home)\/[^)]+)\)/gi, '$1')
    .replace(/(?:file:\/\/)?\/?(?:Users|home)\/[^\s\]\)\'"<>]+/gi, (path) => {
      const parts = path.replace(/^file:\/\//i, '').split('/').filter(Boolean);
      return parts[parts.length - 1] || 'file';
    });
}

export function mountRoundView(ctx) {
  const {call, $, text, filters, applyTextSize, TEXT_SIZES} = ctx;
  const root = document.getElementById('round-view');
  if (!root) return;
  document.body.classList.add('round-mode');
  root.hidden = false;
  const desk = document.getElementById('desk');
  if (desk) desk.hidden = true;

  let scale = 1;
  let kindFilter = 'all';
  let currentProject = null;
  let currentData = null;
  try {
    const stored = Number.parseFloat(localStorage.getItem('devobs-text-scale') || '');
    if (TEXT_SCALES.includes(stored)) scale = stored;
    else {
      const legacy = Number.parseInt(localStorage.getItem('devobs-text-size') || '', 10);
      if (Number.isFinite(legacy)) scale = TEXT_SCALES.reduce((best, item) => Math.abs(item * 16 - legacy) < Math.abs(best * 16 - legacy) ? item : best, 1);
    }
  } catch {}

  function ensureChrome() {
    if (document.getElementById('round-chrome')) return;
    const chrome = el('div', {id: 'round-chrome'});
    const err = el('p', {id: 'round-error', class: 'banner', hidden: true, role: 'alert'});
    const body = el('div', {id: 'round-body'});
    const host = el('div', {id: 'round-toolbar-host'});
    const switcher = el('div', {id: 'round-switch'});
    const toolbar = document.querySelector('.toolbar');
    if (toolbar) {
      toolbar.classList.add('embedded');
      toolbar.hidden = true;
      const move = (selector) => {
        const node = toolbar.querySelector(selector);
        if (node) switcher.append(node);
      };
      move('label:has(#project)');
      move('label:has(#version)');
      const refresh = document.getElementById('refresh');
      if (refresh) switcher.append(refresh);
    }
    host.append(switcher);
    chrome.append(host);
    root.replaceChildren(chrome, err, body);
  }

  function showError(message) {
    ensureChrome();
    const err = document.getElementById('round-error');
    err.hidden = !message;
    err.textContent = message || '';
    const legacy = $('error');
    if (legacy) legacy.textContent = message || '';
  }

  function emptyState(title, detail) {
    ensureChrome();
    const body = document.getElementById('round-body');
    body.replaceChildren(
      el('section', {class: 'empty'}, [
        el('h2', {text: title}),
        el('p', {class: 'muted', text: detail}),
      ]),
    );
  }

  async function load(project, pr, round) {
    ensureChrome();
    currentProject = project;
    const body = document.getElementById('round-body');
    showError('');
    body.replaceChildren(el('p', {class: 'muted', text: 'Loading the round view…'}));
    try {
      const data = await call('observability_round', {
        project,
        ...(pr ? {pr} : {}),
        ...(round ? {round: Number(round)} : {}),
      });
      currentData = data;
      render(data, project);
    } catch (error) {
      const message = error.message || 'Round view unavailable';
      currentData = null;
      const missing = String(message).startsWith('tracker_unavailable');
      showError(missing ? '' : message);
      renderChrome(null, project);
      emptyState(
        missing ? 'No change-list round' : 'Could not load this round',
        missing
          ? 'This project has no change-list tracker. Choose another project, or go back to the last one with Refresh after you pick it.'
          : message,
      );
    }
  }

  function renderChrome(data, project) {
    ensureChrome();
    const chrome = document.getElementById('round-chrome');
    const host = document.getElementById('round-toolbar-host');
    const prLabel = data?.pr?.label || (data?.pr?.number ? `PR #${data.pr.number}` : 'Change list');
    const updated = data?.updated_at;
    const started = data?.round_started_at;
    const stale = isStale(updated);
    const scaleOut = el('output', {id: 'ts-val', 'aria-live': 'polite', text: `${Math.round(currentScale() * 100)}%`});
    applyScale(currentScale() || scale, scaleOut);

    const filterCount = appliedFilterCount(kindFilter, data?.round);
    const pop = el('details', {class: 'pop', id: 'round-filter-pop'}, [
      el('summary', {'aria-label': `Filter, ${filterCount} applied`}, [
        'Filter ',
        el('span', {class: 'n', text: String(filterCount)}),
      ]),
      el('div', {class: 'sheet slim'}, [
        el('label', {}, [
          'Kind ',
          el('select', {id: 'round-kind', 'aria-label': 'Kind', onchange: (event) => {
            kindFilter = event.target.value || 'all';
            if (currentData) render(currentData, project);
          }}, [
            ...[['all', 'All'], ['failure', 'Failure'], ['blocked', 'Blocked'], ['failed_attempts', 'Failed attempts']].map(([value, label]) => {
              const option = el('option', {text: label});
              option.value = value;
              if (value === kindFilter) option.selected = true;
              return option;
            }),
          ]),
        ]),
      ]),
    ]);

    const header = el('header', {class: 'top'}, [
      el('div', {}, [
        el('p', {class: 'eyebrow', text: `Developer observability · ${project || 'No project'}`}),
        el('h1', {}, [
          data ? `${prLabel} · Round ${data.round ?? '—'} ` : 'No change-list round ',
          el('span', {class: 'muted', text: data?.cycle != null ? `· Cycle ${data.cycle}` : ''}),
        ]),
        el('p', {class: 'fresh'}, [
          updated ? el('span', {}, [
            'Updated ',
            el('time', {datetime: updated, text: istTime(updated)}),
            ' · ',
            el('span', {text: relative(updated)}),
          ]) : el('span', {class: 'muted', text: 'No tracker update time'}),
          started ? el('span', {}, [
            ' · Round started ',
            el('time', {datetime: started, text: istTime(started)}),
          ]) : (data ? el('span', {class: 'muted', text: ' · Round start not stored'}) : null),
          el('span', {}, [
            ' · Snapshot ',
            updated ? el('time', {datetime: updated, text: istTime(updated, true)}) : el('span', {text: 'not stored'}),
            stale ? el('span', {class: 'tag stale', text: 'Stale'}) : null,
          ]),
        ]),
      ]),
      el('div', {class: 'controls'}, [
        el('div', {class: 'seg', role: 'group', 'aria-label': 'Text size'}, [
          el('button', {type: 'button', 'aria-label': 'Smaller text', text: 'A−', onclick: () => {
            const i = Math.max(0, TEXT_SCALES.indexOf(currentScale()) - 1);
            applyScale(TEXT_SCALES[i], scaleOut);
          }}),
          scaleOut,
          el('button', {type: 'button', 'aria-label': 'Larger text', text: 'A+', onclick: () => {
            const i = Math.min(TEXT_SCALES.length - 1, TEXT_SCALES.indexOf(currentScale()) + 1);
            applyScale(TEXT_SCALES[i], scaleOut);
          }}),
        ]),
        pop,
        host,
      ]),
    ]);

    const chips = el('nav', {class: 'rounds', 'aria-label': 'Rounds'}, (data?.round_history || []).map((item) => {
      const current = item.round === data.round && (item.cycle == null || item.cycle === data.cycle);
      const fixed = item.live ? 'live' : `${item.fixed ?? 0} fixed`;
      return el('button', {
        type: 'button',
        'aria-current': current ? 'page' : null,
        text: `R${item.round} · ${fixed}`,
        'aria-label': `Round ${item.round}, cycle ${item.cycle ?? data.cycle ?? 'unknown'}, ${fixed}`,
        onclick: () => load(project, data.pr?.number, item.round),
      });
    }));

    chrome.replaceChildren(header, chips);
  }

  function render(data, project) {
    renderChrome(data, project);
    const body = document.getElementById('round-body');
    const counts = data.status_counts || {};
    const verified = counts.verified_fixed || 0;
    const gate = data.gate || {};
    const threshold = gate.threshold;
    const viewingCurrent = data.viewing_current !== false;

    const needsCards = (data.needs_you || []).filter((card) => kindAllows(card.kind));
    const fallback = data.fallback || {};
    const listNote = fallback.from_pr ? el('p', {class: 'list-note'}, [
      fallback.auto
        ? `No lanes yet in #${fallback.from_pr} · viewing #${fallback.viewing_pr}`
        : `No lanes yet in #${fallback.from_pr}`,
      (fallback.suggest_pr || (fallback.auto && fallback.from_pr)) ? el('button', {
        type: 'button',
        text: fallback.auto ? `View #${fallback.from_pr}` : `View #${fallback.suggest_pr}`,
        onclick: () => load(project, fallback.auto ? fallback.from_pr : fallback.suggest_pr),
      }) : null,
    ]) : null;
    const needs = el('section', {'aria-labelledby': 'needs-h'}, [
      el('h2', {id: 'needs-h'}, [
        'Needs you ',
        el('small', {text: needsCards.length ? `${needsCards.length} ranked · evidence first` : 'Nothing waiting'}),
      ]),
      el('div', {class: 'needs'}, needsCards.map((card) => {
        const action = card.action || (card.group_id ? {label: 'Open lane', target: card.group_id} : null);
        return el('article', {class: 'card need'}, [
          el('p', {class: 'kicker', text: `${card.rank}. ${labelKind(card.view_status || card.kind)}`}, card.group_id ? [el('span', {class: 'mono', text: ` · ${card.group_id}`})] : []),
          el('h3', {text: publicText(card.title) || ''}),
          card.fact ? el('p', {class: 'fact'}, [el('b', {text: 'Fact. '}), publicText(card.fact)]) : null,
          card.gap ? el('p', {class: 'gap'}, [el('b', {text: 'Gap. '}), publicText(card.gap)]) : null,
          card.guess ? el('p', {class: 'guess'}, [el('b', {text: 'Guess. '}), publicText(card.guess)]) : null,
          !card.fact && card.why ? el('p', {class: 'fact'}, [el('b', {text: 'Fact. '}), publicText(card.why)]) : null,
          card.evidence ? el('p', {class: 'muted ev-line', text: `Evidence: ${publicText(card.evidence)}`}) : null,
          action ? el('button', {type: 'button', class: 'act', text: action.label || 'Open lane', onclick: () => openLane(action.target || card.group_id)}) : null,
        ]);
      })),
      data.needs_you_more ? el('details', {class: 'more'}, [
        el('summary', {text: `${data.needs_you_more} more · FYI`}),
        el('p', {class: 'muted', text: 'Lower-priority notes are not shown as cards.'}),
      ]) : null,
    ]);

    const ringLabel = !viewingCurrent
      ? `${gate.fixed ?? 0} verified in this finished round`
      : (threshold == null
        ? `${verified} verified fixed`
        : `${gate.fixed ?? 0} / ${threshold} to the merge gate`);
    const summaryBits = viewingCurrent ? [
      `${verified} verified fixed`,
      counts.in_progress ? ` · ${counts.in_progress} in progress` : '',
      counts.untested ? ` · ${counts.untested} untested` : '',
      counts.blocked ? ` · ${counts.blocked} blocked` : '',
      counts.deferred ? ` · ${counts.deferred} deferred` : '',
    ].join('') : ringLabel;
    const summary = el('section', {class: 'summary', 'aria-labelledby': 'sum-h'}, [
      el('h2', {id: 'sum-h', class: 'sr', text: 'Round summary'}),
      el('div', {class: 'gate'}, [
        (threshold == null && !(viewingCurrent ? verified : (gate.fixed || verified))
          ? null
          : gateRing(threshold != null && viewingCurrent
            ? (gate.fixed || 0) / Math.max(threshold, 1)
            : 1, ringLabel)),
        el('p', {}, [summaryBits]),
      ]),
    ]);

    const liveGroups = data.groups || [];
    const selected = data.round;
    const currentGroups = liveGroups.filter((group) => groupBelongs(group, selected) && !isParkedGroup(group) && kindAllows(displayStatus(group)));
    const parkedGroups = liveGroups.filter((group) => isParkedGroup(group) || (selected != null && !groupBelongs(group, selected) && data.viewing_current));
    const jobsFor = (id) => (data.jobs || []).filter((job) => job.group_id === id && (selected == null || job.round == null || job.round === selected));

    const lanes = el('section', {'aria-labelledby': 'lanes-h'}, [
      el('h2', {id: 'lanes-h', text: 'Lanes'}),
      el('div', {class: 'lhead', 'aria-hidden': 'true'}, ['Lane', 'Phase', 'Why', 'Min', 'Tokens', 'Model', ''].map((label) => el('span', {text: label}))),
      el('div', {class: 'lanes'}, currentGroups.map((group) => {
        const jobs = jobsFor(group.id);
        const tokens = jobs.reduce((sum, job) => sum + (typeof job.tokens?.total === 'number' ? job.tokens.total : 0), 0);
        const minutes = jobs.reduce((sum, job) => sum + (typeof job.duration_ms === 'number' ? job.duration_ms : 0), 0);
        const model = jobs.map((job) => job.model || job.agent).find(Boolean) || '—';
        const why = publicText(group.why || ((displayStatus(group) === 'blocked') ? (group.blocked_reason || group.reason) : group.reason) || '');
        const node = el('details', {class: `lane ${statusClass(displayStatus(group))}`, id: `lane-${group.id}`}, [
          el('summary', {}, [
            el('span', {class: 'id mono', text: group.id}),
            el('span', {class: 'name', text: shortName(group)}),
            el('span', {class: `tag ${statusClass(displayStatus(group))}`, text: phaseTrack(group)}),
            el('span', {class: 'why', text: why || '—'}),
            el('span', {class: 'muted', text: durationLabel(minutes) || '—'}),
            el('span', {class: 'muted', text: tokens ? formatTokens(tokens) : '—'}),
            el('span', {class: 'model', text: model}),
          ]),
          el('div', {class: 'lane-body lbody'}, [
            el('h3', {text: 'What we know'}),
            why ? el('p', {text: why}) : el('p', {class: 'muted', text: 'No written reason for this lane.'}),
            group.findings?.length ? el('ul', {}, group.findings.map((finding) => el('li', {}, [
              el('strong', {class: 'mono', text: finding.id}),
              ` ${finding.title || ''} `,
              el('span', {class: 'muted', text: displayStatus(group)}),
            ]))) : null,
            el('h3', {text: 'Jobs'}),
            jobs.length ? el('div', {class: 'table-wrap'}, [el('table', {class: 'jobs'}, [
              el('thead', {}, [el('tr', {}, ['Phase', 'Agent', 'Started', 'Duration', 'Tokens', 'Exit'].map((h) => el('th', {text: h})))]),
              el('tbody', {}, jobs.map((job) => el('tr', {}, [
                el('td', {text: job.phase || ''}),
                el('td', {class: 'mono', text: job.agent || ''}),
                el('td', {}, job.started_at ? [el('time', {datetime: job.started_at, text: istTime(job.started_at)})] : ['—']),
                el('td', {text: durationLabel(job.duration_ms) || '—'}),
                el('td', {text: tokenCell(job)}),
                el('td', {text: job.exit_code == null ? (job.event === 'incomplete' ? 'open' : '—') : String(job.exit_code)}),
              ]))),
            ])]) : el('p', {class: 'muted', text: 'No recorded job for this lane.'}),
            el('details', {class: 'ev'}, [
              el('summary', {text: 'Raw evidence'}),
              el('pre', {class: 'mono', text: JSON.stringify({id: group.id, view_status: displayStatus(group), status: group.status, phase: group.phase, parked_to: isParkedGroup(group) ? group.parked_to : undefined, why: group.why, findings: group.findings || [], jobs}, null, 2)}),
            ]),
          ]),
        ]);
        return node;
      })),
      parkedGroups.length ? el('p', {class: 'parked', text: parkedLine(parkedGroups)}) : null,
      !currentGroups.length ? el('p', {class: 'muted', text: fallback.suggest_pr
        ? `No lanes yet in #${fallback.from_pr || data.pr?.number} · view #${fallback.suggest_pr}`
        : (data.pr?.number ? `No lanes yet in #${data.pr.number}.` : 'No lanes in this round.')}) : null,
    ]);

    const tt = data.time_tokens || {};
    const unfinished = tt.incomplete || 0;
    const bars = el('section', {'aria-labelledby': 'bars-h'}, [
      el('h2', {id: 'bars-h', text: 'Where time and tokens went'}),
      el('p', {class: 'muted', text: `${tt.coverage || '0/0'} jobs have token coverage · ${formatTokens(tt.tokens || 0)} tokens · ${durationLabel(tt.duration_ms) || 'duration unknown'}`}),
      barRow('Time', (tt.by_lane || []).map((lane) => ({
        id: lane.group_id,
        value: lane.duration_ms || 0,
        unknown: !lane.duration_ms,
      })), tt.duration_ms || 1, durationLabel),
      barRow('Tokens', (tt.by_lane || []).map((lane) => ({
        id: lane.group_id,
        value: lane.tokens || 0,
        unknown: lane.covered === 0,
      })), (tt.tokens || 0) || 1, formatTokens),
      el('p', {class: 'muted', text: unfinished
        ? `${unfinished} job${unfinished === 1 ? '' : 's'} never recorded a finish. Unrecorded session time is not in this bar.`
        : 'Unrecorded session time is not in this bar.'}),
    ]);

    body.replaceChildren(...[listNote, needs, summary, lanes, bars].filter(Boolean));
    const health = $('health');
    if (health) {
      health.textContent = data.updated_at
        ? `Tracker updated ${istTime(data.updated_at)} (${relative(data.updated_at)}). ${ringLabel}.`
        : 'Round view ready.';
    }
  }

  function openLane(id) {
    if (!id) return;
    const node = document.getElementById(`lane-${id}`);
    if (node) {
      node.open = true;
      node.scrollIntoView({block: 'start', behavior: 'smooth'});
    }
  }

  function groupBelongs(group, selected) {
    if (selected == null) return true;
    const match = String(group.id || '').match(/^R(\d+)-/i);
    if (!match) return false;
    return Number(match[1]) === selected;
  }

  function isParkedGroup(group) {
    const status = displayStatus(group);
    return status === 'deferred' || status === 'parked' || group.id === 'DEFER';
  }

  function parkedLine(groups) {
    const names = groups.map((group) => group.id).filter(Boolean);
    if (names.length === 1) return `Parked or deferred: ${names[0]}`;
    return `Parked or deferred: ${names.length} groups (${names.slice(0, 4).join(', ')}${names.length > 4 ? '…' : ''})`;
  }

  function phaseTrack(group) {
    return displayStatus(group) || '—';
  }

  function kindAllows(kind) {
    if (!kindFilter || kindFilter === 'all') return true;
    if (kindFilter === 'failure' || kindFilter === 'failed_attempts') {
      return ['failed', 'failed_attempts', 'blocked', 'error'].includes(String(kind || '').toLowerCase());
    }
    return String(kind || '').toLowerCase() === kindFilter;
  }

  function isStale(iso) {
    if (!iso) return true;
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return true;
    return (Date.now() - date.getTime()) > 36 * 3600 * 1000;
  }

  function gateRing(fraction, label) {
    const value = Math.max(0, Math.min(1, Number(fraction) || 0));
    const c = 2 * Math.PI * 16;
    const dash = `${(value * c).toFixed(2)} ${c.toFixed(2)}`;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 40 40');
    svg.setAttribute('width', '40');
    svg.setAttribute('height', '40');
    svg.setAttribute('class', 'gate-ring');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
    svg.innerHTML = `<circle cx="20" cy="20" r="16" fill="none" stroke="currentColor" stroke-width="3" opacity="0.2"/><circle cx="20" cy="20" r="16" fill="none" stroke="currentColor" stroke-width="3" stroke-dasharray="${dash}" stroke-linecap="round" transform="rotate(-90 20 20)"/>`;
    return svg;
  }

  function barRow(label, parts, total, format) {
    const row = el('div', {class: 'bar-row'});
    row.append(el('p', {class: 'bar-label', text: label}));
    const track = el('div', {class: 'bar', role: 'img', 'aria-label': label});
    const safeTotal = total > 0 ? total : 1;
    if (!parts.length) {
      track.append(el('span', {class: 'hatched', style: 'width:100%', text: 'unknown'}));
    } else {
      for (const part of parts) {
        const width = Math.max(part.unknown && !part.value ? 8 : 0, (part.value / safeTotal) * 100);
        track.append(el('span', {
          class: part.unknown && !part.value ? 'hatched' : 'seg-bar',
          style: `width:${width}%`,
          title: `${part.id}: ${format(part.value)}${part.unknown ? ' (unknown)' : ''}`,
          text: width >= 12 ? part.id : '',
        }));
      }
    }
    row.append(track);
    return row;
  }

  function tokenCell(job) {
    if (job.tokens_source === 'stamp' || job.tokens_source === 'inline') {
      const total = job.tokens && typeof job.tokens.total === 'number' ? job.tokens.total : null;
      return total == null ? 'unknown' : formatTokens(total);
    }
    return 'unknown';
  }

  function labelKind(kind) {
    return ({
      blocked: 'Blocked',
      failed_attempts: 'Failed attempts',
      token_coverage: 'Token coverage',
      incomplete_jobs: 'Incomplete jobs',
      parked: 'Parked',
    })[kind] || kind || 'Note';
  }

  if (!window.__roundFilterEsc) {
    window.__roundFilterEsc = true;
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      const open = document.getElementById('round-filter-pop');
      if (open && open.open) {
        open.open = false;
        event.preventDefault();
      }
    });
  }

  const projectSelect = $('project');
  const boot = async () => {
    ensureChrome();
    const listing = await call('observability_projects', {});
    const projects = listing.projects || [];
    projectSelect.replaceChildren(text('option', 'Choose a project'));
    projectSelect.options[0].value = '';
    for (const item of projects) {
      const option = text('option', item.label || item.project_id);
      option.value = item.project_id;
      projectSelect.append(option);
    }
    const preferred = projects.find((item) => item.project_id === 'agent-plugins') || projects[0];
    if (preferred) {
      projectSelect.value = preferred.project_id;
      await load(preferred.project_id);
    } else {
      renderChrome(null, '');
      emptyState('No project is available yet.', 'Choose a project when one appears.');
    }
  };

  const reload = () => load(currentProject, currentData?.pr?.number, currentData?.round);

  return {load, boot, reload};
}
