const CDP_EVENT_RING_MAX = 1000;
const ASSET_INVENTORY_MAX = 20;
const ASSET_LIMIT_MAX = 500;

let cdpSequence = 0;
const cdpEventsByTab = new Map();
const dialogsByTab = new Map();
const assetInventories = new Map();

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function boundedClone(value, maxChars = 100_000) {
  try {
    const json = JSON.stringify(value ?? {});
    if (json.length <= maxChars) return JSON.parse(json);
    return { truncated: true, preview: json.slice(0, maxChars) };
  } catch {
    return { unserializable: true, preview: String(value).slice(0, 2000) };
  }
}

export function recordParityCdpEvent(source, method, params) {
  const tabId = Number(source?.tabId || 0);
  if (!tabId) return;
  cdpSequence += 1;
  let entries = cdpEventsByTab.get(tabId);
  if (!entries) {
    entries = [];
    cdpEventsByTab.set(tabId, entries);
  }
  entries.push({
    sequence: cdpSequence,
    method: String(method || ""),
    params: boundedClone(params),
    source: {
      tabId,
      ...(source?.targetId ? { targetId: source.targetId } : {}),
      ...(source?.sessionId ? { sessionId: source.sessionId } : {}),
      ...(source?.extensionId ? { extensionId: source.extensionId } : {}),
    },
  });
  if (entries.length > CDP_EVENT_RING_MAX) {
    entries.splice(0, entries.length - CDP_EVENT_RING_MAX);
  }
  if (method === "Page.javascriptDialogOpening") {
    dialogsByTab.set(tabId, {
      type: String(params?.type || "alert"),
      message: String(params?.message || "").slice(0, 2000),
      default_prompt: String(params?.defaultPrompt || "").slice(0, 2000),
      url: String(params?.url || ""),
      opened_at: Date.now(),
    });
  }
  if (method === "Page.javascriptDialogClosed") dialogsByTab.delete(tabId);
}

export function cleanupParityTab(tabId) {
  cdpEventsByTab.delete(Number(tabId));
  dialogsByTab.delete(Number(tabId));
  for (const [id, inventory] of assetInventories) {
    if (Number(inventory.tabId) === Number(tabId)) assetInventories.delete(id);
  }
}

export function getParityDialog(tabId) {
  return dialogsByTab.get(Number(tabId)) || null;
}

/** Race a click promise against a JS alert/confirm/prompt opening on the tab. */
export async function raceClickWithDialog(tabId, clickPromise, waitMs = 8000) {
  const dialogWait = Math.max(500, Number(waitMs) || 8000);
  const click = Promise.resolve(clickPromise).then((value) => ({ clicked: true, value }));
  click.catch(() => {});
  const dialog = (async () => {
    const deadline = Date.now() + dialogWait;
    while (Date.now() < deadline) {
      const current = dialogsByTab.get(Number(tabId));
      if (current) return { dialog: current };
      await sleep(25);
    }
    return new Promise(() => {});
  })();
  // Dialog can still win during dialogWait. After that, do not wait forever on click.
  const clickTimeout = sleep(dialogWait + 7000).then(() => {
    throw new Error("Content script action timed out: click");
  });
  return Promise.race([click, dialog, clickTimeout]);
}

export async function dismissParityDialog(tabId, send) {
  if (!dialogsByTab.has(Number(tabId))) return false;
  await send(Number(tabId), "Page.handleJavaScriptDialog", { accept: false }).catch(() => {});
  dialogsByTab.delete(Number(tabId));
  return true;
}

function locatorSpec(action) {
  const raw = action.locator && typeof action.locator === "object" ? action.locator : action;
  return {
    by: String(raw.by || (raw.selector ? "css" : raw.role ? "role" : raw.text ? "text" : "css")),
    selector: String(raw.selector || ""),
    role: String(raw.role || ""),
    name: raw.name == null ? null : String(raw.name),
    text: raw.text == null ? null : String(raw.text),
    label: raw.label == null ? null : String(raw.label),
    placeholder: raw.placeholder == null ? null : String(raw.placeholder),
    testId: raw.testId == null ? (raw.test_id == null ? null : String(raw.test_id)) : String(raw.testId),
    exact: Boolean(raw.exact),
    visible: raw.visible !== false,
    hasText: raw.hasText == null ? (raw.has_text == null ? null : String(raw.has_text)) : String(raw.hasText),
    notHasText: raw.notHasText == null ? (raw.not_has_text == null ? null : String(raw.not_has_text)) : String(raw.notHasText),
    within: raw.within == null ? null : String(raw.within),
    nth: raw.nth == null ? null : Number(raw.nth),
    first: Boolean(raw.first),
    last: Boolean(raw.last),
  };
}

function frameSelectors(action) {
  const raw = action.frame_selectors ?? action.frameSelectors ?? action.locator?.frame_selectors
    ?? action.locator?.frameSelectors ?? action.frame_selector ?? action.frameSelector
    ?? action.locator?.frame_selector ?? action.locator?.frameSelector;
  if (raw == null || raw === "") return [];
  return (Array.isArray(raw) ? raw : [raw]).map(String).filter(Boolean);
}

function frameElements(selector) {
  return Array.from(document.querySelectorAll(selector))
    .filter((element) => element instanceof HTMLIFrameElement || element instanceof HTMLFrameElement)
    .map((element, index) => {
      const rect = element.getBoundingClientRect();
      return {
        index,
        src: element.src || "",
        name: element.name || "",
        left: rect.left + (element.clientLeft || 0),
        top: rect.top + (element.clientTop || 0),
      };
    });
}

async function resolveFrameTargets(tabId, selectors) {
  if (!selectors.length) return [{ frameId: 0, offsetX: 0, offsetY: 0, chain: [] }];
  const frames = await chrome.webNavigation.getAllFrames({ tabId });
  let targets = [{ frameId: 0, offsetX: 0, offsetY: 0, chain: [] }];
  for (const selector of selectors) {
    const next = [];
    for (const target of targets) {
      const result = await chrome.scripting.executeScript({
        target: { tabId, frameIds: [target.frameId] },
        func: frameElements,
        args: [selector],
      });
      const matched = result?.[0]?.result || [];
      const children = (frames || []).filter((frame) => Number(frame.parentFrameId) === Number(target.frameId));
      const used = new Set();
      for (const item of matched) {
        let child = children.find((frame) => !used.has(frame.frameId) && item.src && frame.url === item.src);
        if (!child) child = children.find((frame) => !used.has(frame.frameId));
        if (!child) continue;
        used.add(child.frameId);
        next.push({
          frameId: child.frameId,
          offsetX: target.offsetX + Number(item.left || 0),
          offsetY: target.offsetY + Number(item.top || 0),
          chain: [...target.chain, selector],
        });
      }
    }
    if (!next.length) throw new Error(`No frame matched selector: ${selector}`);
    targets = next;
  }
  return targets;
}

function queryLocator(spec) {
  const normal = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  const matches = (actual, expected, exact) => {
    const a = normal(actual).toLowerCase();
    const e = normal(expected).toLowerCase();
    return exact ? a === e : a.includes(e);
  };
  const isSubmitter = (element) => {
    const tag = element.tagName.toLowerCase();
    if (tag === "button") {
      const type = String(element.getAttribute("type") || "submit").toLowerCase();
      return type === "submit";
    }
    if (tag === "input") {
      return ["submit", "image"].includes(String(element.type || "").toLowerCase());
    }
    return false;
  };
  // Prefer exact, then prefix, then includes; break ties toward form submitters.
  // When exact:true finds no hit, soft-fallback only to a unique ranked winner so
  // novice short names like "Search" still reach "Search groceries" without
  // clicking a competing hero "Search catalog" control.
  const resolveNamed = (candidates, expected, exact, nameOf) => {
    const e = normal(expected).toLowerCase();
    if (!e) return candidates;
    const scored = candidates.map((element) => {
      const a = normal(nameOf(element)).toLowerCase();
      let score = -1;
      if (a === e) score = 300;
      else if (a.startsWith(`${e} `) || a.startsWith(e)) score = 200;
      else if (a.includes(e)) score = 100;
      if (score >= 0 && isSubmitter(element)) score += 10;
      return { element, score };
    }).filter((item) => item.score >= 0);
    if (!scored.length) return [];
    const exactHits = scored.filter((item) => item.score >= 300);
    if (exactHits.length) return exactHits.map((item) => item.element);
    if (!exact) {
      return scored
        .filter((item) => matches(nameOf(item.element), expected, false))
        .map((item) => item.element);
    }
    scored.sort((left, right) => right.score - left.score);
    const top = scored[0].score;
    const winners = scored.filter((item) => item.score === top);
    if (winners.length === 1) return [winners[0].element];
    const submitters = winners.filter((item) => isSubmitter(item.element));
    if (submitters.length === 1) return [submitters[0].element];
    return [];
  };
  const visible = (element) => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0
      && style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || 1) > 0 && !element.closest('[aria-hidden="true"]');
  };
  const implicitRole = (element) => {
    const explicit = element.getAttribute("role");
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a" && element.hasAttribute("href")) return "link";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "img") return "img";
    if (tag === "summary") return "button";
    if (tag === "input") {
      const type = String(element.type || "text").toLowerCase();
      if (["button", "submit", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "number") return "spinbutton";
      if (type !== "hidden") return "textbox";
    }
    return "";
  };
  const accessibleName = (element) => {
    const labelledBy = element.getAttribute("aria-labelledby");
    const labelled = labelledBy
      ? labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.textContent || "").join(" ")
      : "";
    const labels = element.labels ? Array.from(element.labels).map((label) => label.innerText || label.textContent || "").join(" ") : "";
    return normal(element.getAttribute("aria-label") || labelled || labels || element.alt
      || element.innerText || element.value || element.title || element.placeholder || "");
  };
  const cssPath = (element) => {
    if (element.id) return `#${CSS.escape(element.id)}`;
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.documentElement) {
      let part = current.tagName.toLowerCase();
      const siblings = current.parentElement
        ? Array.from(current.parentElement.children).filter((item) => item.tagName === current.tagName)
        : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(" > ");
  };

  const isStickyOrFixed = (node) => {
    try {
      const pos = getComputedStyle(node).position;
      return pos === "sticky" || pos === "fixed";
    } catch {
      return false;
    }
  };
  const stickyCardRect = (element, nameRect) => {
    let stuck = false;
    let hops = 0;
    for (let node = element; node && node.nodeType === Node.ELEMENT_NODE && hops < 16; node = node.parentElement, hops += 1) {
      if (isStickyOrFixed(node)) {
        stuck = true;
        break;
      }
    }
    if (!stuck) return nameRect;
    const elX = nameRect.left + nameRect.width / 2;
    hops = 0;
    for (let node = element.parentElement; node && hops < 12; node = node.parentElement, hops += 1) {
      const tag = node.tagName;
      const role = (node.getAttribute("role") || "").toLowerCase();
      if (tag === "HTML" || tag === "BODY" || tag === "MAIN" || role === "main") continue;
      if (isStickyOrFixed(node)) continue;
      const nr = node.getBoundingClientRect();
      if (nr.width >= window.innerWidth * 0.9 || nr.height >= window.innerHeight * 0.8) continue;
      if (nr.height <= nameRect.height * 1.5) continue;
      const nx = nr.left + nr.width / 2;
      if (Math.abs(nx - elX) > Math.max(80, nameRect.width)) continue;
      return nr;
    }
    return nameRect;
  };

  const root = spec.within ? document.querySelector(spec.within) : document;
  if (!root) return [];
  let elements = [];
  if (spec.by === "css") elements = Array.from(root.querySelectorAll(spec.selector));
  if (spec.by === "text") {
    const textCandidates = Array.from(root.querySelectorAll("a,button,input,textarea,select,label,summary,[role],h1,h2,h3,h4,h5,h6,p,span,li,td,th"));
    elements = spec.text == null
      ? textCandidates
      : resolveNamed(
        textCandidates,
        spec.text,
        Boolean(spec.exact),
        (element) => element.innerText || element.textContent || element.value,
      );
  }
  if (spec.by === "role") {
    const roleCandidates = Array.from(root.querySelectorAll("*"))
      .filter((element) => implicitRole(element) === spec.role);
    elements = spec.name == null
      ? roleCandidates
      : resolveNamed(roleCandidates, spec.name, Boolean(spec.exact), accessibleName);
  }
  if (spec.by === "label") {
    const labels = Array.from(root.querySelectorAll("label"))
      .filter((label) => matches(label.innerText || label.textContent, spec.label, spec.exact));
    elements = labels.map((label) => label.control || label.querySelector("input,textarea,select,button")).filter(Boolean);
  }
  if (spec.by === "placeholder") {
    elements = Array.from(root.querySelectorAll("[placeholder]"))
      .filter((element) => matches(element.getAttribute("placeholder"), spec.placeholder, spec.exact));
  }
  if (["testid", "test_id", "test-id"].includes(spec.by)) {
    elements = Array.from(root.querySelectorAll("[data-testid],[data-test-id],[data-test]"))
      .filter((element) => ["data-testid", "data-test-id", "data-test"]
        .some((attr) => matches(element.getAttribute(attr), spec.testId, true)));
  }
  if (spec.visible) elements = elements.filter(visible);
  if (spec.hasText != null) elements = elements.filter((element) => matches(element.innerText || element.textContent, spec.hasText, false));
  if (spec.notHasText != null) elements = elements.filter((element) => !matches(element.innerText || element.textContent, spec.notHasText, false));
  if (spec.first) elements = elements.slice(0, 1);
  if (spec.last) elements = elements.slice(-1);
  if (Number.isInteger(spec.nth)) {
    const index = spec.nth < 0 ? elements.length + spec.nth : spec.nth;
    elements = index >= 0 && index < elements.length ? [elements[index]] : [];
  }
  return elements.slice(0, 200).map((element) => {
    element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const nativeRect = element.getBoundingClientRect();
    const rect = stickyCardRect(element, nativeRect);
    const nameX = nativeRect.left + nativeRect.width / 2;
    const cardX = rect.left + rect.width / 2;
    const x = Math.abs(cardX - nameX) <= Math.max(24, nativeRect.width) ? nameX : cardX;
    const y = rect.top + rect.height / 2;
    return {
      selector: cssPath(element),
      tag: element.tagName.toLowerCase(),
      role: implicitRole(element),
      name: accessibleName(element).slice(0, 500),
      text: normal(element.innerText || element.textContent || "").slice(0, 1000),
      value: "value" in element ? String(element.value ?? "").slice(0, 1000) : null,
      visible: visible(element),
      enabled: !(element.disabled || element.getAttribute("aria-disabled") === "true"),
      checked: "checked" in element ? Boolean(element.checked) : null,
      href: element.href || null,
      src: element.currentSrc || element.src || null,
      point: { x, y },
    };
  });
}

async function resolveLocator(tabId, action) {
  const spec = locatorSpec(action);
  if (spec.by === "css" && !spec.selector) throw new Error("A CSS locator requires selector");
  const targets = await resolveFrameTargets(tabId, frameSelectors(action));
  const matches = [];
  for (const target of targets) {
    const result = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [target.frameId] },
      func: queryLocator,
      args: [spec],
    });
    for (const item of result?.[0]?.result || []) {
      matches.push({
        ...item,
        frame_id: target.frameId,
        frame_chain: target.chain,
        point: {
          x: Number(item.point?.x || 0) + target.offsetX,
          y: Number(item.point?.y || 0) + target.offsetY,
        },
      });
    }
  }
  return matches;
}

async function dispatchMouse(send, tabId, point, clickCount = 1, button = "left") {
  await send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y });
  await send(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button, clickCount });
  await send(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button, clickCount });
}

async function clickOrDialog(send, tabId, point, clickCount = 1) {
  const click = dispatchMouse(send, tabId, point, clickCount).then(() => ({ clicked: true }));
  click.catch(() => {});
  const dialog = (async () => {
    const deadline = Date.now() + 1500;
    while (Date.now() < deadline) {
      const current = dialogsByTab.get(tabId);
      if (current) return { dialog: current };
      await sleep(25);
    }
    return new Promise(() => {});
  })();
  return Promise.race([click, dialog]);
}

function keyDefinition(value) {
  const key = String(value || "");
  const map = {
    Enter: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 },
    Tab: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
    Escape: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
    Backspace: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 },
    Delete: { key: "Delete", code: "Delete", windowsVirtualKeyCode: 46 },
    ArrowDown: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
    ArrowUp: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 },
    ArrowLeft: { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 },
    ArrowRight: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 },
    Space: { key: " ", code: "Space", windowsVirtualKeyCode: 32 },
  };
  return map[key] || { key, code: key.length === 1 ? `Key${key.toUpperCase()}` : key, text: key.length === 1 ? key : undefined };
}

export async function pressKey(send, tabId, value, modifiers = []) {
  const definition = keyDefinition(value);
  const modifierBits = (modifiers || []).reduce((bits, item) => {
    const name = String(item).toLowerCase();
    return bits | (name === "alt" ? 1 : name === "ctrl" || name === "control" ? 2 : name === "meta" || name === "command" ? 4 : name === "shift" ? 8 : 0);
  }, 0);
  const params = { ...definition, modifiers: modifierBits };
  if (modifierBits) delete params.text;
  await send(tabId, "Input.dispatchKeyEvent", { type: "keyDown", ...params });
  await send(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...params, text: undefined });
}

async function locatorAction(action, state, hooks) {
  const operation = String(action.operation || action.op || "inspect").toLowerCase();
  const timeoutMs = Math.max(0, Math.min(Number(action.timeoutMs ?? action.timeout ?? 5000) || 5000, 60_000));
  const interactiveOperations = new Set([
    "click", "dblclick", "double_click", "fill", "type", "press",
    "check", "uncheck", "set_checked", "select_option",
  ]);
  const shouldWaitForMatch = ["wait", "wait_for"].includes(operation) || interactiveOperations.has(operation);
  let matches = [];
  const deadline = Date.now() + timeoutMs;
  do {
    matches = await resolveLocator(state.tabId, action);
    if (matches.length || !shouldWaitForMatch) break;
    await sleep(100);
  } while (Date.now() < deadline);

  if (operation === "count") return { type: "locator", operation, count: matches.length };
  if (operation === "all_text" || operation === "all_text_contents") {
    return { type: "locator", operation, count: matches.length, texts: matches.map((item) => item.text) };
  }
  if (operation === "inspect") return { type: "locator", operation, count: matches.length, matches };
  if (["wait", "wait_for"].includes(operation)) {
    if (!matches.length) throw new Error(`Locator did not resolve after ${timeoutMs}ms`);
    return { type: "locator", operation, count: matches.length, match: matches[0] };
  }
  let match = matches[0];
  if (!match) throw new Error(`Locator did not resolve for ${operation}`);

  if (["is_visible", "is_enabled", "is_checked", "inner_text", "text_content", "get_attribute", "value"].includes(operation)) {
    if (operation === "is_visible") return { type: "locator", operation, value: match.visible };
    if (operation === "is_enabled") return { type: "locator", operation, value: match.enabled };
    if (operation === "is_checked") return { type: "locator", operation, value: match.checked };
    if (operation === "inner_text" || operation === "text_content") return { type: "locator", operation, value: match.text };
    if (operation === "value") return { type: "locator", operation, value: match.value };
    const attribute = String(action.attribute || action.name || "");
    const values = await chrome.scripting.executeScript({
      target: { tabId: state.tabId, frameIds: [match.frame_id] },
      func: (selector, name) => document.querySelector(selector)?.getAttribute(name) ?? null,
      args: [match.selector, attribute],
    });
    return { type: "locator", operation, attribute, value: values?.[0]?.result ?? null };
  }

  await hooks.moveCursorToPoint(state.tabId, match.point);
  const refreshedMatches = await resolveLocator(state.tabId, action);
  const refreshedMatch = refreshedMatches[0];
  if (!refreshedMatch) {
    throw new Error(`Locator target disappeared during cursor movement for ${operation}`);
  }
  const targetMoved = refreshedMatch.frame_id !== match.frame_id
    || refreshedMatch.selector !== match.selector
    || Math.abs(refreshedMatch.point.x - match.point.x) > 1
    || Math.abs(refreshedMatch.point.y - match.point.y) > 1;
  match = refreshedMatch;
  if (targetMoved) await hooks.moveCursorToPoint(state.tabId, match.point);
  if (operation === "click" || operation === "dblclick" || operation === "double_click") {
    const outcome = await clickOrDialog(hooks.send, state.tabId, match.point, operation === "click" ? 1 : 2);
    if (outcome.dialog) return { type: "locator", operation, match, dialog_opened: outcome.dialog };
  } else if (operation === "fill" || operation === "type") {
    if (operation === "fill") {
      const fillValue = String(action.value ?? action.text ?? "");
      const filled = await chrome.scripting.executeScript({
        target: { tabId: state.tabId, frameIds: [match.frame_id] },
        func: (selector, value) => {
          const element = document.querySelector(selector);
          if (!element) throw new Error("Locator fill target disappeared");
          element.focus();
          if (element instanceof HTMLInputElement) {
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(element, value);
          } else if (element instanceof HTMLTextAreaElement) {
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(element, value);
          } else if (element.isContentEditable) {
            element.textContent = value;
          } else {
            throw new Error("Locator fill target is not editable");
          }
          element.dispatchEvent(new Event("input", { bubbles: true }));
          element.dispatchEvent(new Event("change", { bubbles: true }));
          return "value" in element ? element.value : element.textContent;
        },
        args: [match.selector, fillValue],
      });
      return { type: "locator", operation, match, value: filled?.[0]?.result ?? null };
    }
    await dispatchMouse(hooks.send, state.tabId, match.point, 1);
    await hooks.send(state.tabId, "Input.insertText", { text: String(action.value ?? action.text ?? "") });
  } else if (operation === "press") {
    await dispatchMouse(hooks.send, state.tabId, match.point, 1);
    await pressKey(hooks.send, state.tabId, action.key || action.value, action.modifiers || []);
  } else if (["check", "uncheck", "set_checked"].includes(operation)) {
    const desired = operation === "check" ? true : operation === "uncheck" ? false : Boolean(action.checked ?? action.value);
    const flipped = await chrome.scripting.executeScript({
      target: { tabId: state.tabId, frameIds: [match.frame_id] },
      func: (selector, desiredChecked) => {
        const element = document.querySelector(selector);
        if (!element) throw new Error("Locator check target is missing");
        const isBox = (node) =>
          node instanceof HTMLInputElement && (node.type === "checkbox" || node.type === "radio");
        let control = isBox(element) ? element : null;
        if (!control) {
          const label = element.closest?.("label");
          if (label && isBox(label.control)) control = label.control;
          if (!control && label) {
            const inner = label.querySelector("input[type=checkbox], input[type=radio]");
            if (isBox(inner)) control = inner;
          }
        }
        if (!control) {
          const inner = element.querySelector?.("input[type=checkbox], input[type=radio]");
          if (isBox(inner)) control = inner;
        }
        if (!control) throw new Error("Locator check target is not a checkbox or radio");
        if (control.checked !== desiredChecked) control.click();
        if (control.checked !== desiredChecked) {
          const label = control.closest("label") || (control.labels && control.labels[0]) || null;
          let fallback = null;
          if (label) {
            const nodes = label.querySelectorAll("svg, [role=img], [class*='checkbox'], [class*='icon'], span");
            for (const node of nodes) {
              if (node === control) continue;
              const box = node.getBoundingClientRect();
              if (box.width > 0 && box.height > 0) {
                fallback = node;
                break;
              }
            }
            if (!fallback) fallback = label;
          }
          if (fallback && fallback !== control) fallback.click();
        }
        if (control.checked !== desiredChecked) {
          throw new Error("Locator check did not change checked state");
        }
        return control.checked;
      },
      args: [match.selector, desired],
    });
    const checked = flipped?.[0]?.result;
    if (Boolean(checked) !== desired) {
      throw new Error("Locator check did not reach the requested checked state");
    }
    return { type: "locator", operation, match: { ...match, checked } };
  } else if (operation === "select_option") {
    if (String(match.tag || "").toLowerCase() !== "select") {
      throw new Error(
        "Locator select_option requires a native select element; open the custom combobox and choose its visible option instead"
      );
    }
    const selected = await chrome.scripting.executeScript({
      target: { tabId: state.tabId, frameIds: [match.frame_id] },
      func: (selector, value, label, index) => {
        const element = document.querySelector(selector);
        if (!(element instanceof HTMLSelectElement)) throw new Error("Locator is not a select element");
        const values = Array.isArray(value) ? value.map(String) : value == null ? null : [String(value)];
        const labels = Array.isArray(label) ? label.map(String) : label == null ? null : [String(label)];
        for (const option of element.options) option.selected = false;
        let changed = 0;
        for (const option of element.options) {
          if ((values && values.includes(option.value)) || (labels && labels.includes(option.label))) {
            option.selected = true;
            changed += 1;
          }
        }
        if (Number.isInteger(index) && element.options[index]) {
          element.options[index].selected = true;
          changed += 1;
        }
        if (!changed) throw new Error("No select option matched");
        element.dispatchEvent(new Event("input", { bubbles: true }));
        element.dispatchEvent(new Event("change", { bubbles: true }));
        return Array.from(element.selectedOptions).map((option) => ({ value: option.value, label: option.label }));
      },
      args: [match.selector, action.value ?? null, action.label ?? null, action.index ?? null],
    });
    const selectedOptions = selected?.[0]?.result;
    if (!Array.isArray(selectedOptions) || selectedOptions.length === 0) {
      throw new Error("Locator select_option did not select an option");
    }
    return { type: "locator", operation, match, selected: selectedOptions };
  } else {
    throw new Error(`Unsupported locator operation: ${operation}`);
  }
  return { type: "locator", operation, match };
}

function permittedCdpMethod(method) {
  const exact = new Set([
    "Runtime.enable", "Runtime.disable", "Runtime.getProperties", "Runtime.releaseObject",
    "Page.enable", "Page.disable", "Page.getNavigationHistory", "Page.getFrameTree", "Page.getLayoutMetrics",
    "Network.enable", "Network.disable", "Network.getResponseBody", "Network.getRequestPostData",
    "DOM.enable", "DOM.disable", "DOM.getDocument", "DOM.getOuterHTML", "DOM.getAttributes",
    "DOM.querySelector", "DOM.querySelectorAll", "DOM.performSearch", "DOM.getSearchResults",
    "DOM.discardSearchResults", "DOM.describeNode", "DOM.resolveNode", "DOM.requestNode",
    "CSS.enable", "CSS.disable", "Performance.enable", "Performance.disable", "Performance.getMetrics",
    "Log.enable", "Log.disable", "Accessibility.enable", "Accessibility.disable",
    "Accessibility.getFullAXTree", "Accessibility.getPartialAXTree",
  ]);
  return exact.has(method) || /^(Page|DOM|CSS|Accessibility|Debugger)\.get[A-Z]/.test(method);
}

export async function evaluateReadOnly(tabId, expression, send) {
  const evaluate = () => send(tabId, "Runtime.evaluate", {
    expression: String(expression || ""),
    awaitPromise: true,
    returnByValue: true,
    throwOnSideEffect: true,
    timeout: 5000,
    disableBreaks: true,
  });
  let result;
  try {
    result = await evaluate();
  } catch (error) {
    const message = String(error?.message || error);
    let transient = message === "Internal error";
    try {
      const parsed = JSON.parse(message);
      transient ||= parsed?.code === -32603 && parsed?.message === "Internal error";
    } catch {}
    if (!transient) throw error;
    await sleep(25);
    result = await evaluate();
  }
  if (result?.exceptionDetails) {
    const detail = result.exceptionDetails.exception?.description || result.exceptionDetails.text || "Read-only evaluation failed";
    throw new Error(detail);
  }
  return result?.result?.value;
}

async function cdpAction(action, state, hooks) {
  if (action.type === "cdp_send") {
    const method = String(action.method || "");
    if (!permittedCdpMethod(method) && method !== "Runtime.evaluate") {
      throw new Error(`CDP method is not permitted by the tab-scoped safe surface: ${method}`);
    }
    if (method === "Runtime.evaluate") {
      const value = await evaluateReadOnly(state.tabId, action.params?.expression || action.expression, hooks.send);
      return { type: action.type, method, result: { result: { type: typeof value, value } } };
    }
    const result = await hooks.send(state.tabId, method, action.params || {});
    return { type: action.type, method, result };
  }

  const entries = cdpEventsByTab.get(state.tabId) || [];
  if (action.afterSequence == null && action.after_sequence == null) {
    return { type: action.type, cursor: cdpSequence, events: [], has_more: false, truncated: false };
  }
  const after = Number(action.afterSequence ?? action.after_sequence ?? 0);
  const methods = Array.isArray(action.methods) ? new Set(action.methods.map(String)) : null;
  const limit = Math.max(1, Math.min(Number(action.limit || 100) || 100, 1000));
  const timeoutMs = Math.max(0, Math.min(Number(action.timeoutMs ?? action.timeout_ms ?? 0) || 0, 30_000));
  const deadline = Date.now() + timeoutMs;
  let matches = [];
  do {
    matches = (cdpEventsByTab.get(state.tabId) || []).filter((entry) => entry.sequence > after && (!methods || methods.has(entry.method)));
    if (matches.length || Date.now() >= deadline) break;
    await sleep(50);
  } while (true);
  const current = cdpEventsByTab.get(state.tabId) || [];
  const earliest = current[0]?.sequence ?? cdpSequence;
  return {
    type: action.type,
    cursor: matches.length ? matches[Math.min(matches.length, limit) - 1].sequence : cdpSequence,
    events: matches.slice(0, limit),
    has_more: matches.length > limit,
    truncated: after < earliest - 1,
  };
}

async function ensureOffscreenDocument() {
  const url = chrome.runtime.getURL("offscreen.html");
  if (chrome.runtime.getContexts) {
    const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls: [url] });
    if (contexts.length) return;
  }
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["CLIPBOARD"],
      justification: "Read and write the user-visible browser clipboard on explicit agent action",
    });
  } catch (error) {
    if (!String(error?.message || error).includes("single offscreen")) throw error;
  }
}

async function pageClipboard(message) {
  const blobToBase64 = async (blob) => {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  };
  const base64ToBlob = (base64, type) => {
    const binary = atob(String(base64 || ""));
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return new Blob([bytes], { type });
  };
  if (message.operation === "read_text") return { text: await navigator.clipboard.readText() };
  if (message.operation === "write_text") {
    await navigator.clipboard.writeText(String(message.text ?? ""));
    return { written: true, chars: String(message.text ?? "").length };
  }
  if (message.operation === "read") {
    const items = [];
    for (const item of await navigator.clipboard.read()) {
      for (const type of item.types) {
        const blob = await item.getType(type);
        const entry = { type, size: blob.size };
        if (type.startsWith("text/")) entry.text = (await blob.text()).slice(0, 200_000);
        if (message.includeData && blob.size <= 750_000) entry.base64 = await blobToBase64(blob);
        if (message.includeData && blob.size > 750_000) entry.data_omitted = "item exceeds 750KB bridge limit";
        items.push(entry);
      }
    }
    return { items };
  }
  if (message.operation === "write") {
    const representations = {};
    for (const item of message.items || []) {
      const type = String(item.type || "text/plain");
      representations[type] = item.base64 != null
        ? base64ToBlob(item.base64, type)
        : new Blob([String(item.text ?? "")], { type });
    }
    await navigator.clipboard.write([new ClipboardItem(representations)]);
    return { written: true, types: Object.keys(representations) };
  }
  throw new Error(`Unsupported clipboard operation: ${message.operation}`);
}

async function clipboardAction(action, state) {
  const operation = action.type.replace("clipboard_", "");
  const request = {
    target: "comet-control-offscreen-clipboard",
    operation,
    text: action.text,
    items: action.items,
    includeData: Boolean(action.includeData ?? action.include_data),
  };
  let response;
  try {
    const result = await chrome.scripting.executeScript({
      target: { tabId: state.tabId, frameIds: [0] },
      world: "ISOLATED",
      func: pageClipboard,
      args: [request],
    });
    response = { success: true, result: result?.[0]?.result || {} };
  } catch (pageError) {
    await ensureOffscreenDocument();
    response = await chrome.runtime.sendMessage(request);
    if (!response?.success) {
      throw new Error(response?.error || String(pageError?.message || pageError));
    }
  }
  if (!response?.success) throw new Error(response?.error || `Clipboard ${operation} failed`);
  return { type: action.type, ...response.result };
}

async function uploadFiles(action, state, hooks) {
  const selector = String(action.selector || 'input[type="file"]');
  const files = (Array.isArray(action.paths) ? action.paths : [action.path]).filter(Boolean).map(String);
  if (!files.length) throw new Error("upload_files requires path or paths");
  await hooks.send(state.tabId, "DOM.enable");
  const document = await hooks.send(state.tabId, "DOM.getDocument", { depth: -1, pierce: true });
  const node = await hooks.send(state.tabId, "DOM.querySelector", { nodeId: document.root.nodeId, selector });
  if (!node?.nodeId) throw new Error(`File input not found: ${selector}`);
  await hooks.send(state.tabId, "DOM.setFileInputFiles", { nodeId: node.nodeId, files });
  return { type: action.type, selector, file_count: files.length, files: files.map((path) => path.split("/").pop()) };
}

async function waitForDownload(downloadId, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const [item] = await chrome.downloads.search({ id: downloadId });
    if (!item) throw new Error(`Download ${downloadId} disappeared`);
    if (item.state === "complete") return item;
    if (item.state === "interrupted") throw new Error(item.error || `Download ${downloadId} was interrupted`);
    await sleep(100);
  }
  throw new Error(`Download ${downloadId} did not finish after ${timeoutMs}ms`);
}

async function downloadAction(action, state, hooks) {
  const timeoutMs = Math.max(1000, Math.min(Number(action.timeoutMs ?? action.timeout ?? 30_000) || 30_000, 180_000));
  let downloadId;
  if (action.type === "download_media") {
    const matches = await resolveLocator(state.tabId, action);
    const match = matches[0];
    const url = String(action.url || match?.href || match?.src || "");
    if (!url) throw new Error("download_media locator has no href/src URL");
    downloadId = await chrome.downloads.download({ url, saveAs: Boolean(action.saveAs ?? action.save_as) });
  } else {
    const before = new Set((await chrome.downloads.search({})).map((item) => item.id));
    const matches = await resolveLocator(state.tabId, action);
    const match = matches[0];
    if (!match) throw new Error("download_click locator did not resolve");
    await hooks.moveCursorToPoint(state.tabId, match.point);
    const clickOutcome = await clickOrDialog(hooks.send, state.tabId, match.point, 1);
    if (clickOutcome.dialog) {
      throw new Error(`Download click opened a ${clickOutcome.dialog.type} dialog; handle it and retry`);
    }
    const deadline = Date.now() + Math.min(timeoutMs, 10_000);
    while (Date.now() < deadline && downloadId == null) {
      const current = await chrome.downloads.search({});
      downloadId = current.find((item) => !before.has(item.id))?.id;
      if (downloadId == null) await sleep(100);
    }
    if (downloadId == null) throw new Error("No download started after locator click");
  }
  const item = await waitForDownload(downloadId, timeoutMs);
  return {
    type: action.type,
    id: item.id,
    filename: item.filename,
    url: item.finalUrl || item.url,
    mime: item.mime || null,
    bytes_received: item.bytesReceived,
    total_bytes: item.totalBytes,
    state: item.state,
  };
}

function collectPageAssets(limit) {
  const assets = new Map();
  const inlineSvgs = [];
  const add = (url, kind, source) => {
    try {
      const absolute = new URL(url, document.baseURI).href;
      if (!/^https?:|^data:/.test(absolute)) return;
      const existing = assets.get(absolute) || { url: absolute, kind, sources: [] };
      if (existing.kind === "other" && kind !== "other") existing.kind = kind;
      existing.sources.push(source);
      assets.set(absolute, existing);
    } catch { /* invalid URL */ }
  };
  const kindFor = (url, initiator = "") => {
    const value = String(url).toLowerCase();
    if (initiator === "img" || /\.(png|jpe?g|gif|webp|avif|svg)(\?|$)/.test(value)) return "image";
    if (initiator === "css" || /\.css(\?|$)/.test(value)) return "stylesheet";
    if (initiator === "script" || /\.m?js(\?|$)/.test(value)) return "script";
    if (/\.(woff2?|ttf|otf)(\?|$)/.test(value)) return "font";
    if (/\.(mp4|webm|mov|m3u8)(\?|$)/.test(value)) return "video";
    return "other";
  };
  for (const entry of performance.getEntriesByType("resource").slice(-limit)) {
    add(entry.name, kindFor(entry.name, entry.initiatorType), { kind: "resource" });
  }
  const selectors = [
    ["img[src],source[src],source[srcset]", "image", ["src", "srcset"]],
    ["link[rel=stylesheet][href]", "stylesheet", ["href"]],
    ["script[src]", "script", ["src"]],
    ["video[src],video[poster],source[src]", "video", ["src", "poster"]],
  ];
  for (const [selector, kind, attrs] of selectors) {
    for (const element of document.querySelectorAll(selector)) {
      for (const attr of attrs) {
        const raw = element.getAttribute(attr);
        if (!raw) continue;
        for (const part of raw.split(",")) add(part.trim().split(/\s+/)[0], kind, { kind: "attribute", property: attr });
      }
    }
  }
  for (const element of Array.from(document.querySelectorAll('[style*="url("]')).slice(0, limit)) {
    const background = getComputedStyle(element).backgroundImage || "";
    for (const match of background.matchAll(/url\(["']?([^"')]+)["']?\)/g)) add(match[1], "image", { kind: "computedStyle", property: "background-image" });
  }
  for (const [index, svg] of Array.from(document.querySelectorAll("svg")).slice(0, 50).entries()) {
    inlineSvgs.push({ id: `svg-${index + 1}`, name: svg.id || `inline-${index + 1}.svg`, markup: svg.outerHTML.slice(0, 200_000) });
  }
  return { pageUrl: location.href, assets: Array.from(assets.values()).slice(0, limit), inlineSvgs };
}

function assetName(url, index, kind) {
  try {
    const parsed = new URL(url);
    const tail = decodeURIComponent(parsed.pathname.split("/").pop() || "");
    const clean = tail.replace(/[^a-zA-Z0-9._-]+/g, "-").slice(0, 100);
    if (clean) return clean;
  } catch { /* fallback */ }
  return `${kind}-${index + 1}`;
}

async function listPageAssets(action, state) {
  const limit = Math.max(1, Math.min(Number(action.limit || 200) || 200, ASSET_LIMIT_MAX));
  const results = await chrome.scripting.executeScript({
    target: { tabId: state.tabId, allFrames: true },
    func: collectPageAssets,
    args: [limit],
  });
  const byUrl = new Map();
  const inlineSvgs = [];
  let pageUrl = null;
  for (const frame of results || []) {
    const result = frame.result || {};
    if (frame.frameId === 0) pageUrl = result.pageUrl || pageUrl;
    for (const asset of result.assets || []) {
      const existing = byUrl.get(asset.url) || { ...asset, sources: [] };
      existing.sources.push(...(asset.sources || []).map((source) => ({ ...source, frameId: frame.frameId })));
      byUrl.set(asset.url, existing);
    }
    if (action.includeInlineSvg ?? action.include_inline_svg) {
      inlineSvgs.push(...(result.inlineSvgs || []).map((svg) => ({ ...svg, frameId: frame.frameId })));
    }
  }
  const id = crypto.randomUUID();
  const assets = Array.from(byUrl.values()).slice(0, limit).map((asset, index) => ({
    id: `asset-${index + 1}`,
    kind: asset.kind,
    name: assetName(asset.url, index, asset.kind),
    url: asset.url,
    sources: asset.sources,
  }));
  const inventory = { id, tabId: state.tabId, pageUrl, assets, inlineSvgs, createdAt: Date.now() };
  assetInventories.set(id, inventory);
  while (assetInventories.size > ASSET_INVENTORY_MAX) assetInventories.delete(assetInventories.keys().next().value);
  const byKind = {};
  for (const asset of assets) byKind[asset.kind] = (byKind[asset.kind] || 0) + 1;
  return {
    type: action.type,
    id,
    page_url: pageUrl,
    summary: { total_count: assets.length, inline_svg_count: inlineSvgs.length, by_kind: byKind },
    assets,
    inline_svgs: inlineSvgs,
  };
}

async function bundlePageAssets(action) {
  const id = String(action.inventoryId ?? action.inventory_id ?? "");
  const inventory = assetInventories.get(id);
  if (!inventory) throw new Error(`Unknown or expired page asset inventory: ${id}`);
  const assetIds = Array.isArray(action.assetIds ?? action.asset_ids) ? new Set(action.assetIds ?? action.asset_ids) : null;
  const kinds = Array.isArray(action.kinds) ? new Set(action.kinds.map(String)) : null;
  const requested = inventory.assets.filter((asset) => (!assetIds || assetIds.has(asset.id)) && (!kinds || kinds.has(asset.kind)));
  const folder = `Comet Control Page Assets/${id}`;
  const assets = [];
  const failures = [];
  const startedAt = Date.now();
  for (const asset of requested) {
    try {
      const downloadId = await chrome.downloads.download({ url: asset.url, filename: `${folder}/${asset.name}`, conflictAction: "uniquify" });
      const item = await waitForDownload(downloadId, Math.min(Number(action.timeout || 60_000), 180_000));
      assets.push({ ...asset, path: item.filename, contentType: item.mime || null });
    } catch (error) {
      failures.push({ id: asset.id, name: asset.name, url: asset.url, contentType: null, reason: String(error?.message || error) });
    }
  }
  const manifest = { inventoryId: id, pageUrl: inventory.pageUrl, assets, failures };
  const manifestUrl = `data:application/json;base64,${btoa(unescape(encodeURIComponent(JSON.stringify(manifest, null, 2))))}`;
  const manifestId = await chrome.downloads.download({ url: manifestUrl, filename: `${folder}/manifest.json`, conflictAction: "overwrite" });
  const manifestItem = await waitForDownload(manifestId, 30_000);
  const directoryPath = manifestItem.filename.replace(/\/manifest\.json$/, "");
  return {
    type: action.type,
    inventory_id: id,
    directory_path: directoryPath,
    manifest_path: manifestItem.filename,
    assets,
    failures,
    summary: {
      requested_count: requested.length,
      downloaded_count: assets.length,
      failed_count: failures.length,
      elapsed_ms: Date.now() - startedAt,
    },
  };
}

export async function listUserTabs(options = {}) {
  const filter = String(options.filter || "").toLowerCase();
  const limit = Math.max(1, Math.min(Number(options.limit || 100) || 100, 500));
  const tabs = (await chrome.tabs.query({}))
    .filter((tab) => !filter || `${tab.title || ""} ${tab.url || ""}`.toLowerCase().includes(filter))
    .sort((a, b) => Number(b.lastAccessed || 0) - Number(a.lastAccessed || 0))
    .slice(0, limit)
    .map((tab) => ({
      id: tab.id,
      title: tab.title || "",
      url: tab.url || "",
      window_id: tab.windowId,
      active: Boolean(tab.active),
      pinned: Boolean(tab.pinned),
      group_id: tab.groupId,
      last_accessed: tab.lastAccessed || null,
    }));
  return tabs;
}

export async function readBrowserHistory(options = {}) {
  const limit = Math.max(1, Math.min(Number(options.limit || 50) || 50, 500));
  const queries = Array.isArray(options.queries) ? options.queries.map(String).filter(Boolean) : [];
  const text = String(options.query || queries.join(" ") || "");
  const results = await chrome.history.search({
    text,
    startTime: options.startTime ?? options.start_time ?? 0,
    ...(options.endTime ?? options.end_time ? { endTime: options.endTime ?? options.end_time } : {}),
    maxResults: limit,
  });
  return results.map((item) => ({
    id: item.id,
    url: item.url,
    title: item.title || "",
    last_visit_time: item.lastVisitTime || null,
    visit_count: item.visitCount || 0,
    typed_count: item.typedCount || 0,
  }));
}

export async function runParityAction(action, state, hooks) {
  const type = String(action.type || "");
  if (type === "locator") return { handled: true, result: await locatorAction(action, state, hooks) };
  if (type === "cdp_send" || type === "cdp_events") return { handled: true, result: await cdpAction(action, state, hooks) };
  if (type === "dialog_get") {
    await hooks.ensureAttached(state.tabId);
    return { handled: true, result: { type, dialog: dialogsByTab.get(state.tabId) || null } };
  }
  if (type === "dialog_handle") {
    await hooks.ensureAttached(state.tabId);
    const dialog = dialogsByTab.get(state.tabId);
    if (!dialog) throw new Error("No JavaScript dialog is open for this tab");
    await hooks.send(state.tabId, "Page.handleJavaScriptDialog", {
      accept: action.accept !== false && !action.dismiss,
      ...(action.promptText ?? action.prompt_text != null ? { promptText: String(action.promptText ?? action.prompt_text) } : {}),
    });
    dialogsByTab.delete(state.tabId);
    return { handled: true, result: { type, handled: true, dialog_type: dialog.type, accepted: action.accept !== false && !action.dismiss } };
  }
  if (["clipboard_read", "clipboard_read_text", "clipboard_write", "clipboard_write_text"].includes(type)) {
    return { handled: true, result: await clipboardAction(action, state) };
  }
  if (type === "upload_files") return { handled: true, result: await uploadFiles(action, state, hooks) };
  if (type === "download_click" || type === "download_media") {
    return { handled: true, result: await downloadAction(action, state, hooks) };
  }
  if (type === "page_assets_list") return { handled: true, result: await listPageAssets(action, state) };
  if (type === "page_assets_bundle") return { handled: true, result: await bundlePageAssets(action) };
  if (type === "viewport_set") {
    const width = Math.max(200, Math.min(Number(action.width) || 0, 10_000));
    const height = Math.max(200, Math.min(Number(action.height) || 0, 10_000));
    if (!width || !height) throw new Error("viewport_set requires width and height");
    await hooks.send(state.tabId, "Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: Math.max(0.1, Math.min(Number(action.deviceScaleFactor ?? action.device_scale_factor ?? 1) || 1, 10)),
      mobile: Boolean(action.mobile),
    });
    return { handled: true, result: { type, width, height, mobile: Boolean(action.mobile) } };
  }
  if (type === "viewport_reset") {
    await hooks.send(state.tabId, "Emulation.clearDeviceMetricsOverride");
    return { handled: true, result: { type, reset: true } };
  }
  return { handled: false, result: null };
}
