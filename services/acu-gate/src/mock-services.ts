import { createServer } from "node:http";
import type { Server } from "node:http";
import { once } from "node:events";
import type { PageFetch } from "./linkedin-page.ts";

type OEmbedMock = {
  status: number;
  author_url?: string;
  html?: string;
};

type MockState = {
  oembed: Map<string, OEmbedMock>;
  githubReleases: unknown[];
  pages: Map<string, { status: number; body: string }>;
};

/** startMockServices starts local oEmbed and GitHub mocks. */
export async function startMockServices() {
  const state: MockState = {
    oembed: new Map(),
    githubReleases: [
      {
        tag_name: "agent-computer-use-v1.0.0",
        draft: false,
        prerelease: false,
        published_at: "2026-01-01T00:00:00Z",
        assets: [
          {
            name: "AgentComputerUse-1.0.0.dmg",
            download_count: 42,
            browser_download_url: "https://github.com/ingpoc/agent-plugins/releases/download/agent-computer-use-v1.0.0/AgentComputerUse-1.0.0.dmg",
          },
        ],
      },
    ],
    pages: new Map(),
  };
  const server = createServer(async (req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (url.pathname === "/oembed") {
      const post = url.searchParams.get("url") || "";
      const mock = state.oembed.get(post) ?? {
        status: 404,
      };
      res.setHeader("content-type", "application/json");
      if (mock.status !== 200) {
        res.writeHead(mock.status);
        res.end(JSON.stringify({ error: "not found" }));
        return;
      }
      res.writeHead(200);
      res.end(
        JSON.stringify({
          author_name: "Test",
          author_url: mock.author_url,
          html: mock.html,
        }),
      );
      return;
    }
    if (url.pathname === "/repos/ingpoc/agent-plugins/releases") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(state.githubReleases));
      return;
    }
    const page = state.pages.get(url.pathname);
    if (page) {
      res.writeHead(page.status, { "content-type": "text/html" });
      res.end(page.body);
      return;
    }
    res.writeHead(404);
    res.end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("mock_listen_failed");
  const base = `http://127.0.0.1:${address.port}`;
  const pageFetch: PageFetch = async (target) => {
    const parsed = new URL(target);
    const page = state.pages.get(parsed.pathname);
    if (page) return page;
    return { status: 404, body: "" };
  };
  return {
    base,
    state,
    env: {
      OEMBED_BASE: `${base}/oembed`,
      GITHUB_API_BASE: base,
    },
    pageFetch,
    close: () => closeServer(server),
  };
}

export async function closeServer(server: Server) {
  await new Promise<void>((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
}

/** seedGoodPost registers a passing oEmbed response for tests. */
export function seedGoodPost(
  state: MockState,
  postUrl: string,
  handle: string,
  text: string,
) {
  state.oembed.set(postUrl, {
    status: 200,
    author_url: `https://x.com/${handle}`,
    html: `<p>${text}</p>`,
  });
}
