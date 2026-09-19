/**
 * Comet Control Bridge — Floating Cursor Agent Overlay
 * 
 * Renders a visible floating cursor on every page that shows where
 * the AI agent is interacting. The cursor:
 * - Is a non-interfering overlay (pointerEvents: none on base layer)
 * - Animates smoothly between positions
 * - Passes real clicks through to the underlying page at the cursor position
 */
(() => {
  // Prevent double-injection, but allow re-injection if the extension context
  // was invalidated (reload) or the prior instance is half-dead (getStatus
  // stopped answering — Seller Accept→Dispatch).
  if (document.documentElement.dataset.cometControlAgentCursorInjected) {
    try {
      void chrome.runtime.id; // throws "Extension context invalidated" after a reload
      if (window.__cometControlAgentCursorAlive) {
        return;
      }
      // Half-dead: alive flag cleared but DOM/dataset linger. Tear down leftovers
      // before rebinding so executeScript reinject actually installs a listener.
      document.getElementById('comet-control-agent-cursor-overlay')?.remove();
      document.querySelector('style[data-comet-control-cursor-style]')?.remove();
      document.documentElement.removeAttribute('data-comet-control-agent-cursor-injected');
    } catch {
      document.documentElement.removeAttribute('data-comet-control-agent-cursor-injected');
    }
  }
  document.documentElement.dataset.cometControlAgentCursorInjected = "true";
  window.__cometControlAgentCursorInjected = true;
  window.__cometControlAgentCursorAlive = true;

  // ---- State ----
  let cursorX = -100;
  let cursorY = -100;
  let cursorEl = null;
  let pointerEl = null;
  let labelEl = null;
  let animationFrame = null;
  let isVisible = false;
  let cursorPhase = 'idle'; // idle | moving | clicking | arrived
  let agentIdentity = { agentId: '', label: '', sessionId: '', color: '#64d8ff' };
  let elementTargetTokens = new WeakMap();
  const targetElementsByToken = new Map();
  const semanticTargetCache = new Map();
  let nextTargetToken = 1;
  let pageRevision = 1;
  let clickRingTimers = [];

  const COMET_CONTROL_CURSOR_ID = 'comet-control-agent-cursor-overlay';
  const POINTER_SVG = chrome.runtime.getURL('images/pointer-shape-animated.svg') + '?v=codex-like-2';

  function _isCursorMutation(record) {
    const target = record?.target instanceof Element ? record.target : record?.target?.parentElement;
    if (!target) return false;
    if (target.closest?.(`#${COMET_CONTROL_CURSOR_ID}`)) return true;
    if (target.closest?.('.comet-control-click-ring') || target.matches?.('.comet-control-click-ring')) return true;
    if (target.matches?.('style[data-comet-control-click-ring-style]')) return true;
    return false;
  }

  function _bumpPageRevision(records) {
    if (records?.length && records.every(_isCursorMutation)) return;
    pageRevision += 1;
    for (const [token, target] of targetElementsByToken) {
      if (!target.isConnected) targetElementsByToken.delete(token);
    }
    semanticTargetCache.clear();
  }

  // Chrome-parity: no page-wide observer while idle. Start only when locator
  // stability / pageRevision is needed; parkIdle() tears it down after actions
  // unless visual cursor mode keeps the session warm.
  let pageObserver = null;
  function ensurePageObserver() {
    if (pageObserver) return;
    pageObserver = new MutationObserver(_bumpPageRevision);
    pageObserver.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['aria-disabled', 'class', 'disabled', 'hidden', 'readonly', 'style'],
    });
  }
  function stopPageObserver() {
    if (!pageObserver) return;
    pageObserver.disconnect();
    pageObserver = null;
  }

  // ---- Styles ----
  const css = `
    #${COMET_CONTROL_CURSOR_ID} {
      position: fixed;
      z-index: 2147483647;
      pointer-events: none;
      left: 0;
      top: 0;
      will-change: transform;
      transform: translate(-100px, -100px);
      /* Compositor-driven glide: animates even when Comet is NOT the foreground
         window, unlike requestAnimationFrame (throttled to ~1fps in background). */
      transition: transform 0.32s cubic-bezier(0.22, 0.61, 0.36, 1), opacity 0.2s ease;
      opacity: 0;
    }
    #${COMET_CONTROL_CURSOR_ID}.comet-control-visible {
      opacity: 1;
    }
    #${COMET_CONTROL_CURSOR_ID} .comet-control-cursor-pointer {
      width: 18px;
      height: 18px;
      display: block;
      transform: translate(-1px, -1px);
      transform-origin: 1px 1px;
      filter:
        drop-shadow(0 0 8px var(--comet-control-cursor-color, rgba(73, 182, 255, 0.9)))
        drop-shadow(0 2px 3px rgba(0,0,0,0.45));
      transition: transform 0.12s ease;
      animation: comet-control-pointer-idle 1.7s ease-in-out infinite;
      user-select: none;
      -webkit-user-drag: none;
    }
    #${COMET_CONTROL_CURSOR_ID} .comet-control-cursor-agent-label {
      position: absolute;
      left: 16px;
      top: 20px;
      display: none;
      max-width: 320px;
      padding: 4px 8px;
      border: 1px solid color-mix(in srgb, var(--comet-control-cursor-color, #64d8ff) 72%, white);
      border-radius: 999px;
      background: rgba(10, 18, 28, 0.94);
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.38);
      color: #fff;
      font: 700 12px/1.25 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0.01em;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      user-select: none;
    }
    #${COMET_CONTROL_CURSOR_ID}.comet-control-moving .comet-control-cursor-pointer {
      animation: comet-control-pointer-moving 0.48s ease-in-out infinite;
    }
    #${COMET_CONTROL_CURSOR_ID} .comet-control-cursor-pointer.comet-control-clicking {
      transform: translate(-1px, -1px) scale(0.86);
      animation: none;
    }
    @keyframes comet-control-pointer-idle {
      0%, 100% { transform: translate(-1px, -1px) rotate(0deg); }
      50% { transform: translate(0, -2px) rotate(0.35deg); }
    }
    @keyframes comet-control-pointer-moving {
      0%, 100% { transform: translate(-1px, -1px) rotate(-2deg); }
      50% { transform: translate(1px, -3px) rotate(2deg); }
    }
    /* On-demand click ring — never present at load; only via pulseClick/showClickRing */
    .comet-control-click-ring {
      position: fixed;
      z-index: 2147483646;
      pointer-events: none;
      border-radius: 999px;
      border: 2px solid var(--comet-control-cursor-color, #64d8ff);
      box-shadow:
        0 0 0 2px rgba(100, 216, 255, 0.25),
        0 0 14px rgba(100, 216, 255, 0.55);
      transform: translate(-50%, -50%) scale(0.55);
      opacity: 0.95;
      animation: comet-control-click-ring-pulse 280ms ease-out forwards;
      will-change: transform, opacity;
    }
    @keyframes comet-control-click-ring-pulse {
      0% { transform: translate(-50%, -50%) scale(0.45); opacity: 0.95; }
      70% { transform: translate(-50%, -50%) scale(1.15); opacity: 0.55; }
      100% { transform: translate(-50%, -50%) scale(1.35); opacity: 0; }
    }
  `;

  // ---- DOM Setup ----
  function createOverlay(options = {}) {
    // Preserve last-known position + visibility across script re-injection.
    // Without this, a service-worker idle restart or extension reload silently
    // resets the cursor to (-100, -100) opacity:0 — the operator sees the
    // cursor vanish mid-flow until the agent's next cursor_move. Read the
    // prior element's transform + visibility, then restore after recreating.
    // Session-warm / watchable leases: default priorVisible so recreating the
    // overlay does not debut opacity-0 offscreen waiting for the next moveTo.
    const old = document.getElementById(COMET_CONTROL_CURSOR_ID);
    let priorTransform = null;
    let priorVisible = Boolean(options.sessionWarm || options.forceVisible
      || window.__cometControlSessionWarm === true);
    let priorIdentity = null;
    if (old) {
      priorTransform = old.style.transform || null;
      priorVisible = priorVisible || old.classList.contains('comet-control-visible');
      priorIdentity = {
        agentId: old.dataset.agentId || '',
        label: old.dataset.agentLabel || '',
        sessionId: old.dataset.sessionId || '',
        color: old.style.getPropertyValue('--comet-control-cursor-color') || '#64d8ff'
      };
      old.remove();
    }

    // Replace the style block too (CSS may have changed across reload).
    const oldStyle = document.querySelector('style[data-comet-control-cursor-style]');
    if (oldStyle) oldStyle.remove();
    const style = document.createElement('style');
    style.dataset.cometControlCursorStyle = '1';
    style.textContent = css;
    document.head.appendChild(style);

    cursorEl = document.createElement('div');
    cursorEl.id = COMET_CONTROL_CURSOR_ID;

    const pointerImg = document.createElement('img');
    pointerImg.className = 'comet-control-cursor-pointer';
    pointerImg.src = POINTER_SVG;
    pointerImg.alt = '';
    pointerImg.decoding = 'async';
    pointerEl = pointerImg;

    labelEl = document.createElement('div');
    labelEl.className = 'comet-control-cursor-agent-label';
    labelEl.setAttribute('role', 'status');
    labelEl.setAttribute('aria-live', 'polite');

    cursorEl.appendChild(pointerImg);
    cursorEl.appendChild(labelEl);
    document.documentElement.appendChild(cursorEl);

    if (priorIdentity?.label) setIdentity(priorIdentity);

    // Restore prior position + visibility so the cursor stays parked where it
    // was at the moment of re-injection (operator-facing continuity).
    if (priorTransform && priorTransform !== 'none' && priorTransform.indexOf('-100') === -1) {
      cursorEl.style.transform = priorTransform;
      const m = priorTransform.match(/-?\d+(\.\d+)?/g);
      if (m && m.length >= 2) {
        const useMatrix = priorTransform.indexOf('matrix') === 0;
        cursorX = parseFloat(useMatrix ? m[4] : m[0]);
        cursorY = parseFloat(useMatrix ? m[5] : m[1]);
      }
    }
    if (priorVisible) {
      // Warm path: never leave logical coords at the offscreen park point.
      if (!(Number.isFinite(cursorX) && Number.isFinite(cursorY))
          || cursorX < 0 || cursorY < 0
          || (cursorEl.style.transform || '').indexOf('-100') !== -1) {
        cursorX = Math.round((window.innerWidth || 800) / 2);
        cursorY = Math.round((window.innerHeight || 600) / 2);
        cursorEl.style.transform = `translate(${cursorX}px, ${cursorY}px)`;
      }
      cursorEl.classList.add('comet-control-visible');
      isVisible = true;
    } else {
      isVisible = false;
    }
  }

  // ---- Motion ----
  // RELIABILITY CONTRACT: the *logical* position (cursorX/cursorY) snaps to the
  // target immediately, so click() / elementFromPoint always resolve the element
  // the operator asked for — even if the visible glide hasn't finished or the
  // tab is backgrounded. The *visible* glide is a CSS transform transition
  // (compositor-driven), so the operator still sees the cursor travel to the
  // target. What is seen and what is clicked therefore always agree.
  const GLIDE_MS = 320;
  let arrivalTimer = null;
  let arrivalDeadline = 0;

  function moveTo(x, y) {
    if (!cursorEl) createOverlay();
    if (isVisible && cursorX === x && cursorY === y) return;
    arrivalDeadline = performance.now() + GLIDE_MS + 40;
    cursorX = x;
    cursorY = y;
    if (!isVisible) {
      cursorEl.classList.add('comet-control-visible');
      isVisible = true;
    }
    cursorPhase = 'moving';
    cursorEl.classList.add('comet-control-moving');
    cursorEl.style.transform = `translate(${x}px, ${y}px)`; // CSS transition glides
    if (arrivalTimer) clearTimeout(arrivalTimer);
    arrivalTimer = setTimeout(() => {
      cursorPhase = 'idle';
      cursorEl?.classList.remove('comet-control-moving');
    }, GLIDE_MS);
  }

  function moveToAndWait(x, y, timeoutMs = 900) {
    moveTo(x, y);
    // Logical position is already at the target; wait only for the visible glide
    // so the operator sees the cursor arrive before the click. setTimeout (not
    // rAF) resolves promptly even in a backgrounded tab.
    const wait = Math.min(Math.max(0, arrivalDeadline - performance.now()), Math.max(0, timeoutMs));
    return new Promise((resolve) => setTimeout(() => resolve(getStatus()), wait));
  }

  function clearClickRings() {
    for (const t of clickRingTimers) clearTimeout(t);
    clickRingTimers = [];
    document.querySelectorAll('.comet-control-click-ring').forEach((el) => el.remove());
    document.querySelector('style[data-comet-control-click-ring-style]')?.remove();
  }

  function ensureClickRingStyle() {
    if (document.querySelector('style[data-comet-control-click-ring-style]')) return;
    // Standalone style so ring can outlive cursor overlay park (trusted CDP path).
    const style = document.createElement('style');
    style.dataset.cometControlClickRingStyle = '1';
    style.textContent = `
      .comet-control-click-ring {
        position: fixed;
        z-index: 2147483646;
        pointer-events: none;
        border-radius: 999px;
        border: 2px solid var(--comet-control-cursor-color, #64d8ff);
        box-shadow:
          0 0 0 2px rgba(100, 216, 255, 0.25),
          0 0 14px rgba(100, 216, 255, 0.55);
        transform: translate(-50%, -50%) scale(0.55);
        opacity: 0.95;
        animation: comet-control-click-ring-pulse 280ms ease-out forwards;
        will-change: transform, opacity;
      }
      @keyframes comet-control-click-ring-pulse {
        0% { transform: translate(-50%, -50%) scale(0.45); opacity: 0.95; }
        70% { transform: translate(-50%, -50%) scale(1.15); opacity: 0.55; }
        100% { transform: translate(-50%, -50%) scale(1.35); opacity: 0; }
      }
    `;
    document.documentElement.appendChild(style);
  }

  // On-demand only: brief ring at click point (~280ms). Never at page load.
  function showClickRing(x = cursorX, y = cursorY) {
    const cx = Number(x);
    const cy = Number(y);
    if (!Number.isFinite(cx) || !Number.isFinite(cy)) return { click_ring: false };
    ensureClickRingStyle();
    const ring = document.createElement('div');
    ring.className = 'comet-control-click-ring';
    ring.setAttribute('data-comet-control-click-ring', '1');
    ring.style.left = `${cx}px`;
    ring.style.top = `${cy}px`;
    ring.style.width = '28px';
    ring.style.height = '28px';
    if (agentIdentity?.color) {
      ring.style.setProperty('--comet-control-cursor-color', agentIdentity.color);
    }
    document.documentElement.appendChild(ring);
    const timer = setTimeout(() => {
      ring.remove();
      clickRingTimers = clickRingTimers.filter((t) => t !== timer);
      if (!document.querySelector('.comet-control-click-ring')) {
        document.querySelector('style[data-comet-control-click-ring-style]')?.remove();
      }
    }, 300);
    clickRingTimers.push(timer);
    return { click_ring: true, x: cx, y: cy };
  }

  function pulseClick() {
    cursorPhase = 'clicking';
    pointerEl?.classList.add('comet-control-clicking');
    const ring = showClickRing(cursorX, cursorY);
    setTimeout(() => {
      cursorPhase = 'idle';
      pointerEl?.classList.remove('comet-control-clicking');
    }, 140);
    return { pulsed: true, click_ring: Boolean(ring?.click_ring), x: cursorX, y: cursorY };
  }

  function _clickTextValue(el) {
    return (el?.innerText || el?.value || el?.getAttribute?.('aria-label') || '').trim();
  }

  function _targetTokenForElement(el) {
    let token = elementTargetTokens.get(el);
    if (!token) {
      token = `comet-control-target-${pageRevision}-${nextTargetToken++}`;
      elementTargetTokens.set(el, token);
    }
    targetElementsByToken.set(token, el);
    return token;
  }

  function _relatedTargetAtCursor(elements) {
    const hit = document.elementFromPoint(cursorX, cursorY);
    if (!hit) return null;
    // elementFromPoint returns the deepest painted node at the cursor. The
    // expected semantic target may therefore contain that node, but accepting
    // the inverse relationship would allow a broad ancestor hit outside the
    // expected element's actual bounds. Shadow-hosted 16px icons also need
    // the clickable ancestor / composed-tree walk in _hitIsOnTarget.
    const related = elements.filter((el) => _hitIsOnTarget(el, hit));
    if (!related.length) return null;
    const interactive = (el) => /^(A|BUTTON|SUMMARY|LABEL|INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.getAttribute('role') === 'button';
    related.sort((a, b) => {
      const rank = Number(!interactive(a)) - Number(!interactive(b));
      if (rank !== 0) return rank;
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return (ar.width * ar.height) - (br.width * br.height);
    });
    return related[0];
  }

  function _expectedTargetAtCursor(expectation = {}) {
    // Unrelated DOM churn must not invalidate an otherwise identical target.
    // The token/selector/text checks below still require the same connected,
    // visible, enabled element to remain under the cursor at click time.
    if (expectation.targetToken) {
      const token = String(expectation.targetToken);
      const target = targetElementsByToken.get(token);
      const selector = expectation.selector ? String(expectation.selector) : '';
      const needle = expectation.text ? String(expectation.text).toLowerCase().trim() : '';
      const stillMatches = Boolean(
        target
        && target.isConnected
        && _isVisible(target)
        && _isEnabled(target)
        && (!expectation.editable || _isEditable(target))
        && (!selector || target.matches(selector))
        && (!needle || _clickTextValue(target).toLowerCase().includes(needle))
      );
      if (!stillMatches || !_relatedTargetAtCursor([target])) {
        // React-style layout churn can move or replace an anchor during the
        // visible glide. Only links with the same expected destination may
        // fall through to semantic resolution at the current cursor position.
        if (!expectation.targetHref) {
          throw new Error(`CLICK_TARGET_MISMATCH token=${token}`);
        }
      } else {
        return {
          target,
          verifiedBy: selector ? 'selector' : (needle ? 'text' : 'identity')
        };
      }
    }

    if (expectation.selector) {
      const selector = String(expectation.selector);
      const target = _relatedTargetAtCursor(
        _querySelectorAllDeep(selector).filter(_isVisible).filter(
          (el) => !expectation.targetHref || el.href === expectation.targetHref
        )
      );
      if (!target) {
        throw new Error(`CLICK_TARGET_MISMATCH selector=${selector}`);
      }
      return { target, verifiedBy: 'selector' };
    }

    if (expectation.text) {
      const needle = String(expectation.text).toLowerCase().trim();
      const target = _relatedTargetAtCursor(
        _querySelectorAllDeep(
          'a,button,[role=button],input[type=button],input[type=submit],label,summary'
        ).filter(_isVisible)
          .filter((el) => _clickTextValue(el).toLowerCase().includes(needle))
          .filter((el) => !expectation.targetHref || el.href === expectation.targetHref)
      );
      if (!target) {
        throw new Error(`CLICK_TARGET_MISMATCH text=${String(expectation.text).slice(0, 80)}`);
      }
      return { target, verifiedBy: 'text' };
    }

    return { target: document.elementFromPoint(cursorX, cursorY), verifiedBy: 'point' };
  }

  function click(expectation = {}) {
    if (!cursorEl) return;
    pointerEl?.classList.add('comet-control-clicking');
    setTimeout(() => pointerEl?.classList.remove('comet-control-clicking'), 140);
    const ring = showClickRing(cursorX, cursorY);

    // Resolve the expected semantic target atomically at click time. This blocks
    // stale coordinates from clicking a different element after layout movement.
    const { target: el, verifiedBy } = _expectedTargetAtCursor(expectation);

    if (el) {
      const control = _checkboxControl(el);
      const before = control ? control.checked : null;
      const scrollX = window.scrollX || 0;
      const scrollY = window.scrollY || 0;
      const opts = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new MouseEvent('mousedown', { ...opts, clientX: cursorX, clientY: cursorY, pageX: cursorX + scrollX, pageY: cursorY + scrollY }));
      el.dispatchEvent(new MouseEvent('mouseup', { ...opts, clientX: cursorX, clientY: cursorY, pageX: cursorX + scrollX, pageY: cursorY + scrollY }));
      el.dispatchEvent(new MouseEvent('click', { ...opts, clientX: cursorX, clientY: cursorY, pageX: cursorX + scrollX, pageY: cursorY + scrollY }));
      if (control && control.checked === before) {
        control.click();
      }
    }
    flashLabel('click');
    return {
      clicked: Boolean(el),
      verified_by: verifiedBy,
      tag: el?.tagName || '',
      text: _clickTextValue(el).slice(0, 160),
      href: el?.href || '',
      click_ring: Boolean(ring?.click_ring)
    };
  }

  function tripleClick() {
    if (!cursorEl) return;
    // Cursor host is pointer-events:none — elementFromPoint already skips it.
    const el = document.elementFromPoint(cursorX, cursorY);
    if (el) {
      const opts = { bubbles: true, cancelable: true, view: window };
      [1, 2, 3].forEach(() => {
        el.dispatchEvent(new MouseEvent('mousedown', { ...opts, clientX: cursorX, clientY: cursorY }));
        el.dispatchEvent(new MouseEvent('mouseup', { ...opts, clientX: cursorX, clientY: cursorY }));
        el.dispatchEvent(new MouseEvent('click', { ...opts, clientX: cursorX, clientY: cursorY }));
      });
    }
    flashLabel('triple-click');
  }

  function rightClick() {
    if (!cursorEl) return;
    // Cursor host is pointer-events:none — elementFromPoint already skips it.
    const el = document.elementFromPoint(cursorX, cursorY);
    if (el) {
      el.dispatchEvent(new MouseEvent('contextmenu', {
        bubbles: true, cancelable: true, view: window,
        clientX: cursorX, clientY: cursorY
      }));
    }
    flashLabel('right-click');
  }

  function dblClick() {
    if (!cursorEl) return;
    // Cursor host is pointer-events:none — elementFromPoint already skips it.
    const el = document.elementFromPoint(cursorX, cursorY);
    if (el) {
      const opts = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new MouseEvent('mousedown', { ...opts, clientX: cursorX, clientY: cursorY }));
      el.dispatchEvent(new MouseEvent('mouseup', { ...opts, clientX: cursorX, clientY: cursorY }));
      el.dispatchEvent(new MouseEvent('dblclick', { ...opts, clientX: cursorX, clientY: cursorY }));
    }
    flashLabel('double-click');
  }

  function _setEditableValue(el, text, append) {
    const next = append && el.value !== undefined
      ? `${String(el.value || '')}${String(text ?? '')}`
      : String(text ?? '');
    if (el instanceof HTMLInputElement) {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(el, next);
    } else if (el instanceof HTMLTextAreaElement) {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set?.call(el, next);
    } else if (el instanceof HTMLSelectElement) {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set?.call(el, next);
    } else if (el.isContentEditable) {
      el.textContent = append ? `${el.textContent || ''}${next}` : next;
    } else {
      throw _actionabilityError('ACTIONABILITY_NOT_EDITABLE', 'Fill target is not editable', {
        kind: 'selector',
      });
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return 'value' in el ? el.value : el.textContent;
  }

  function fillBySelector(selector, text, opts = {}) {
    const value = String(selector || '');
    if (!value) {
      throw _actionabilityError('ACTIONABILITY_EMPTY_LOCATOR', 'Selector is empty', { kind: 'selector' });
    }
    const matches = _querySelectorAllDeep(value).filter(_isVisible);
    let el = matches.find(_isEditable) || matches[0];
    if (el && el.tagName === 'LABEL' && el.control) el = el.control;
    if (!el) {
      throw _actionabilityError(
        'ELEMENT_NOT_FOUND',
        `No visible element matched selector: ${value}`,
        { kind: 'selector', locator: value },
      );
    }
    if (!_isEditable(el)) {
      throw _actionabilityError('ACTIONABILITY_NOT_EDITABLE', 'Fill target is not editable', {
        kind: 'selector',
        locator: value,
      });
    }
    el.focus();
    const written = _setEditableValue(el, text, Boolean(opts.append));
    flashLabel('type');
    return written;
  }

  function focusAndType(text, opts = {}, expectation = {}) {
    if (!cursorEl) {
      throw _actionabilityError('TYPE_CURSOR_NOT_READY', 'Cursor overlay is not ready for type');
    }
    const { target: el } = _expectedTargetAtCursor(expectation);
    if (!el) {
      throw _actionabilityError('ELEMENT_NOT_FOUND', 'No editable target under cursor');
    }
    el.focus();
    const written = _setEditableValue(el, text, Boolean(opts.append));
    flashLabel('type');
    return written;
  }

  function keyPress(key, modifiers = []) {
    if (!cursorEl) return;
    // Cursor host is pointer-events:none — elementFromPoint already skips it.
    const el = document.elementFromPoint(cursorX, cursorY) || document.activeElement || document.body;
    const opts = {
      bubbles: true, cancelable: true, view: window,
      key, code: key,
      ctrlKey: modifiers.includes('control') || modifiers.includes('ctrl'),
      shiftKey: modifiers.includes('shift'),
      altKey: modifiers.includes('alt') || modifiers.includes('option'),
      metaKey: modifiers.includes('command') || modifiers.includes('cmd'),
    };
    el.dispatchEvent(new KeyboardEvent('keydown', opts));
    el.dispatchEvent(new KeyboardEvent('keypress', opts));
    el.dispatchEvent(new KeyboardEvent('keyup', opts));
    flashLabel(`⌨ ${key}`);
  }

  function showKey(key) {
    flashLabel(`⌨ ${key}`);
  }

  function dragTo(endX, endY, duration = 500) {
    return new Promise((resolve) => {
      const startX = cursorX;
      const startY = cursorY;
      const startTime = performance.now();
      // Cursor host is pointer-events:none — elementFromPoint already skips it.
      const el = document.elementFromPoint(cursorX, cursorY);
      if (el) {
        el.dispatchEvent(new MouseEvent('mousedown', {
          bubbles: true, cancelable: true, view: window,
          clientX: cursorX, clientY: cursorY
        }));
      }
      function step(now) {
        const t = Math.min(1, (now - startTime) / duration);
        const x = startX + (endX - startX) * t;
        const y = startY + (endY - startY) * t;
        moveTo(x, y);
        if (el) {
          el.dispatchEvent(new MouseEvent('mousemove', {
            bubbles: true, cancelable: true, view: window,
            clientX: x, clientY: y
          }));
        }
        if (t < 1) {
          requestAnimationFrame(step);
        } else {
          if (el) {
            el.dispatchEvent(new MouseEvent('mouseup', {
              bubbles: true, cancelable: true, view: window,
              clientX: endX, clientY: endY
            }));
          }
          flashLabel('drag');
          resolve();
        }
      }
      requestAnimationFrame(step);
    });
  }

  function _scrollableAncestor(start) {
    let node = start;
    while (node && node !== document.documentElement) {
      if (node instanceof Element) {
        try {
          const s = window.getComputedStyle(node);
          const canY = (s.overflowY === 'auto' || s.overflowY === 'scroll' || s.overflowY === 'overlay')
            && node.scrollHeight > node.clientHeight + 1;
          const canX = (s.overflowX === 'auto' || s.overflowX === 'scroll' || s.overflowX === 'overlay')
            && node.scrollWidth > node.clientWidth + 1;
          if (canY || canX) return node;
        } catch { /* keep walking */ }
      }
      const parent = node.parentElement;
      if (parent) {
        node = parent;
        continue;
      }
      const root = node.getRootNode?.();
      node = root && root !== node && root.host ? root.host : null;
    }
    return document.scrollingElement || document.documentElement;
  }

  function scroll(deltaX, deltaY) {
    const hit = document.elementFromPoint(cursorX, cursorY) || document.documentElement;
    const el = _scrollableAncestor(hit);
    try { el.scrollBy(deltaX, deltaY); } catch { /* non-element host */ }
    const we = new WheelEvent('wheel', {
      bubbles: true, cancelable: true, view: window,
      clientX: cursorX, clientY: cursorY,
      deltaX, deltaY
    });
    hit.dispatchEvent(we);
    flashLabel('scroll');
  }

  function getVisibleText(maxChars) {
    return {
      url: location.href,
      title: document.title,
      text: (document.body?.innerText || '').trim().slice(0, maxChars)
    };
  }

  function _isVisible(el) {
    try {
      const s = window.getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    } catch { return true; }
  }

  function _actionabilityError(code, message, details = {}) {
    const error = new Error(message);
    error.code = code;
    error.details = { ...details, page_revision: pageRevision };
    return error;
  }

  function _isEnabled(el) {
    return !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  }

  function _isEditable(el) {
    return _isEnabled(el) && !el.readOnly && (
      el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)
    );
  }

  function _sameRect(left, right) {
    return ['top', 'left', 'width', 'height'].every((key) => Math.abs(left[key] - right[key]) <= 0.5);
  }

  function _nextFrame() {
    return new Promise((resolve) => requestAnimationFrame(resolve));
  }

  function _semanticCacheKey(kind, locator, mode) {
    return [
      chrome.runtime.getManifest().version,
      location.origin,
      location.pathname,
      document.title.slice(0, 80),
      pageRevision,
      mode,
      kind,
      String(locator || '').slice(0, 200),
    ].join('|');
  }


  function _checkboxControl(el) {
    if (!el) return null;
    const isBox = (node) =>
      node instanceof HTMLInputElement && (node.type === 'checkbox' || node.type === 'radio');
    if (isBox(el)) return el;
    const label = el.closest?.('label');
    if (label) {
      if (isBox(label.control)) return label.control;
      const inner = label.querySelector?.('input[type="checkbox"], input[type="radio"]');
      if (isBox(inner)) return inner;
    }
    const wrapped = el.querySelector?.('input[type="checkbox"], input[type="radio"]');
    if (isBox(wrapped)) return wrapped;
    return null;
  }

  function _isDocumentSized(rect) {
    return rect.width >= window.innerWidth * 0.9 || rect.height >= window.innerHeight * 0.8;
  }

  function _stickyCardRect(el, nameRect) {
    // Unique sticky names (FPL pitch): keep the chosen node, rewrite Y to the
    // nearest same-column non-sticky card. Do not drop unique sticky matches.
    if (!_isStickyOrFixedDescendant(el)) return nameRect;
    const elX = nameRect.left + nameRect.width / 2;
    let hops = 0;
    for (const node of _hostChain(el)) {
      if (!(node instanceof Element) || node === el) continue;
      hops += 1;
      if (hops > 12) break;
      const tag = node.tagName;
      const role = (node.getAttribute('role') || '').toLowerCase();
      if (tag === 'HTML' || tag === 'BODY' || tag === 'MAIN' || role === 'main') continue;
      let pos = '';
      try { pos = window.getComputedStyle(node).position; } catch { continue; }
      if (pos === 'sticky' || pos === 'fixed') continue;
      const nr = node.getBoundingClientRect();
      if (_isDocumentSized(nr)) continue;
      if (nr.height <= nameRect.height * 1.5) continue;
      const nx = nr.left + nr.width / 2;
      if (Math.abs(nx - elX) > Math.max(80, nameRect.width)) continue;
      return nr;
    }
    return nameRect;
  }

  function _hitIsOnStickyCard(el, top) {
    if (!el || !top) return false;
    for (const node of _hostChain(el)) {
      if (!(node instanceof Element) || node === el) continue;
      const tag = node.tagName;
      const role = (node.getAttribute('role') || '').toLowerCase();
      if (tag === 'HTML' || tag === 'BODY' || tag === 'MAIN' || role === 'main') continue;
      let pos = '';
      try { pos = window.getComputedStyle(node).position; } catch { continue; }
      if (pos === 'sticky' || pos === 'fixed') continue;
      const nr = node.getBoundingClientRect();
      if (_isDocumentSized(nr)) continue;
      if (node.contains(top) || _composedRelated(node, top)) return true;
    }
    return false;
  }

  async function _pointForElement(el, { mode, locator, kind }) {
    if (!_isVisible(el)) {
      throw _actionabilityError('ACTIONABILITY_NOT_VISIBLE', 'Target is not visible', { kind, locator });
    }
    if (!_isEnabled(el)) {
      throw _actionabilityError('ACTIONABILITY_DISABLED', 'Target is disabled', { kind, locator });
    }
    if (mode === 'fill' && !_isEditable(el)) {
      throw _actionabilityError('ACTIONABILITY_NOT_EDITABLE', 'Fill target is not editable', { kind, locator });
    }
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const first = el.getBoundingClientRect();
    await _nextFrame();
    const second = el.getBoundingClientRect();
    await _nextFrame();
    const r = el.getBoundingClientRect();
    if (!_sameRect(first, second) || !_sameRect(second, r)) {
      throw _actionabilityError('ACTIONABILITY_UNSTABLE', 'Target moved across animation frames', { kind, locator });
    }
    const clickRect = _stickyCardRect(el, r);
    const nameX = r.left + r.width / 2;
    const cardX = clickRect.left + clickRect.width / 2;
    const x = Math.round(Math.abs(cardX - nameX) <= Math.max(24, r.width) ? nameX : cardX);
    const y = Math.round(clickRect.top + clickRect.height / 2);
    const top = document.elementFromPoint(x, y);
    if (!_hitIsOnTarget(el, top) && !_hitIsOnStickyCard(el, top)) {
      throw _actionabilityError('ACTIONABILITY_OBSCURED', 'Target center is covered by another element', {
        kind,
        locator,
        hit_tag: top?.tagName || null,
      });
    }
    return {
      x,
      y,
      onTarget: true,
      target_token: _targetTokenForElement(el),
      page_revision: pageRevision,
      tag: el.tagName,
      text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 160),
      href: el.href || ''
    };
  }

  async function _actionablePoint(kind, locator, mode, discover) {
    ensurePageObserver();
    const key = _semanticCacheKey(kind, locator, mode);
    const cached = semanticTargetCache.get(key);
    const candidates = cached?.isConnected ? [cached] : discover();
    if (candidates.length !== 1) {
      semanticTargetCache.delete(key);
      throw _actionabilityError(
        'ACTIONABILITY_TARGET_COUNT',
        `Expected exactly one actionable target, found ${candidates.length}`,
        { kind, locator, count: candidates.length }
      );
    }
    const target = candidates[0];
    try {
      const point = await _pointForElement(target, { mode, locator, kind });
      semanticTargetCache.set(key, target);
      return point;
    } catch (error) {
      semanticTargetCache.delete(key);
      throw error;
    }
  }

  function _querySelectorAllDeep(selector) {
    const value = String(selector || '');
    const results = [];
    const seen = new Set();
    const visit = (root) => {
      if (!root) return;
      let matches;
      try {
        matches = root.querySelectorAll(value);
      } catch {
        return;
      }
      for (const el of matches) {
        if (!seen.has(el)) {
          seen.add(el);
          results.push(el);
        }
      }
      let nodes;
      try {
        nodes = root.querySelectorAll('*');
      } catch {
        return;
      }
      for (const el of nodes) {
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(document);
    return results;
  }

  function _hostChain(node) {
    const chain = [];
    let cur = node;
    while (cur) {
      chain.push(cur);
      const parent = cur.parentElement;
      if (parent) {
        cur = parent;
        continue;
      }
      const root = cur.getRootNode?.();
      cur = root && root !== cur && root.host ? root.host : null;
    }
    return chain;
  }

  function _clickableAncestor(node) {
    for (const cur of _hostChain(node)) {
      if (!(cur instanceof Element)) continue;
      const role = cur.getAttribute?.('role');
      if (
        /^(A|BUTTON|SUMMARY|LABEL|INPUT|TEXTAREA|SELECT)$/.test(cur.tagName)
        || role === 'button'
        || role === 'link'
      ) return cur;
    }
    return null;
  }

  function _composedRelated(el, other) {
    if (!el || !other) return false;
    if (el === other) return true;
    if (el.contains?.(other) || other.contains?.(el)) return true;
    return _hostChain(other).includes(el) || _hostChain(el).includes(other);
  }

  function _hitIsOnTarget(el, top) {
    if (!el || !top) return false;
    if (_composedRelated(el, top)) return true;
    const hitClickable = _clickableAncestor(top);
    if (hitClickable && _composedRelated(el, hitClickable)) return true;
    const elClickable = _clickableAncestor(el);
    if (elClickable && _composedRelated(elClickable, top)) return true;
    if (_isStickyOrFixedDescendant(el) && _hitIsOnStickyCard(el, top)) return true;
    return false;
  }

  function _isStickyOrFixedDescendant(el) {
    for (const node of _hostChain(el)) {
      if (!(node instanceof Element)) continue;
      try {
        const pos = window.getComputedStyle(node).position;
        if (pos === 'sticky' || pos === 'fixed') return true;
      } catch { /* detached */ }
    }
    return false;
  }

  function _isHeaderNavToolbar(el) {
    try {
      return Boolean(el.closest?.('header,nav,[role="banner"],[role="navigation"],[role="toolbar"]'));
    } catch {
      return false;
    }
  }

  function _preferInPageTargets(elements) {
    if (!elements || elements.length <= 1) return elements || [];
    const inPage = elements.filter((el) => !_isStickyOrFixedDescendant(el) && !_isHeaderNavToolbar(el));
    const pool = inPage.length ? inPage : elements;
    if (pool.length === 1) return pool;
    const areaOf = (el) => {
      const r = el.getBoundingClientRect();
      return Math.max(0, r.width) * Math.max(0, r.height);
    };
    const distOf = (el) => {
      const r = el.getBoundingClientRect();
      const dx = (r.left + r.width / 2) - (window.innerWidth / 2);
      const dy = (r.top + r.height / 2) - (window.innerHeight / 2);
      return dx * dx + dy * dy;
    };
    let best = pool[0];
    let bestArea = areaOf(best);
    let bestDist = distOf(best);
    for (let i = 1; i < pool.length; i += 1) {
      const el = pool[i];
      const area = areaOf(el);
      const dist = distOf(el);
      if (area > bestArea * 1.15 || (area >= bestArea * 0.85 && dist < bestDist)) {
        best = el;
        bestArea = area;
        bestDist = dist;
      }
    }
    return [best];
  }

  function _accessibleName(el) {
    if (!el) return '';
    try {
      const aria = el.getAttribute?.('aria-label');
      if (aria && String(aria).trim()) return String(aria).trim();
      const labelledBy = el.getAttribute?.('aria-labelledby');
      if (labelledBy) {
        const parts = String(labelledBy).split(/\s+/)
          .map((id) => document.getElementById(id)?.innerText?.trim())
          .filter(Boolean);
        if (parts.length) return parts.join(' ');
      }
      const title = el.getAttribute?.('title');
      if (title && String(title).trim()) return String(title).trim();
      if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value) {
        return String(el.value).trim();
      }
      return String(el.innerText || el.value || '').trim();
    } catch {
      return String(el.innerText || el.value || '').trim();
    }
  }

  function findPointBySelector(selector, mode = 'click') {
    ensurePageObserver();
    const value = String(selector || '');
    return _actionablePoint('selector', value, mode, () =>
      _querySelectorAllDeep(value).filter(_isVisible)
    );
  }

  function findPointByText(text, mode = 'click') {
    ensurePageObserver();
    const needle = String(text || '').toLowerCase().trim();
    if (!needle) {
      throw _actionabilityError('ACTIONABILITY_EMPTY_LOCATOR', 'Text locator is empty', { kind: 'text' });
    }
    // Semantic-first interactive set. Prefer accessible-name / ARIA roles before
    // brittle CSS. Still interactive-only — never scan p/span/li/td (port wedge).
    const elements = _querySelectorAllDeep(
      'a,button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=button],input[type=submit],label,summary'
    ).filter(_isVisible);
    const candidates = [];
    for (const el of elements) {
      const name = _accessibleName(el).toLowerCase();
      const raw = (el.innerText || el.value || '').trim().toLowerCase();
      let score = 99;
      for (const [value, base] of [[name, 0], [raw, 3]]) {
        if (!value || !value.includes(needle)) continue;
        const s = value === needle ? base : (value.startsWith(needle) ? base + 1 : base + 2);
        if (s < score) score = s;
      }
      if (score < 99) candidates.push({ el, score });
    }
    const bestScore = Math.min(...candidates.map((candidate) => candidate.score));
    return _actionablePoint('text', needle, mode, () =>
      _preferInPageTargets(
        candidates.filter((candidate) => candidate.score === bestScore).map((candidate) => candidate.el)
      )
    );
  }

  function hasSelector(selector) {
    return _querySelectorAllDeep(String(selector || '')).length > 0;
  }

  function getDOMSnapshot() {
    // Excludes div/span containers — they duplicate all descendant text via innerText.
    // Focuses on interactive + semantic elements only.
    const SELECTOR = 'a,button,input,textarea,select,[role],h1,h2,h3,h4,h5,h6,label,p,li,td,th';
    const elements = Array.from(document.querySelectorAll(SELECTOR))
      .filter(_isVisible)
      .filter(el => {
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        return t.length > 0 || el.href || el.getAttribute('role');
      })
      .slice(0, 200)
      .map((el, i) => {
        const entry = {
          i,
          ref: _targetTokenForElement(el),
          page_revision: pageRevision,
          tag: el.tagName,
          role: el.getAttribute('role') || '',
          text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 120),
          href: el.href || ''
        };
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
          entry.value = String(el.value || '').slice(0, 100);
          entry.input_type = el.type || '';
          entry.name = el.name || el.id || '';
        }
        return entry;
      });
    return { page_revision: pageRevision, elements };
  }

  function getPageContext(sections, options) {
    // Lightweight overview — compact by default when sections omitted (agent path).
    // Selective sections keep full hrefs unless options.compact === true.
    if (sections && !Array.isArray(sections) && typeof sections === 'object') {
      options = sections;
      sections = options.sections;
    }
    options = options || {};
    const allowed = ['url', 'title', 'headings', 'nav', 'links', 'buttons', 'inputs'];
    if (sections !== undefined && (!Array.isArray(sections) || sections.length > allowed.length
      || sections.some(section => !allowed.includes(section)))) {
      throw new Error('page_context sections must be an array of url, title, headings, nav, links, buttons, inputs');
    }
    const compact = sections === undefined
      ? options.compact !== false
      : options.compact === true;
    const wants = section => sections === undefined || sections.includes(section);
    function vis(el) { return _isVisible(el); }
    // Keep actionable names long enough to click_text; size win is href+caps.
    const textCap = 60;
    const headCap = compact ? 6 : 8;
    const headTextCap = compact ? 80 : 100;
    const navCap = compact ? 8 : 20;
    // Raised soft-caps (defect-005); prefer main/article before sidebar fill (defect-003).
    const linkCap = compact ? 20 : 35;
    const btnCap = compact ? 10 : 15;
    const inputCap = compact ? 8 : 10;

    function labelOf(el) {
      return _accessibleName(el).slice(0, textCap);
    }
    function hrefOf(el) {
      const href = el.href || '';
      if (!href) return '';
      if (!compact) return href;
      try {
        const u = new URL(href, location.href);
        const base = new URL(location.href);
        if (u.origin === base.origin) {
          return `${u.pathname}${u.search}${u.hash}`.slice(0, 80);
        }
        return `${u.host}${u.pathname}`.slice(0, 80);
      } catch {
        return String(href).slice(0, 80);
      }
    }
    function linkEntry(el) {
      const text = labelOf(el);
      if (!text) return null;
      const href = hrefOf(el);
      return href ? { text, href } : { text };
    }

    let navEntries = [];
    if (wants('nav')) {
      let navEls;
      if (compact) {
        // Primary chrome only — header/banner/first nav landmark (role/name first).
        const root = document.querySelector('header,[role="banner"],nav,[role="navigation"]');
        navEls = root
          ? Array.from(root.querySelectorAll(
              'a,[role="link"],button,[role="button"],[role="tab"],[role="menuitem"]'
            )).filter(vis)
          : Array.from(document.querySelectorAll('nav a,[role="navigation"] a')).filter(vis);
      } else {
        navEls = Array.from(document.querySelectorAll(
          'nav a,[role="navigation"] a,[role="menubar"] *,[role="tablist"] *'
        )).filter(vis);
      }
      const seen = new Set();
      for (const el of navEls) {
        const entry = linkEntry(el);
        if (!entry) continue;
        const key = `${entry.text}|${entry.href || ''}`;
        if (seen.has(key)) continue;
        seen.add(key);
        navEntries.push(entry);
        if (navEntries.length >= navCap) break;
      }
    }

    let linkEntries = [];
    if (wants('links')) {
      const navKeys = new Set(navEntries.map((x) => `${x.text}|${x.href || ''}`));
      const chromeSel = 'nav,[role="navigation"],[role="menubar"],[role="tablist"],header,[role="banner"]';
      const mainRoots = Array.from(document.querySelectorAll('main,article,[role="main"]'));
      const inMain = (a) => mainRoots.length > 0 && mainRoots.some((root) => root.contains(a));
      const all = Array.from(document.querySelectorAll('a,[role="link"]')).filter(vis)
        .filter((a) => !a.closest(chromeSel));
      // Prefer main/article deep links so dense docs sidebars do not crowd out content.
      const preferred = mainRoots.length ? all.filter(inMain) : [];
      const rest = mainRoots.length ? all.filter((a) => !inMain(a)) : all;
      // Compact: also surface secondary landmark links agents click (sidebar posts).
      const secondary = compact
        ? Array.from(document.querySelectorAll(
            'nav a,[role="navigation"] a,[role="menubar"] a,[role="tablist"] a'
          )).filter(vis)
        : [];
      const seen = new Set();
      for (const el of [...preferred, ...rest, ...secondary]) {
        const entry = linkEntry(el);
        if (!entry || !entry.href) continue;
        const key = `${entry.text}|${entry.href}`;
        if (navKeys.has(key) || seen.has(key)) continue;
        seen.add(key);
        linkEntries.push(entry);
        if (linkEntries.length >= linkCap) break;
      }
    }

    return {
      url: location.href,
      title: compact ? String(document.title || '').slice(0, 80) : document.title,
      page_revision: pageRevision,
      ...(wants('headings') ? { headings: Array.from(document.querySelectorAll('h1,h2,h3')).filter(vis)
        .map(h => ({ tag: h.tagName, text: (h.innerText || '').trim().slice(0, headTextCap) }))
        .filter(h => h.text).slice(0, headCap) } : {}),
      ...(wants('nav') ? { nav: navEntries } : {}),
      ...(wants('links') ? { links: linkEntries } : {}),
      ...(wants('buttons') ? { buttons: Array.from(document.querySelectorAll(
          'button,[role="button"],input[type=button],input[type=submit]'
        )).filter(vis)
        .map(b => labelOf(b)).filter(Boolean).slice(0, btnCap) } : {}),
      ...(wants('inputs') ? { inputs: Array.from(document.querySelectorAll('input,textarea,select')).filter(vis)
        .map(el => ({
          name: (el.name || el.id || el.getAttribute('aria-label') || el.placeholder || '').slice(0, textCap),
          type: el.type || el.tagName.toLowerCase(),
          value: String(el.value || '').slice(0, compact ? 40 : 60),
        }))
        .filter(x => x.name).slice(0, inputCap) } : {}),
      ...(compact ? { compact: true } : {}),
    };
  }

  function flashLabel(label) {
    return label;
  }

  function ensureSessionCursorWarm(options = {}) {
    // After reinject / setAgentIdentity: show labeled cursor immediately at last
    // (x,y) or viewport center. Do not wait for the next moveTo first-show.
    const warm = options.sessionWarm !== false && options.watchable !== false;
    if (!warm) return getStatus();
    window.__cometControlSessionWarm = true;
    if (!cursorEl) createOverlay({ sessionWarm: true, forceVisible: true });
    if (!(Number.isFinite(cursorX) && Number.isFinite(cursorY))
        || cursorX < 0 || cursorY < 0) {
      cursorX = Math.round((window.innerWidth || 800) / 2);
      cursorY = Math.round((window.innerHeight || 600) / 2);
    }
    if (cursorEl) {
      cursorEl.style.transform = `translate(${cursorX}px, ${cursorY}px)`;
      cursorEl.classList.add('comet-control-visible');
      isVisible = true;
      cursorPhase = 'idle';
    }
    return getStatus();
  }

  function setIdentity(identity = {}) {
    const color = /^#[0-9a-f]{6}$/i.test(String(identity.color || '')) ? String(identity.color) : '#64d8ff';
    agentIdentity = {
      agentId: String(identity.agentId || '').slice(0, 96),
      label: String(identity.label || identity.agentId || 'Browser agent').trim().slice(0, 80),
      sessionId: String(identity.sessionId || '').slice(0, 96),
      color
    };
    const watchable = identity.watchable !== false && identity.silent !== true;
    // Explicit sessionWarm only (preflight / post-nav reinject). Label refresh must
    // not force-visible after silent/navigation_only parkIdle.
    const sessionWarm = identity.sessionWarm === true;
    if (sessionWarm) window.__cometControlSessionWarm = true;
    if (sessionWarm && (!cursorEl || !labelEl)) {
      createOverlay({ sessionWarm: true, forceVisible: true });
    }
    if (!cursorEl || !labelEl) return agentIdentity;
    cursorEl.dataset.agentId = agentIdentity.agentId;
    cursorEl.dataset.agentLabel = agentIdentity.label;
    cursorEl.dataset.sessionId = agentIdentity.sessionId;
    cursorEl.style.setProperty('--comet-control-cursor-color', agentIdentity.color);
    labelEl.textContent = agentIdentity.label;
    labelEl.style.display = agentIdentity.label ? 'block' : 'none';
    if (sessionWarm) {
      // Force visible immediately — no opacity-0 offscreen debut.
      if (!(Number.isFinite(cursorX) && Number.isFinite(cursorY))
          || cursorX < 0 || cursorY < 0) {
        cursorX = Math.round((window.innerWidth || 800) / 2);
        cursorY = Math.round((window.innerHeight || 600) / 2);
      }
      cursorEl.style.transform = `translate(${cursorX}px, ${cursorY}px)`;
      cursorEl.classList.add('comet-control-visible');
      isVisible = true;
    }
    return agentIdentity;
  }

  function clearIdentity() {
    agentIdentity = { agentId: '', label: '', sessionId: '', color: '#64d8ff' };
    if (cursorEl) {
      delete cursorEl.dataset.agentId;
      delete cursorEl.dataset.agentLabel;
      delete cursorEl.dataset.sessionId;
      cursorEl.style.removeProperty('--comet-control-cursor-color');
    }
    if (labelEl) {
      labelEl.textContent = '';
      labelEl.style.display = 'none';
    }
    return agentIdentity;
  }

  function getStatus() {
    window.__cometControlAgentCursorAlive = true;
    const pointerBounds = pointerEl?.getBoundingClientRect?.();
    const labelBounds = labelEl?.getBoundingClientRect?.();
    return {
      visible: isVisible,
      x: Math.round(cursorX),
      y: Math.round(cursorY),
      phase: cursorPhase,
      agent_id: agentIdentity.agentId,
      agent_label: agentIdentity.label,
      session_id: agentIdentity.sessionId,
      color: agentIdentity.color,
      page_revision: pageRevision,
      overlay_present: Boolean(cursorEl && document.getElementById(COMET_CONTROL_CURSOR_ID)),
      observer_active: Boolean(pageObserver),
      pointer_bounds: pointerBounds ? { top: pointerBounds.top, right: pointerBounds.right, bottom: pointerBounds.bottom, left: pointerBounds.left } : null,
      label_bounds: labelBounds ? { top: labelBounds.top, right: labelBounds.right, bottom: labelBounds.bottom, left: labelBounds.left } : null,
      label_below_pointer: Boolean(pointerBounds && labelBounds && labelBounds.top >= pointerBounds.bottom),
      url: location.href,
      title: document.title
    };
  }

  function hide() {
    if (cursorEl) cursorEl.classList.remove('comet-control-visible');
    if (cursorEl) cursorEl.classList.remove('comet-control-moving');
    isVisible = false;
    cursorPhase = 'idle';
  }

  function invalidateConnectionState() {
    _bumpPageRevision();
    return { invalidated: true, page_revision: pageRevision };
  }

  function destroy() {
    if (animationFrame) cancelAnimationFrame(animationFrame);
    if (arrivalTimer) clearTimeout(arrivalTimer);
    arrivalTimer = null;
    stopPageObserver();
    clearClickRings();
    const old = document.getElementById(COMET_CONTROL_CURSOR_ID);
    if (old) old.remove();
    document.querySelector('style[data-comet-control-cursor-style]')?.remove();
    cursorEl = null;
    pointerEl = null;
    labelEl = null;
    isVisible = false;
    cursorPhase = 'idle';
    window.__cometControlAgentCursorInjected = false;
    window.__cometControlAgentCursorAlive = false;
    document.documentElement.removeAttribute('data-comet-control-agent-cursor-injected');
  }

  // Tear down overlay + page-wide observer but keep the message listener so the
  // next leased action can reuse this world without reinjection retries.
  function parkIdle(options = {}) {
    // parkIdle is silent/bench + closeout only — watchable leases must not call this
    // between chained acts (session_closeout / silent benches tear the overlay).
    if (animationFrame) cancelAnimationFrame(animationFrame);
    if (arrivalTimer) clearTimeout(arrivalTimer);
    arrivalTimer = null;
    stopPageObserver();
    // keepClickRing: trusted CDP path may park overlay while ring finishes (~280ms)
    if (!options || options.keepClickRing !== true) clearClickRings();
    const old = document.getElementById(COMET_CONTROL_CURSOR_ID);
    if (old) old.remove();
    document.querySelector('style[data-comet-control-cursor-style]')?.remove();
    cursorEl = null;
    pointerEl = null;
    labelEl = null;
    isVisible = false;
    cursorPhase = 'idle';
    cursorX = -100;
    cursorY = -100;
    window.__cometControlSessionWarm = false;
    window.__cometControlAgentCursorAlive = true;
    return getStatus();
  }

  // ---- Evaluate (runs JS in page main world via content script) ----
  // This avoids CDP debugger attach which fails on pages with extension frames.
  function evaluateInPage(expression) {
    // Use Function constructor to evaluate in page's main world (not isolated world)
    // This is safe because we control the expression source (service worker, not page content)
    try {
      const fn = new Function(`return (${expression});`);
      const result = fn();
      // Serialize safely — handle primitives, arrays, plain objects; skip functions/DOM nodes
      if (result === undefined) return undefined;
      if (result === null) return null;
      if (typeof result === 'function') return '[Function]';
      if (typeof result === 'object') {
        try { return JSON.parse(JSON.stringify(result)); } catch { return String(result); }
      }
      return result;
    } catch (e) {
      throw new Error(`evaluate: ${e.message}`);
    }
  }

  // ---- Screenshot via content script (fallback when CDP fails) ----
  function captureScreenshot() {
    // Use html2canvas-like approach: capture the visible viewport via canvas
    // This is a simplified version — for full page, we'd need scrolling
    const canvas = document.createElement('canvas');
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    const ctx = canvas.getContext('2d');
    // We can't directly render the page to canvas from a content script,
    // so we return the scroll position and viewport info for the service worker
    // to use with CDP on the main frame only
    return {
      viewport: { x: window.scrollX, y: window.scrollY, width: window.innerWidth, height: window.innerHeight },
      note: "CDP required for actual screenshot"
    };
  }

  // ---- Message Listener (from service worker) ----
  const actions = {
    moveTo, moveToAndWait, pulseClick, showClickRing, clearClickRings, click, tripleClick, rightClick, dblClick,
    focusAndType, fillBySelector, keyPress, showKey, dragTo, scroll,
    getVisibleText, getDOMSnapshot, getPageContext,
    findPointBySelector, findPointByText, hasSelector,
    getStatus, hide, destroy, parkIdle, setIdentity, ensureSessionCursorWarm, clearIdentity, invalidateConnectionState,
    evaluate: evaluateInPage,
    captureScreenshot
  };

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    // Ad/tracker iframes are controllable https. If sendMessage is delivered
    // beyond frame 0, skip page_context/status so DoubleClick/Twitter cannot win.
    if (
      window !== window.top
      && (msg?.action === 'getPageContext' || msg?.action === 'getStatus')
    ) {
      return false;
    }
    const fn = actions[msg.action];
    if (!fn) {
      sendResponse({ success: false, error: `Unknown action: ${msg.action}` });
      return true;
    }
    try {
      const result = fn(...(msg.args || []));
      if (result instanceof Promise) {
        result.then(r => sendResponse({ success: true, result: r }))
              .catch(e => sendResponse({
                success: false,
                error_code: e?.code || 'CONTENT_SCRIPT_ERROR',
                error: String(e?.message || e),
                ...(e?.details ? { details: e.details } : {}),
              }));
        return true;
      }
      sendResponse({ success: true, result });
    } catch (err) {
      sendResponse({
        success: false,
        error_code: err?.code || 'CONTENT_SCRIPT_ERROR',
        error: String(err?.message || err),
        ...(err?.details ? { details: err.details } : {}),
      });
    }
    return true;
  });

  // ---- Init ----
  // Do NOT createOverlay() at startup: always-on overlay interacts badly with
  // Comet trusted/native clicks (EXC_BREAKPOINT). moveTo() creates lazily.

  // Notify service worker that content script is ready
  try { chrome.runtime.sendMessage('comet-control-cursor-ready'); } catch {}
})();
