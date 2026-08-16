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

  const COMET_CONTROL_CURSOR_ID = 'comet-control-agent-cursor-overlay';
  const POINTER_SVG = chrome.runtime.getURL('images/pointer-shape-animated.svg') + '?v=codex-like-2';

  function _isCursorMutation(record) {
    const target = record?.target instanceof Element ? record.target : record?.target?.parentElement;
    return Boolean(target?.closest?.(`#${COMET_CONTROL_CURSOR_ID}`));
  }

  function _bumpPageRevision(records) {
    if (records?.length && records.every(_isCursorMutation)) return;
    pageRevision += 1;
    for (const [token, target] of targetElementsByToken) {
      if (!target.isConnected) targetElementsByToken.delete(token);
    }
    semanticTargetCache.clear();
  }

  new MutationObserver(_bumpPageRevision).observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['aria-disabled', 'class', 'disabled', 'hidden', 'readonly', 'style'],
  });

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
      width: 28px;
      height: 28px;
      display: block;
      transform: translate(-1px, -1px);
      transform-origin: 1px 1px;
      filter:
        drop-shadow(0 0 12px var(--comet-control-cursor-color, rgba(73, 182, 255, 0.9)))
        drop-shadow(0 2px 4px rgba(0,0,0,0.45));
      transition: transform 0.12s ease;
      animation: comet-control-pointer-idle 1.7s ease-in-out infinite;
      user-select: none;
      -webkit-user-drag: none;
    }
    #${COMET_CONTROL_CURSOR_ID} .comet-control-cursor-agent-label {
      position: absolute;
      left: 22px;
      top: 30px;
      display: none;
      max-width: 320px;
      padding: 6px 12px;
      border: 1px solid color-mix(in srgb, var(--comet-control-cursor-color, #64d8ff) 72%, white);
      border-radius: 999px;
      background: rgba(10, 18, 28, 0.94);
      box-shadow: 0 3px 14px rgba(0, 0, 0, 0.38);
      color: #fff;
      font: 700 14px/1.25 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
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
  `;

  // ---- DOM Setup ----
  function createOverlay() {
    // Preserve last-known position + visibility across script re-injection.
    // Without this, a service-worker idle restart or extension reload silently
    // resets the cursor to (-100, -100) opacity:0 — the operator sees the
    // cursor vanish mid-flow until the agent's next cursor_move. Read the
    // prior element's transform + visibility, then restore after recreating.
    const old = document.getElementById(COMET_CONTROL_CURSOR_ID);
    let priorTransform = null;
    let priorVisible = false;
    let priorIdentity = null;
    if (old) {
      priorTransform = old.style.transform || null;
      priorVisible = old.classList.contains('comet-control-visible');
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

  function moveTo(x, y) {
    if (!cursorEl) createOverlay();
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
    const wait = Math.min(GLIDE_MS + 40, Math.max(0, timeoutMs));
    return new Promise((resolve) => setTimeout(() => resolve(getStatus()), wait));
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
    // expected element's actual bounds.
    const related = elements.filter((el) => el === hit || el.contains(hit));
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
        Array.from(document.querySelectorAll(selector)).filter(_isVisible).filter(
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
        Array.from(document.querySelectorAll(
          'a,button,[role=button],input[type=button],input[type=submit],label,summary'
        )).filter(_isVisible)
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

    // Resolve the expected semantic target atomically at click time. This blocks
    // stale coordinates from clicking a different element after layout movement.
    const { target: el, verifiedBy } = _expectedTargetAtCursor(expectation);

    if (el) {
      const scrollX = window.scrollX || 0;
      const scrollY = window.scrollY || 0;
      const opts = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new MouseEvent('mousedown', { ...opts, clientX: cursorX, clientY: cursorY, pageX: cursorX + scrollX, pageY: cursorY + scrollY }));
      el.dispatchEvent(new MouseEvent('mouseup', { ...opts, clientX: cursorX, clientY: cursorY, pageX: cursorX + scrollX, pageY: cursorY + scrollY }));
      el.dispatchEvent(new MouseEvent('click', { ...opts, clientX: cursorX, clientY: cursorY, pageX: cursorX + scrollX, pageY: cursorY + scrollY }));
    }
    flashLabel('click');
    return {
      clicked: Boolean(el),
      verified_by: verifiedBy,
      tag: el?.tagName || '',
      text: _clickTextValue(el).slice(0, 160),
      href: el?.href || ''
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

  function focusAndType(text, opts = {}, expectation = {}) {
    if (!cursorEl) return;
    const { target: el } = _expectedTargetAtCursor(expectation);
    if (!el) return;
    el.focus();
    if (opts.append && el.value !== undefined) {
      // Insert text at cursor / append
      const start = el.selectionStart || el.value.length;
      const end = el.selectionEnd || el.value.length;
      el.value = el.value.slice(0, start) + text + el.value.slice(end);
      el.selectionStart = el.selectionEnd = start + text.length;
    } else if (el.value !== undefined) {
      el.value = text;
    } else if ('innerText' in el) {
      el.innerText = text;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    flashLabel('type');
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

  function scroll(deltaX, deltaY) {
    const el = document.elementFromPoint(cursorX, cursorY) || document.documentElement;
    el.scrollBy(deltaX, deltaY);
    const we = new WheelEvent('wheel', {
      bubbles: true, cancelable: true, view: window,
      clientX: cursorX, clientY: cursorY,
      deltaX, deltaY
    });
    el.dispatchEvent(we);
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
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height / 2);
    const top = document.elementFromPoint(x, y);
    if (!top || !(top === el || el.contains(top))) {
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

  function findPointBySelector(selector, mode = 'click') {
    const value = String(selector || '');
    return _actionablePoint('selector', value, mode, () =>
      Array.from(document.querySelectorAll(value)).filter(_isVisible)
    );
  }

  function findPointByText(text, mode = 'click') {
    const needle = String(text || '').toLowerCase().trim();
    if (!needle) {
      throw _actionabilityError('ACTIONABILITY_EMPTY_LOCATOR', 'Text locator is empty', { kind: 'text' });
    }
    // Interactive-only. Scanning p/span/li/td with innerText on Seller (Samantha
    // panel, dense order DOM) blocks the message port long enough that SW
    // sendMessageFast+probes false-negative as "Content script missing".
    const elements = Array.from(document.querySelectorAll(
      'a,button,[role=button],input[type=button],input[type=submit],label,summary'
    )).filter(_isVisible);
    const candidates = [];
    for (const el of elements) {
      const value = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
      if (!value.includes(needle)) continue;
      candidates.push({ el, score: value === needle ? 0 : (value.startsWith(needle) ? 1 : 2) });
    }
    const bestScore = Math.min(...candidates.map((candidate) => candidate.score));
    return _actionablePoint('text', needle, mode, () =>
      candidates.filter((candidate) => candidate.score === bestScore).map((candidate) => candidate.el)
    );
  }

  function hasSelector(selector) {
    return Boolean(document.querySelector(String(selector || '')));
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

  function getPageContext() {
    // Lightweight overview — use before snapshot for progressive disclosure.
    function vis(el) { return _isVisible(el); }
    return {
      url: location.href,
      title: document.title,
      page_revision: pageRevision,
      headings: Array.from(document.querySelectorAll('h1,h2,h3')).filter(vis)
        .map(h => ({ tag: h.tagName, text: h.innerText?.trim().slice(0, 100) })).filter(h => h.text).slice(0, 8),
      nav: Array.from(document.querySelectorAll('nav a,[role="navigation"] a,[role="menubar"] *,[role="tablist"] *'))
        .filter(vis).map(a => ({ text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 60), href: a.href || '' }))
        .filter(x => x.text).slice(0, 20),
      buttons: Array.from(document.querySelectorAll('button,[role="button"]')).filter(vis)
        .map(b => (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 60)).filter(Boolean).slice(0, 15),
      inputs: Array.from(document.querySelectorAll('input,textarea,select')).filter(vis)
        .map(el => ({ name: el.name || el.id || el.placeholder || '', type: el.type || el.tagName.toLowerCase(), value: String(el.value || '').slice(0, 60) }))
        .filter(x => x.name).slice(0, 10)
    };
  }

  function flashLabel(label) {
    return label;
  }

  function setIdentity(identity = {}) {
    const color = /^#[0-9a-f]{6}$/i.test(String(identity.color || '')) ? String(identity.color) : '#64d8ff';
    agentIdentity = {
      agentId: String(identity.agentId || '').slice(0, 96),
      label: String(identity.label || identity.agentId || 'Browser agent').trim().slice(0, 80),
      sessionId: String(identity.sessionId || '').slice(0, 96),
      color
    };
    if (!cursorEl || !labelEl) return agentIdentity;
    cursorEl.dataset.agentId = agentIdentity.agentId;
    cursorEl.dataset.agentLabel = agentIdentity.label;
    cursorEl.dataset.sessionId = agentIdentity.sessionId;
    cursorEl.style.setProperty('--comet-control-cursor-color', agentIdentity.color);
    labelEl.textContent = agentIdentity.label;
    labelEl.style.display = agentIdentity.label ? 'block' : 'none';
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
    const old = document.getElementById(COMET_CONTROL_CURSOR_ID);
    if (old) old.remove();
    window.__cometControlAgentCursorInjected = false;
    window.__cometControlAgentCursorAlive = false;
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
    moveTo, moveToAndWait, click, tripleClick, rightClick, dblClick,
    focusAndType, keyPress, showKey, dragTo, scroll,
    getVisibleText, getDOMSnapshot, getPageContext,
    findPointBySelector, findPointByText, hasSelector,
    getStatus, hide, destroy, setIdentity, clearIdentity, invalidateConnectionState,
    evaluate: evaluateInPage,
    captureScreenshot
  };

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
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
  createOverlay();

  // Notify service worker that content script is ready
  try { chrome.runtime.sendMessage('comet-control-cursor-ready'); } catch {}
})();
