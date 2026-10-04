/**
 * CloudStream WebDAV Bridge - Cloudflare Worker Edge Router
 *
 * Edge routing layer bridging CX File Explorer / WebDAV clients to dynamic
 * Google Cloud Shell tunnels with 0 bytes video proxying (HTTP 302 Found).
 */

export interface Env {
  RENDER_HUB_URL?: string;
  [key: string]: unknown;
}

export interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
  [key: string]: unknown;
}

interface CachedTunnel {
  tunnel_url: string;
  cachedAt: number;
}

const DEFAULT_HUB_URL = "https://cloud-stream-bridge.onrender.com";
const CACHE_TTL_MS = 10_000; // 10s in-memory cache

// In-memory module state cache for active Cloud Shell tunnels
const tunnelCache = new Map<string, CachedTunnel>();

/**
 * Standard CORS headers for cross-origin WebDAV and HTTP requests
 */
function getCorsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS, PROPFIND, PROPPATCH, MKCOL",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
  };
}

/**
 * Query Render Hub status endpoint for user_id with 10s module state cache.
 */
async function getActiveTunnel(hubUrl: string, userId: string): Promise<string | null> {
  const now = Date.now();
  const cached = tunnelCache.get(userId);
  if (cached && now - cached.cachedAt < CACHE_TTL_MS) {
    return cached.tunnel_url;
  }

  const cleanHubUrl = hubUrl.replace(/\/+$/, "");
  const statusEndpoint = `${cleanHubUrl}/api/status/${encodeURIComponent(userId)}`;

  try {
    const res = await fetch(statusEndpoint, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "User-Agent": "CloudStream-Worker/1.0",
      },
      signal: AbortSignal.timeout(5000),
    });

    if (!res.ok) {
      tunnelCache.delete(userId);
      return null;
    }

    const data = (await res.json()) as {
      active?: boolean;
      tunnel_url?: string | null;
      status?: string;
    };

    if (data && data.active && data.tunnel_url) {
      const cleanTunnel = data.tunnel_url.trim().replace(/\/+$/, "");
      tunnelCache.set(userId, {
        tunnel_url: cleanTunnel,
        cachedAt: now,
      });
      return cleanTunnel;
    }

    tunnelCache.delete(userId);
    return null;
  } catch (_err) {
    if (cached && now - cached.cachedAt < CACHE_TTL_MS) {
      return cached.tunnel_url;
    }
    return null;
  }
}

/**
 * Generate standard RFC 4918 WebDAV XML error response for dormant/offline tunnels.
 */
function makeDormantResponse(userId: string, reason = "dormant or offline"): Response {
  const xml = `<?xml version="1.0" encoding="utf-8"?>
<D:error xmlns:D="DAV:">
  <D:response-description>Cloud Shell session is ${reason} for user '${userId}'. Run your command in Google Cloud Shell to activate.</D:response-description>
</D:error>`;

  return new Response(xml, {
    status: 503,
    statusText: "Service Unavailable",
    headers: {
      "Content-Type": "application/xml; charset=utf-8",
      DAV: "1",
      "MS-Author-Via": "DAV",
      "Retry-After": "10",
      ...getCorsHeaders(),
    },
  });
}

/**
 * Rewrite a single URL from an upstream WebDAV XML href.
 * - Rewrites temporary tunnel URLs to permanent hub paths (/dav/{user_id}/...).
 * - Rewrites relative /dav/... paths to /dav/{user_id}/...
 */
function rewriteSingleHref(url: string, tunnelUrl: string, userId: string): string {
  const stripped = url.trim();
  const cleanTunnel = tunnelUrl.trim().replace(/\/+$/, "");
  const hubPrefix = `/dav/${userId}`;

  // Candidate tunnel base URLs (support https and http)
  const tunnels = [cleanTunnel];
  if (cleanTunnel.startsWith("https://")) {
    tunnels.push("http://" + cleanTunnel.slice(8));
  } else if (cleanTunnel.startsWith("http://")) {
    tunnels.push("https://" + cleanTunnel.slice(7));
  }

  for (const t of tunnels) {
    if (stripped.startsWith(t)) {
      let remainder = stripped.slice(t.length);
      if (remainder.startsWith("/dav")) {
        remainder = remainder.slice(4);
      }
      if (!remainder.startsWith("/")) {
        remainder = remainder ? "/" + remainder : "/";
      }
      return `${hubPrefix}${remainder}`;
    }
  }

  // Upstream relative paths starting with /dav/
  if (stripped.startsWith("/dav/") && !stripped.startsWith(`${hubPrefix}/`)) {
    const remainder = stripped.slice("/dav/".length);
    return `${hubPrefix}/${remainder}`;
  } else if (stripped === "/dav") {
    return `${hubPrefix}/`;
  }

  return url;
}

/**
 * Rewrite all <D:href> and <href> tags in XML responses to point to /dav/{user_id}/...
 */
function rewriteWebDavHrefs(xmlContent: string, tunnelUrl: string, userId: string): string {
  return xmlContent.replace(
    /(<([A-Za-z0-9_-]+:)?href\b[^>]*>)([\s\S]*?)(<\/[A-Za-z0-9_-]+:href>|<\/href>)/gi,
    (_match, openTag, _prefix, rawUrl, closeTag) => {
      const rewritten = rewriteSingleHref(rawUrl, tunnelUrl, userId);
      return `${openTag}${rewritten}${closeTag}`;
    }
  );
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const pathname = url.pathname;
    const method = request.method.toUpperCase();
    const hubUrl = (env?.RENDER_HUB_URL || DEFAULT_HUB_URL).trim();

    // 1. CORS Preflight for non-DAV endpoints
    if (method === "OPTIONS" && !pathname.startsWith("/dav/")) {
      return new Response(null, {
        status: 204,
        headers: {
          ...getCorsHeaders(),
          DAV: "1",
          "MS-Author-Via": "DAV",
        },
      });
    }

    // 2. Health check and root status
    if (pathname === "/" || pathname === "/health") {
      return new Response(
        JSON.stringify({
          status: "ok",
          service: "cloudstream-cloudflare-worker",
          render_hub_url: hubUrl,
          cached_tunnels_count: tunnelCache.size,
          timestamp: new Date().toISOString(),
        }),
        {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            ...getCorsHeaders(),
          },
        }
      );
    }

    // 3. WebDAV router: /dav/{user_id} and /dav/{user_id}/{path:.*}
    const davMatch = pathname.match(/^\/dav\/([^/]+)(?:\/(.*))?$/);
    if (davMatch) {
      const userId = decodeURIComponent(davMatch[1]).trim();
      const rawPath = davMatch[2] !== undefined ? davMatch[2] : "";

      // Query Render Hub status with 10s in-memory module cache
      const tunnelUrl = await getActiveTunnel(hubUrl, userId);

      // If dormant or offline, return HTTP 503 with standard RFC 4918 WebDAV XML
      if (!tunnelUrl) {
        return makeDormantResponse(userId);
      }

      const cleanPath = rawPath.replace(/^\/+/, "");
      const encodedPath = cleanPath
        ? cleanPath
            .split("/")
            .map((seg) => encodeURIComponent(decodeURIComponent(seg)))
            .join("/")
        : "";

      // 4. GET / HEAD: HTTP 302 Found redirect (0 bytes video proxying)
      if (method === "GET" || method === "HEAD") {
        let redirectUrl = encodedPath
          ? `${tunnelUrl}/dav/${encodedPath}`
          : `${tunnelUrl}/dav/`;

        if (url.search) {
          redirectUrl += url.search;
        }

        return new Response(null, {
          status: 302,
          statusText: "Found",
          headers: {
            Location: redirectUrl,
            DAV: "1",
            "MS-Author-Via": "DAV",
            ...getCorsHeaders(),
          },
        });
      }

      // 5. PROPFIND, OPTIONS, PROPPATCH, MKCOL, DELETE: Reverse-proxy metadata
      let targetUrl = encodedPath
        ? `${tunnelUrl}/dav/${encodedPath}`
        : `${tunnelUrl}/dav/`;

      if (url.search) {
        targetUrl += url.search;
      }

      try {
        const fwdHeaders = new Headers(request.headers);
        fwdHeaders.delete("host");

        const reqBody = await request.arrayBuffer();

        const upstreamResp = await fetch(targetUrl, {
          method,
          headers: fwdHeaders,
          body: reqBody && reqBody.byteLength > 0 ? reqBody : undefined,
        });

        const status = upstreamResp.status;

        // Check if upstream returned a dead tunnel error page (502, 503, 504, 520-530 Cloudflare tunnel errors)
        if (status >= 500) {
          const errText = await upstreamResp.text();
          const contentType = upstreamResp.headers.get("content-type") || "";
          if (
            errText.toLowerCase().includes("<html") ||
            contentType.includes("text/html") ||
            status === 502 ||
            status === 503 ||
            status === 504 ||
            status === 530 ||
            (status >= 520 && status <= 530)
          ) {
            tunnelCache.delete(userId);
            return makeDormantResponse(userId, "unreachable or disconnected");
          }
          return new Response(errText, {
            status,
            headers: upstreamResp.headers,
          });
        }

        const respHeaders = new Headers(upstreamResp.headers);
        respHeaders.set("DAV", "1");
        respHeaders.set("MS-Author-Via", "DAV");
        for (const [k, v] of Object.entries(getCorsHeaders())) {
          respHeaders.set(k, v);
        }

        if (respHeaders.has("location")) {
          const loc = respHeaders.get("location")!;
          respHeaders.set("Location", rewriteSingleHref(loc, tunnelUrl, userId));
        }

        // Rewrite <D:href> or <href> for 207 Multi-Status or 200 XML
        if (status === 207 || status === 200) {
          const contentType = upstreamResp.headers.get("content-type") || "";
          const text = await upstreamResp.text();
          if (
            contentType.includes("xml") ||
            text.trim().startsWith("<?xml") ||
            text.includes("<")
          ) {
            const rewritten = rewriteWebDavHrefs(text, tunnelUrl, userId);
            return new Response(rewritten, {
              status,
              statusText: upstreamResp.statusText,
              headers: respHeaders,
            });
          }
          return new Response(text, {
            status,
            statusText: upstreamResp.statusText,
            headers: respHeaders,
          });
        }

        const responseBody =
          status === 204 || status === 205 || status === 304
            ? null
            : await upstreamResp.arrayBuffer();

        return new Response(responseBody, {
          status,
          statusText: upstreamResp.statusText,
          headers: respHeaders,
        });
      } catch (_fetchErr) {
        tunnelCache.delete(userId);
        return makeDormantResponse(userId, "unreachable or disconnected");
      }
    }

    // 6. Unmatched routes -> 404 Not Found
    return new Response(
      JSON.stringify({
        error: "Not Found",
        message: `Endpoint '${pathname}' not found. Supported: /health, /dav/{user_id}`,
      }),
      {
        status: 404,
        headers: {
          "Content-Type": "application/json",
          ...getCorsHeaders(),
        },
      }
    );
  },
};
