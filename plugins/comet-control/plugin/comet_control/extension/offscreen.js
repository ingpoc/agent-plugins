async function blobToBase64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function base64ToBlob(base64, type) {
  const binary = atob(String(base64 || ""));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type });
}

async function handleClipboard(message) {
  const operation = message.operation;
  if (operation === "read_text") return { text: await navigator.clipboard.readText() };
  if (operation === "write_text") {
    await navigator.clipboard.writeText(String(message.text ?? ""));
    return { written: true, chars: String(message.text ?? "").length };
  }
  if (operation === "read") {
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
  if (operation === "write") {
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
  throw new Error(`Unsupported clipboard operation: ${operation}`);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "comet-control-offscreen-clipboard") return false;
  handleClipboard(message).then(
    (result) => sendResponse({ success: true, result }),
    (error) => sendResponse({ success: false, error: String(error?.message || error) }),
  );
  return true;
});
