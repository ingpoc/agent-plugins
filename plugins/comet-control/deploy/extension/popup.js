document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById(id);
  const dot = $('dot');
  const sub = $('sub');
  const err = $('err');

  function sv(el, state, text) { if (el) { el.textContent = text; el.className = 'val ' + state; } }
  function sd(s) { if (dot) dot.className = 'dot ' + (s === 'ok' ? 'ok' : s === 'bad' ? 'bad' : 'warn'); }
  function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  // Fault isolation: each popup concern initializes in its own guard so a
  // failure in one (e.g. a chrome.storage throw) is logged + surfaced but
  // NEVER aborts the others. Critical controls run first; optional features
  // last. This is the structural fix for "one unguarded await bricks the whole
  // popup" — ordering must not determine whether the Feedback Mode toggle works.
  function safeInit(label, fn) {
    Promise.resolve()
      .then(fn)
      .catch((e) => {
        console.error(`[comet-control-popup] ${label} init failed:`, e);
        if (err) { err.textContent = `${label}: ${e?.message || e}`; err.classList.add('show'); }
      });
  }

  // Resolve the active tab once (guarded); dependents handle null gracefully.
  const tabReady = chrome.tabs.query({ active: true, currentWindow: true })
    .then((tabs) => tabs[0] || null)
    .catch((e) => {
      if (err) { err.textContent = 'Tab query failed: ' + (e?.message || e); err.classList.add('show'); }
      return null;
    });

  // 1 — Extension-loaded indicator (critical, synchronous).
  safeInit('status', () => {
    $('ext-id').textContent = chrome.runtime.id || '—';
    $('sock').textContent = 'run/comet-control.sock';
    $('footer-version').textContent = `comet-control v${chrome.runtime.getManifest().version}`;
    sv($('ext-status'), 'ok', 'Loaded ✓');
    sd('warn');
    if (sub) sub.textContent = 'Checking broker…';
  });

  // Operator pause is the emergency boundary. Wire it before any optional UI.
  safeInit('operator-control', async () => {
    const toggle = $('pause-toggle');
    const paint = (status) => {
      const paused = status?.paused === true;
      toggle.classList.toggle('on', paused);
      toggle.setAttribute('aria-checked', String(paused));
      sv($('broker-status'), status?.connected ? (paused ? 'warn' : 'ok') : 'bad', status?.connected ? (paused ? 'Paused' : 'Connected') : 'Disconnected');
      $('protocol').textContent = status?.protocol_version ? `v${status.protocol_version}` : '—';
      const labels = (status?.sessions || []).map((session) => session.agent_label).filter(Boolean);
      $('sessions').textContent = labels.length ? labels.join(', ').slice(0, 80) : '0';
      const extensionBuild = String(status?.extension_build_sha256 || '').slice(0, 8);
      const brokerBuild = String(status?.broker_build_sha256 || '').slice(0, 8);
      $('build').textContent = extensionBuild && brokerBuild ? `ext ${extensionBuild} · broker ${brokerBuild}` : '—';
      sd(status?.connected ? (paused ? 'warn' : 'ok') : 'bad');
      if (sub) sub.textContent = status?.connected ? (paused ? 'Control paused by operator' : 'Connected — Comet Control is ready') : 'Broker disconnected';
    };
    const refresh = async () => {
      const status = await chrome.runtime.sendMessage({ type: 'comet-control-operator-status' });
      if (!status?.ok) throw new Error(status?.error || 'Status unavailable');
      paint(status);
      return status;
    };
    const handle = async () => {
      toggle.classList.add('busy');
      try {
        const current = toggle.getAttribute('aria-checked') === 'true';
        const status = await chrome.runtime.sendMessage({ type: 'comet-control-control-pause', paused: !current });
        if (!status?.ok) throw new Error(status?.error || 'Pause failed');
        paint(status);
      } finally {
        toggle.classList.remove('busy');
      }
    };
    toggle.addEventListener('click', () => { handle().catch((e) => { if (err) { err.textContent = e.message; err.classList.add('show'); } }); });
    toggle.addEventListener('keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); handle().catch(() => {}); } });
    await refresh();
  });

  // 2 — Active-tab card + content-script status (critical).
  safeInit('tab-status', async () => {
    const tab = await tabReady;
    if (!tab) {
      $('tab').innerHTML = '<div class="empty">No active tab detected</div>';
      sv($('cs-status'), 'warn', 'No tab');
      return;
    }
    const url = tab.url ? tab.url.replace(/^https?:\/\//, '').replace(/\/$/, '').substring(0, 48) : '';
    $('tab').innerHTML = `<div class="card"><div class="card-title">${esc(tab.title || 'Untitled')}</div><div class="card-url">${esc(url)}</div></div>`;
    if (!/^(https?|file):\/\//i.test(tab.url || '')) {
      sv($('cs-status'), 'warn', 'Blocked on this URL');
      return;
    }
    try {
      const swResponse = await chrome.runtime.sendMessage({ type: 'comet-control-cursor-status', tabId: tab.id });
      if (swResponse?.injected) sv($('cs-status'), 'ok', 'Injected ✓');
      else sv($('cs-status'), swResponse?.blocked ? 'bad' : 'warn', swResponse?.reason || 'Not injected');
    } catch (e) {
      sv($('cs-status'), 'bad', 'Check failed');
      if (err) { err.textContent = 'Content script check failed: ' + (e?.message || e); err.classList.add('show'); }
    }
  });

  // 3 — Feedback Mode toggle (CRITICAL control). Event listeners attach BEFORE
  // any throwable await, so the toggle is always operable even if the stored
  // state read fails (it would just start in the default visual state).
  safeInit('feedback-toggle', async () => {
    const tab = await tabReady;
    const toggle = $('feedback-toggle');
    if (!tab || !toggle) return;
    const errRow = $('feedback-err-row');
    const errOut = $('feedback-err');
    const storageKey = `feedback-mode:${tab.id}`;
    const showErr = (msg) => { if (errOut) errOut.textContent = msg || ''; if (errRow) errRow.style.display = msg ? 'flex' : 'none'; };
    const paint = (on) => { toggle.classList.toggle('on', !!on); toggle.setAttribute('aria-checked', on ? 'true' : 'false'); };
    const handle = async () => {
      if (toggle.classList.contains('busy')) return;
      const next = !toggle.classList.contains('on');
      toggle.classList.add('busy');
      showErr('');
      try {
        const result = await chrome.runtime.sendMessage({ type: 'comet-control-feedback-toggle', tabId: tab.id, enabled: next });
        if (result?.ok) { paint(next); await chrome.storage.local.set({ [storageKey]: next }); }
        else showErr(result?.error || 'Toggle failed');
      } catch (e) {
        showErr(e?.message || String(e));
      } finally {
        toggle.classList.remove('busy');
      }
    };
    // Wire interaction FIRST — never gate it behind the state read below.
    toggle.addEventListener('click', handle);
    toggle.addEventListener('keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); handle(); } });
    // Initial visual state — non-critical; a failure here must not unwire the toggle.
    try { const { [storageKey]: stored } = await chrome.storage.local.get(storageKey); paint(!!stored); } catch { /* leave default */ }
  });

  // 4 — Queue origin (OPTIONAL, cross-origin mode). Runs last; isolated so a
  // storage failure can never affect the critical controls above.
  // Blank = same-origin; set e.g. http://localhost:7878 when the queue server
  // lives on a different origin than the annotated app.
  safeInit('queue-origin', async () => {
    const originInput = $('queue-origin');
    if (!originInput) return;
    const hintRow = $('queue-origin-hint-row');
    const hintOut = $('queue-origin-hint');
    const showHint = (msg, bad) => { if (hintOut) { hintOut.textContent = msg || ''; hintOut.style.color = bad ? '#f87171' : '#52525b'; } if (hintRow) hintRow.style.display = msg ? 'flex' : 'none'; };
    const save = async () => {
      const raw = originInput.value.trim().replace(/\/$/, '');
      if (!raw) { await chrome.storage.local.remove('feedbackQueueOrigin'); showHint('Same-origin — posts to the annotated page’s own server.'); return; }
      let ok = false;
      try { const u = new URL(raw); ok = /^https?:$/.test(u.protocol) && u.origin === raw; } catch { ok = false; }
      if (!ok) { showHint('Must be a bare origin, e.g. http://localhost:7878', true); return; }
      await chrome.storage.local.set({ feedbackQueueOrigin: raw });
      showHint('Saved — re-toggle Feedback Mode to apply.');
    };
    originInput.addEventListener('change', () => { save().catch((e) => showHint(String(e?.message || e), true)); });
    originInput.addEventListener('blur', () => { save().catch((e) => showHint(String(e?.message || e), true)); });
    const { feedbackQueueOrigin } = await chrome.storage.local.get('feedbackQueueOrigin');
    if (feedbackQueueOrigin) originInput.value = feedbackQueueOrigin;
  });
});
