import type { IncomingMessage } from "node:http";

/** clientIp resolves the client address for rate limits and audit. */
export function clientIp(req: IncomingMessage, trustProxyHops: number) {
  if (trustProxyHops > 0) {
    const header = req.headers["x-forwarded-for"];
    const raw = Array.isArray(header) ? header[0] : header;
    if (raw) {
      const parts = raw.split(",").map((part) => part.trim()).filter(Boolean);
      if (parts.length >= trustProxyHops)
        return parts[parts.length - trustProxyHops] || parts[0]!;
    }
  }
  return req.socket.remoteAddress || "unknown";
}
