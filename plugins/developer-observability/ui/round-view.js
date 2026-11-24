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

export function mountRoundView(ctx) {
  const {call, $, text, filters, applyTextSize, TEXT_SIZES} = ctx;
  const root = document.getElementById('round-view');
  if (!root) return;
  document.body.classList.add('round-mode');
  root.hidden = false;
  const desk = document.getElementById('desk');
  if (desk) desk.hidden = true;

  let scale = 1;
  try {
    const stored = Number.parseFloat(localStorage.getItem('devobs-text-scale') || '');
    if (TEXT_SCALES.includes(stored)) scale = stored;
    else {
      const legacy = Number.parseInt(localStorage.getItem('devobs-text-size') || '', 10);
      if (Number.isFinite(legacy)) scale = TEXT_SCALES.reduce((best, item) => Math.abs(item * 16 - legacy) < Math.abs(best * 16 - legacy) ? item : best, 1);
    }
  } catch {}

  async function load(project, pr, round) {
    root.replaceChildren(el('p', {class: 'muted', text: 'Loading the round view…'}));
    try {
      const data = await call('observability_round', {
        project,
        ...(pr ? {pr} : {}),
        ...(round ? {round: Number(round)} : {}),
      });
      render(data, project);
    } catch (error) {
      root.replaceChildren(el('p', {class: 'muted', text: error.message || 'Round view unavailable'}));
    }
  }

  function render(data, project) {
    const prLabel = data.pr?.label || (data.pr?.number ? `PR #${data.pr.number}` : 'PR');
    const updated = data.updated_at;
    const started = data.round_started_at;
    const counts = data.status_counts || {};
    const verified = counts.verified_fixed || 0;
    const gate = data.gate || {};
    const threshold = gate.threshold;
    const filterCount = countActiveFilters();

    const scaleOut = el('output', {id: 'ts-val', 'aria-live': 'polite', text: `${Math.round(currentScale() * 100)}%`});
    applyScale(currentScale() || scale, scaleOut);

    const sheet = el('div', {class: 'sheet', id: 'round-filter-sheet'});
    const toolbar = document.querySelector('.toolbar');
    if (toolbar && !sheet.contains(toolbar)) {
      toolbar.hidden = false;
      toolbar.classList.add('embedded');
      sheet.append(toolbar);
    }

    const header = el('header', {class: 'top'}, [
      el('div', {}, [
        el('p', {class: 'eyebrow', text: `Developer observability · ${project}`}),
        el('h1', {}, [
          `${prLabel} · Round ${data.round ?? '—'} `,
          el('span', {class: 'muted', text: data.cycle != null ? `· Cycle ${data.cycle}` : ''}),
        ]),
        el('p', {class: 'fresh'}, [
          updated ? el('span', {}, [
            'Updated ',
            el('time', {datetime: updated, text: istTime(updated)}),
            ' · ',
            el('span', {text: relative(updated)}),
          ]) : null,
          started ? el('span', {}, [
            ' · Round started ',
            el('time', {datetime: started, text: istTime(started)}),
          ]) : el('span', {class: 'muted', text: ' · Round start not stored'}),
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
        el('details', {class: 'pop'}, [
          el('summary', {'aria-label': `Filter, ${filterCount} applied`}, [
            'Filter ',
            el('span', {class: 'n', text: String(filterCount)}),
          ]),
          sheet,
        ]),
      ]),
      el('nav', {class: 'rounds', 'aria-label': 'Rounds'}, (data.round_history || []).map((item) => {
        const current = item.round === data.round;
        return el('button', {
          type: 'button',
          'aria-current': current ? 'page' : null,
          text: `R${item.round} · ${item.fixed ?? 0} fixed`,
          onclick: () => {
            const roundInput = filters.round || document.getElementById('filter-round');
            if (roundInput) roundInput.value = item.round;
            load(project, data.pr?.number, item.round);
          },
        });
      }).concat(data.round != null && !(data.round_history || []).some((item) => item.round === data.round)
        ? [el('button', {type: 'button', 'aria-current': 'page', text: `R${data.round} · live`})]
        : [])),
    ]);

    const needs = el('section', {'aria-labelledby': 'needs-h'}, [
      el('h2', {id: 'needs-h'}, [
        'Needs you ',
        el('small', {text: data.needs_you?.length ? `${data.needs_you.length} ranked · evidence first` : 'Nothing waiting'}),
      ]),
      el('div', {class: 'needs'}, (data.needs_you || []).map((card) => el('article', {class: 'need'}, [
        el('p', {class: 'kicker', text: `${card.rank}. ${labelKind(card.kind)}`}, card.group_id ? [el('span', {class: 'mono', text: ` · ${card.group_id}`})] : []),
        el('h3', {text: card.title || ''}),
        el('p', {text: card.why || ''}),
        card.finding_ids?.length ? el('p', {class: 'muted mono', text: card.finding_ids.join(', ')}) : null,
      ]))),
      data.needs_you_more ? el('p', {class: 'muted', text: `${data.needs_you_more} more · FYI`}) : null,
    ]);

    const ringLabel = threshold == null
      ? `${gate.fixed ?? 0} verified · threshold not stored`
      : `${gate.fixed ?? 0} / ${threshold} to the merge gate`;
    const summary = el('section', {class: 'summary', 'aria-labelledby': 'sum-h'}, [
      el('h2', {id: 'sum-h', class: 'sr', text: 'Round summary'}),
      el('p', {}, [
        `${verified} verified fixed`,
        counts.in_progress ? ` · ${counts.in_progress} in progress` : '',
        counts.untested ? ` · ${counts.untested} untested` : '',
        counts.blocked ? ` · ${counts.blocked} blocked` : '',
        counts.deferred ? ` · ${counts.deferred} deferred` : '',
      ]),
      el('p', {class: 'muted', text: ringLabel + (gate.run_id ? ` · ${gate.run_id}` : '')}),
    ]);

    const currentGroups = (data.groups || []).filter((group) => {
      if (data.round == null) return true;
      const match = String(group.id || '').match(/^R(\d+)-/i);
      if (!match) return group.status === 'deferred' || group.id === 'DEFER';
      return Number(match[1]) === data.round;
    });
    const otherGroups = (data.groups || []).filter((group) => !currentGroups.includes(group));

    const jobsFor = (id) => (data.jobs || []).filter((job) => job.group_id === id);
    const lanes = el('section', {'aria-labelledby': 'lanes-h'}, [
      el('h2', {id: 'lanes-h', text: 'Lanes'}),
      el('div', {class: 'lanes'}, currentGroups.concat(otherGroups).map((group) => {
        const jobs = jobsFor(group.id);
        return el('details', {class: `lane ${statusClass(group.status)}`}, [
          el('summary', {}, [
            el('span', {class: 'id mono', text: group.id}),
            el('span', {class: 'name', text: shortName(group)}),
            el('span', {class: `tag ${statusClass(group.status)}`, text: `${group.status || '—'}${group.phase ? ` · ${group.phase}` : ''}`}),
            jobs.length ? el('span', {class: 'muted', text: `${jobs.length} job${jobs.length === 1 ? '' : 's'}`}) : el('span', {class: 'muted', text: 'no job'}),
          ]),
          el('div', {class: 'lane-body'}, [
            group.reason ? el('p', {text: group.reason}) : null,
            group.blocked_reason ? el('p', {text: `Blocked: ${group.blocked_reason}`}) : null,
            group.parked_to != null ? el('p', {class: 'muted', text: `Parked to round ${group.parked_to}`}) : null,
            group.findings?.length ? el('ul', {}, group.findings.map((finding) => el('li', {}, [
              el('strong', {class: 'mono', text: finding.id}),
              ` ${finding.title || ''} `,
              el('span', {class: 'muted', text: finding.status || ''}),
            ]))) : null,
            jobs.length ? el('table', {class: 'jobs'}, [
              el('thead', {}, [el('tr', {}, ['Phase', 'Agent', 'Started', 'Duration', 'Tokens', 'Exit'].map((h) => el('th', {text: h})))]),
              el('tbody', {}, jobs.map((job) => el('tr', {}, [
                el('td', {text: job.phase || ''}),
                el('td', {class: 'mono', text: job.agent || ''}),
                el('td', {}, job.started_at ? [el('time', {datetime: job.started_at, text: istTime(job.started_at)})] : ['—']),
                el('td', {text: durationLabel(job.duration_ms) || '—'}),
                el('td', {text: tokenCell(job)}),
                el('td', {text: job.exit_code == null ? (job.event === 'incomplete' ? 'open' : '—') : String(job.exit_code)}),
              ]))),
            ]) : el('p', {class: 'muted', text: 'No recorded job for this lane.'}),
          ]),
        ]);
      })),
    ]);

    const tt = data.time_tokens || {};
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
      tt.incomplete ? el('p', {class: 'muted', text: `${tt.incomplete} unfinished job(s). Unrecorded session time is not in this bar.`}) : el('p', {class: 'muted', text: 'Unrecorded session time is not in this bar.'}),
    ]);

    root.replaceChildren(header, needs, summary, lanes, bars);
    $('error').textContent = '';
    const health = $('health');
    if (health) {
      health.textContent = updated
        ? `Tracker updated ${istTime(updated)} (${relative(updated)}). Gate ${ringLabel}.`
        : 'Round view ready.';
    }
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

  function countActiveFilters() {
    let count = 0;
    for (const [id, node] of Object.entries(filters || {})) {
      if (!node) continue;
      const value = node.value;
      if (id === 'kind' || id === 'change_class') {
        if (value && value !== 'all') count += 1;
      } else if (value) count += 1;
    }
    const version = document.getElementById('version');
    if (version && version.value) count += 1;
    const status = document.getElementById('status');
    if (status && status.value && status.value !== 'all') count += 1;
    return count;
  }

  const projectSelect = $('project');
  const boot = async () => {
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
      root.replaceChildren(el('p', {class: 'muted', text: 'No project is available yet.'}));
    }
  };

  return {load, boot};
}
