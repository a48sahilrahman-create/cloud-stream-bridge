/**
 * CloudStream WebDAV Bridge - Cloudflare Worker Edge Router & Standalone WebDAV Server
 *
 * Implements:
 * 1. RFC 4918 Standalone WebDAV server (OPTIONS, PROPFIND, GET, HEAD, DELETE)
 * 2. Edge REST Control API (/api/mount, /api/mounts, /api/unmount-all, /health)
 * 3. 0-Byte Video Streaming Direct Redirection (HTTP 302 Found)
 * 4. Two-Tier Stream Probing Shield with 100 GiB Synthetic Floor
 * 5. Backward-compatible dynamic tunnel reverse-proxy fallback
 */

import type {
  StreamMount,
  UserMountsRecord,
  MountManagerEnv,
} from "./mount_manager.ts";
import {
  getMountsRecord,
  addMount,
  removeMount,
  clearMounts,
  findMount,
} from "./mount_manager.ts";

export interface Env extends MountManagerEnv {
  RENDER_HUB_URL?: string;
  MOUNTS_KV?: KVNamespace;
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
const CACHE_TTL_MS = 10_000; // 10s in-memory cache for Cloud Shell tunnels
const SYNTHETIC_FLOOR_BYTES = 107_374_182_400; // 100 GiB Synthetic Floor

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
    "Access-Control-Expose-Headers": "Location, Content-Range, Accept-Ranges, Content-Length, DAV",
  };
}

/**
 * Escape XML special characters for RFC 4918 responses
 */
function escapeXml(unsafe: string): string {
  return unsafe
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

/**
 * Format RFC 1123 / WebDAV date string
 */
function formatWebDavDate(timestampMs?: number): string {
  const d = timestampMs && !isNaN(timestampMs) ? new Date(timestampMs) : new Date();
  return d.toUTCString();
}

/**
 * Infer MIME content-type from virtual filename
 */
function inferContentType(filename: string): string {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".mkv")) return "video/x-matroska";
  if (lower.endsWith(".mp4") || lower.endsWith(".m4v")) return "video/mp4";
  if (lower.endsWith(".avi")) return "video/x-msvideo";
  if (lower.endsWith(".webm")) return "video/webm";
  if (lower.endsWith(".mov")) return "video/quicktime";
  if (lower.endsWith(".ts")) return "video/mp2t";
  if (lower.endsWith(".flv")) return "video/x-flv";
  return "video/mp4";
}

/**
 * Detect if upstream URL belongs to Google CDN / Drive / Photos / YouTube ecosystems
 * where direct client 302 redirects fail due to IP-binding, ephemeral auth tokens,
 * or aggressive bot-checks, requiring Worker edge-proxying.
 */
export function isGoogleCdn(urlStr: string): boolean {
  if (!urlStr || typeof urlStr !== "string") {
    return false;
  }
  try {
    const parsed = new URL(urlStr);
    const host = parsed.hostname.toLowerCase();
    if (
      host === "video-downloads.googleusercontent.com" ||
      host === "googleusercontent.com" ||
      host.endsWith(".googleusercontent.com") ||
      host === "drive.google.com" ||
      host.endsWith(".drive.google.com") ||
      host === "photos.google.com" ||
      host.endsWith(".photos.google.com") ||
      host === "googlevideo.com" ||
      host.endsWith(".googlevideo.com")
    ) {
      return true;
    }
  } catch {
    const lower = urlStr.toLowerCase();
    if (
      lower.includes("video-downloads.googleusercontent.com") ||
      lower.includes("googleusercontent.com") ||
      lower.includes("drive.google.com") ||
      lower.includes("photos.google.com") ||
      lower.includes("googlevideo.com")
    ) {
      return true;
    }
  }
  return false;
}

/**
 * Tier 2 Edge Fallback Range Probe (0-8191 with 4s timeout) or 100 GiB synthetic floor
 */
async function probeStream(
  streamUrl: string,
  customHeaders?: Record<string, string>
): Promise<{ sizeBytes: number; contentType: string; detectedFilename?: string }> {
  let sizeBytes = 0;
  let contentType = "";
  let detectedFilename: string | undefined;

  try {
    const probeHeaders: Record<string, string> = {
      Range: "bytes=0-8191",
      "User-Agent": "CloudStream-Worker-Probe/1.0",
      Accept: "*/*",
      ...(customHeaders || {}),
    };

    const res = await fetch(streamUrl, {
      method: "GET",
      headers: probeHeaders,
      redirect: "follow",
      signal: AbortSignal.timeout(4000),
    });

    const cr = res.headers.get("content-range");
    if (cr) {
      const match = cr.match(/\/(\d+)$/);
      if (match) {
        const val = parseInt(match[1], 10);
        if (val > 0) sizeBytes = val;
      }
    }

    if (!sizeBytes) {
      const cl = res.headers.get("content-length");
      if (cl) {
        const parsedCl = parseInt(cl, 10);
        if (parsedCl > 8192) sizeBytes = parsedCl;
      }
    }

    const ct = res.headers.get("content-type");
    if (ct && (ct.includes("video") || ct.includes("application/octet-stream"))) {
      contentType = ct.split(";")[0].trim();
    }

    const cd = res.headers.get("content-disposition");
    if (cd) {
      const fnMatch = cd.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)["']?/i);
      if (fnMatch && fnMatch[1]) {
        detectedFilename = decodeURIComponent(fnMatch[1].trim());
      }
    }
  } catch (_probeErr) {
    // Probe timeout or network error: will fall back to synthetic floor
  }

  if (sizeBytes > 0) {
    warmStreamStorage(streamUrl, sizeBytes);
  }

  return {
    sizeBytes: sizeBytes > 0 ? sizeBytes : SYNTHETIC_FLOOR_BYTES,
    contentType: contentType || "video/mp4",
    detectedFilename,
  };
}

/**
 * Asynchronous storage pre-warming helper.
 * Non-blockingly fetches the head 32KB (MKV EBML header) and tail 64KB (Cues seek table)
 * into origin NVMe/RAM cache so first-frame rendering and seeking are instant.
 */
export function warmStreamStorage(
  upstreamUrl: string,
  sizeBytes: number,
  ctx?: ExecutionContext
): void {
  if (!upstreamUrl || typeof upstreamUrl !== "string" || !sizeBytes || sizeBytes <= 0) {
    return;
  }

  try {
    const headPromise = fetch(upstreamUrl, {
      headers: {
        Range: "bytes=0-32767",
        "User-Agent": "CloudStream-Edge-Warmer/1.0",
      },
    }).catch(() => {});

    let tailPromise: Promise<unknown> | undefined;
    if (sizeBytes > 65536) {
      tailPromise = fetch(upstreamUrl, {
        headers: {
          Range: `bytes=${sizeBytes - 65536}-${sizeBytes - 1}`,
          "User-Agent": "CloudStream-Edge-Warmer/1.0",
        },
      }).catch(() => {});
    }

    if (ctx && typeof ctx.waitUntil === "function") {
      ctx.waitUntil(headPromise);
      if (tailPromise) {
        ctx.waitUntil(tailPromise);
      }
    }
  } catch (_err) {
    // Non-blocking fire-and-forget
  }
}

function createRangeStream(
  upstreamStream: ReadableStream<Uint8Array>,
  skipBytes: number,
  takeBytes: number // -1 for unlimited
): ReadableStream<Uint8Array> {
  let skipped = 0;
  let taken = 0;
  const reader = upstreamStream.getReader();

  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (takeBytes >= 0 && taken >= takeBytes) {
        controller.close();
        try { await reader.cancel(); } catch (_) {}
        return;
      }

      while (true) {
        const { done, value } = await reader.read();
        if (done || !value) {
          controller.close();
          return;
        }

        if (skipped < skipBytes) {
          const neededSkip = skipBytes - skipped;
          if (value.byteLength <= neededSkip) {
            skipped += value.byteLength;
            continue;
          } else {
            const remaining = value.subarray(neededSkip);
            skipped = skipBytes;
            if (takeBytes >= 0) {
              const neededTake = takeBytes - taken;
              if (remaining.byteLength <= neededTake) {
                taken += remaining.byteLength;
                controller.enqueue(remaining);
                if (taken >= takeBytes) {
                  controller.close();
                  try { await reader.cancel(); } catch (_) {}
                }
              } else {
                controller.enqueue(remaining.subarray(0, neededTake));
                taken = takeBytes;
                controller.close();
                try { await reader.cancel(); } catch (_) {}
              }
            } else {
              controller.enqueue(remaining);
            }
            return;
          }
        }

        if (takeBytes >= 0) {
          const neededTake = takeBytes - taken;
          if (value.byteLength <= neededTake) {
            taken += value.byteLength;
            controller.enqueue(value);
            if (taken >= takeBytes) {
              controller.close();
              try { await reader.cancel(); } catch (_) {}
            }
          } else {
            controller.enqueue(value.subarray(0, neededTake));
            taken = takeBytes;
            controller.close();
            try { await reader.cancel(); } catch (_) {}
          }
        } else {
          controller.enqueue(value);
        }
        return;
      }
    },
    async cancel(reason) {
      try { await reader.cancel(reason); } catch (_) {}
    }
  });
}

/**
 * Render RFC 4918 XML element for a single mounted virtual file
 */
function renderMountFileXml(userId: string, mount: StreamMount): string {
  const encodedFilename = mount.filename
    .split("/")
    .map((seg) => encodeURIComponent(seg))
    .join("/");

  const contentType =
    mount.content_type && mount.content_type !== "application/octet-stream"
      ? mount.content_type
      : inferContentType(mount.filename);

  return `  <D:response>
    <D:href>/dav/${encodeURIComponent(userId)}/${encodedFilename}</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>${escapeXml(mount.filename)}</D:displayname>
        <D:resourcetype/>
        <D:getcontentlength>${mount.size_bytes}</D:getcontentlength>
        <D:getcontenttype>${escapeXml(contentType)}</D:getcontenttype>
        <D:getetag>${escapeXml(mount.etag)}</D:getetag>
        <D:getlastmodified>${formatWebDavDate(mount.created_at)}</D:getlastmodified>
        <D:supportedlock>
          <D:lockentry>
            <D:lockscope><D:exclusive/></D:lockscope>
            <D:locktype><D:write/></D:locktype>
          </D:lockentry>
        </D:supportedlock>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>\n`;
}

/**
 * Render RFC 4918 207 Multi-Status XML payload for PROPFIND
 */
function buildWebDavMultiStatus(
  userId: string,
  record: UserMountsRecord,
  depth: string,
  targetFilename?: string
): string {
  let xml = `<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">\n`;

  // Depth: 0 on a specific child file
  if (targetFilename) {
    const cleanTarget = targetFilename.toLowerCase();
    const mount = record.mounts.find(
      (m) =>
        m.filename.toLowerCase() === cleanTarget ||
        encodeURIComponent(m.filename).toLowerCase() === cleanTarget ||
        m.id === targetFilename
    );
    if (mount) {
      xml += renderMountFileXml(userId, mount);
    }
    xml += `</D:multistatus>`;
    return xml;
  }

  // Root collection response
  xml += `  <D:response>
    <D:href>/dav/${encodeURIComponent(userId)}/</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>${escapeXml(userId)}</D:displayname>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:getlastmodified>${formatWebDavDate(record.updated_at)}</D:getlastmodified>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>\n`;

  // Depth: 1 (or default) includes all mounted children
  if (depth !== "0") {
    for (const mount of record.mounts) {
      xml += renderMountFileXml(userId, mount);
    }
  }

  xml += `</D:multistatus>`;
  return xml;
}

/**
 * Query Render Hub status endpoint for user_id with 10s module state cache.
 */
async function getActiveTunnel(
  hubUrl: string,
  userId: string,
  forceRefresh = false
): Promise<string | null> {
  const now = Date.now();
  const cached = tunnelCache.get(userId);
  if (!forceRefresh && cached && now - cached.cachedAt < CACHE_TTL_MS) {
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
    if (!forceRefresh && cached && now - cached.cachedAt < CACHE_TTL_MS) {
      return cached.tunnel_url;
    }
    return null;
  }
}

/**
 * Generate standard RFC 4918 WebDAV XML error response for dormant/offline sessions.
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
 */
function rewriteSingleHref(url: string, tunnelUrl: string, userId: string): string {
  const stripped = url.trim();
  const cleanTunnel = tunnelUrl.trim().replace(/\/+$/, "");
  const hubPrefix = `/dav/${userId}`;

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

/**
 * Detect dead tunnel error statuses (502, 503, 504, 520-530, or HTML error pages).
 */
function isDeadTunnelError(status: number, contentType = "", bodyText = ""): boolean {
  if (status === 502 || status === 503 || status === 504 || (status >= 520 && status <= 530)) {
    return true;
  }
  if (status >= 500 && (contentType.includes("text/html") || bodyText.toLowerCase().includes("<html"))) {
    return true;
  }
  return false;
}

/**
 * Build standard HTTP 302 Found redirect for video/media streaming (0 bytes video proxying).
 */
function makeRedirectResponse(tunnelUrl: string, encodedPath: string, search: string): Response {
  let redirectUrl = encodedPath ? `${tunnelUrl}/dav/${encodedPath}` : `${tunnelUrl}/dav/`;
  if (search) {
    redirectUrl += search;
  }
  return new Response(null, {
    status: 302,
    statusText: "Found",
    headers: {
      Location: redirectUrl,
      DAV: "1",
      "MS-Author-Via": "DAV",
      "Accept-Ranges": "bytes",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Expose-Headers": "Location, Content-Range, Accept-Ranges",
      "Cache-Control": "private, max-age=1800, stale-while-revalidate=300",
      "Vary": "Range",
      "Keep-Alive": "timeout=60, max=1000",
      ...getCorsHeaders(),
    },
  });
}

/**
 * Format and rewrite successful upstream response for WebDAV client.
 */
async function formatUpstreamResponse(
  upstreamResp: Response,
  tunnelUrl: string,
  userId: string
): Promise<Response> {
  const status = upstreamResp.status;
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
          DAV: "1, 2",
          "MS-Author-Via": "DAV",
        },
      });
    }

    // 2. Health check and root status
    if (pathname === "/" || pathname === "/health") {
      const kvBound = Boolean(env?.MOUNTS_KV);
      return new Response(
        JSON.stringify({
          status: "ok",
          engine: "serverless-edge",
          kv_bound: kvBound,
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

    // =========================================================================
    // REST API Handlers
    // =========================================================================

    // POST /api/mount/:userId
    const mountMatch = pathname.match(/^\/api\/mount\/([^/]+)\/?$/);
    if (method === "POST" && mountMatch) {
      const userId = decodeURIComponent(mountMatch[1]).trim();
      let body: {
        url?: string;
        filename?: string;
        title?: string;
        size_bytes?: number;
        content_type?: string;
        custom_headers?: Record<string, string>;
      };

      try {
        body = (await request.json()) as typeof body;
      } catch {
        return new Response(
          JSON.stringify({ error: "Invalid JSON payload" }),
          {
            status: 400,
            headers: {
              "Content-Type": "application/json",
              ...getCorsHeaders(),
            },
          }
        );
      }

      if (!body || !body.url || typeof body.url !== "string") {
        return new Response(
          JSON.stringify({ error: "Missing required parameter: 'url'" }),
          {
            status: 400,
            headers: {
              "Content-Type": "application/json",
              ...getCorsHeaders(),
            },
          }
        );
      }

      const streamUrl = body.url.trim();
      let filename = body.filename?.trim();
      let sizeBytes = typeof body.size_bytes === "number" ? body.size_bytes : 0;
      let contentType = body.content_type?.trim();

      // If size_bytes is missing or 0, trigger Tier 2 probe or synthetic floor
      if (!sizeBytes || sizeBytes <= 0) {
        const probed = await probeStream(streamUrl, body.custom_headers);
        sizeBytes = probed.sizeBytes;
        if (!contentType && probed.contentType) {
          contentType = probed.contentType;
        }
        if (!filename && probed.detectedFilename) {
          filename = probed.detectedFilename;
        }
      }

      // Fallback filename extraction from URL
      if (!filename) {
        try {
          const parsed = new URL(streamUrl);
          const segments = parsed.pathname.split("/").filter(Boolean);
          const last = segments.pop();
          if (last) {
            filename = decodeURIComponent(last);
          }
        } catch {}
        if (!filename) {
          filename = "stream.mkv";
        }
      }

      // Sanitize virtual filename
      filename = filename.replace(/^[/\\]+/, "").trim() || "stream.mkv";
      if (!filename.includes(".")) {
        filename += ".mkv";
      }

      if (!contentType || contentType === "application/octet-stream") {
        contentType = inferContentType(filename);
      }

      const mountId = `mount_${Math.random().toString(36).substring(2, 10)}`;
      const etag = `W/"${mountId.replace("mount_", "")}-${sizeBytes}"`;

      const mount: StreamMount = {
        id: mountId,
        filename,
        title: body.title?.trim() || filename,
        upstream_url: streamUrl,
        size_bytes: sizeBytes,
        content_type: contentType,
        created_at: Date.now(),
        etag,
        custom_headers: body.custom_headers,
      };

      await addMount(env, userId, mount);

      // Asynchronously pre-warm storage for the newly mounted stream
      warmStreamStorage(mount.upstream_url, mount.size_bytes, ctx);

      return new Response(
        JSON.stringify({
          status: "mounted",
          mount,
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

    // GET /api/mounts/:userId
    const mountsMatch = pathname.match(/^\/api\/mounts\/([^/]+)\/?$/);
    if (method === "GET" && mountsMatch) {
      const userId = decodeURIComponent(mountsMatch[1]).trim();
      const record = await getMountsRecord(env, userId);

      return new Response(
        JSON.stringify({
          user_id: userId,
          mounts: record ? record.mounts : [],
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

    // DELETE /api/mounts/:userId/:filename
    const unmountMatch = pathname.match(/^\/api\/mounts\/([^/]+)\/(.+)$/);
    if (method === "DELETE" && unmountMatch) {
      const userId = decodeURIComponent(unmountMatch[1]).trim();
      const filename = decodeURIComponent(unmountMatch[2]);
      const unmounted = await removeMount(env, userId, filename);

      return new Response(
        JSON.stringify({
          status: unmounted ? "unmounted" : "not_found",
          user_id: userId,
          filename,
        }),
        {
          status: unmounted ? 200 : 404,
          headers: {
            "Content-Type": "application/json",
            ...getCorsHeaders(),
          },
        }
      );
    }

    // POST /api/unmount-all/:userId
    const unmountAllMatch = pathname.match(/^\/api\/unmount-all\/([^/]+)\/?$/);
    if (method === "POST" && unmountAllMatch) {
      const userId = decodeURIComponent(unmountAllMatch[1]).trim();
      const clearedCount = await clearMounts(env, userId);

      return new Response(
        JSON.stringify({
          status: "cleared",
          user_id: userId,
          mounts_cleared: clearedCount,
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

    // =========================================================================
    // RFC 4918 WebDAV Handlers (/dav/{userId} and /dav/{userId}/{path:.*})
    // =========================================================================

    const davMatch = pathname.match(/^\/dav\/([^/]+)(?:\/(.*))?$/);
    if (davMatch) {
      const userId = decodeURIComponent(davMatch[1]).trim();
      const rawPath = davMatch[2] !== undefined ? davMatch[2] : "";
      const cleanPath = rawPath.replace(/^\/+/, "");
      const decodedTargetFilename = cleanPath ? decodeURIComponent(cleanPath) : "";

      // 1. OPTIONS /dav/:userId/
      if (method === "OPTIONS") {
        return new Response(null, {
          status: 200,
          headers: {
            DAV: "1, 2",
            "MS-Author-Via": "DAV",
            Allow: "OPTIONS, GET, HEAD, PROPFIND, DELETE, PROPPATCH, MKCOL",
            "Accept-Ranges": "bytes",
            ...getCorsHeaders(),
          },
        });
      }

      // Check standalone KV/memory mounts
      const userRecord = await getMountsRecord(env, userId);

      // 2. PROPFIND /dav/:userId/ (Standalone WebDAV Directory Listing)
      if (method === "PROPFIND") {
        if (userRecord && userRecord.mounts.length > 0) {
          const depth = request.headers.get("Depth") || "1";
          if (!cleanPath) {
            // Root collection listing (Depth: 0 or 1)
            const xml = buildWebDavMultiStatus(userId, userRecord, depth);
            return new Response(xml, {
              status: 207,
              statusText: "Multi-Status",
              headers: {
                "Content-Type": "application/xml; charset=utf-8",
                DAV: "1, 2",
                "MS-Author-Via": "DAV",
                ...getCorsHeaders(),
              },
            });
          } else {
            // PROPFIND on a specific mounted file
            const found = userRecord.mounts.find(
              (m) =>
                m.filename.toLowerCase() === decodedTargetFilename.toLowerCase() ||
                encodeURIComponent(m.filename).toLowerCase() === decodedTargetFilename.toLowerCase() ||
                m.id === decodedTargetFilename
            );
            if (found) {
              const xml = buildWebDavMultiStatus(userId, userRecord, depth, decodedTargetFilename);
              return new Response(xml, {
                status: 207,
                statusText: "Multi-Status",
                headers: {
                  "Content-Type": "application/xml; charset=utf-8",
                  DAV: "1, 2",
                  "MS-Author-Via": "DAV",
                  ...getCorsHeaders(),
                },
              });
            }
          }
        }
      }

      // 3a. HEAD /dav/:userId/:filename (Synthetic WebDAV Stream Probe - RFC 4918)
      // Serves media attributes directly from KV without upstream 302/403 SigV4 failures
      if (method === "HEAD" && cleanPath) {
        const mount = await findMount(env, userId, decodedTargetFilename);
        if (mount) {
          const totalBytes = mount.size_bytes > 0 ? mount.size_bytes : SYNTHETIC_FLOOR_BYTES;
          const contentType =
            mount.content_type && mount.content_type !== "application/octet-stream"
              ? mount.content_type
              : inferContentType(mount.filename);
          const acceptRanges = "bytes";
          return new Response(null, {
            status: 200,
            statusText: "OK",
            headers: {
              "Accept-Ranges": acceptRanges,
              "Content-Type": contentType,
              "Content-Length": totalBytes.toString(),
              "Last-Modified": formatWebDavDate(mount.created_at || Date.now()),
              ETag: mount.etag || `W/"${mount.id}-${totalBytes}"`,
              "Access-Control-Allow-Origin": "*",
              "Access-Control-Expose-Headers":
                "Location, Content-Range, Accept-Ranges, Content-Length, Content-Type, DAV",
              "Cache-Control": "no-cache, no-store, must-revalidate",
              DAV: "1, 2",
              "MS-Author-Via": "DAV",
              ...getCorsHeaders(),
            },
          });
        }

        // Never redirect HEAD for Google CDN URLs; return synthetic 200 OK
        if (
          isGoogleCdn(decodedTargetFilename) ||
          isGoogleCdn(cleanPath) ||
          isGoogleCdn(url.searchParams.get("url") || "")
        ) {
          return new Response(null, {
            status: 200,
            statusText: "OK",
            headers: {
              "Accept-Ranges": "bytes",
              "Content-Type": "video/mp4",
              "Content-Length": SYNTHETIC_FLOOR_BYTES.toString(),
              "Last-Modified": formatWebDavDate(),
              ETag: `W/"google-cdn-${SYNTHETIC_FLOOR_BYTES}"`,
              "Access-Control-Allow-Origin": "*",
              "Access-Control-Expose-Headers":
                "Location, Content-Range, Accept-Ranges, Content-Length, Content-Type, DAV",
              "Cache-Control": "no-cache, no-store, must-revalidate",
              DAV: "1, 2",
              "MS-Author-Via": "DAV",
              ...getCorsHeaders(),
            },
          });
        }
      }

      // 3b. GET /dav/:userId/:filename (Direct 302 CDN Redirection & Edge Range Proxy)
      // By default: HTTP 302 Found Direct CDN Redirection (identical to Python webdav_engine.py).
      // Offloads heavy 4K UHD Remux video streams directly to high-speed CDN/storage with 0-byte edge latency,
      // avoiding Cloudflare Worker body bandwidth clamping and connection timeouts.
      // Transparent edge range proxying is available when ?proxy=1 is explicitly requested.
      if (method === "GET" && cleanPath) {
        const mount = await findMount(env, userId, decodedTargetFilename);
        if (mount) {
          const forceProxy =
            url.searchParams.get("proxy") === "1" ||
            url.searchParams.get("proxy") === "true" ||
            request.headers.get("X-Stream-Mode") === "proxy";

          if (!forceProxy) {
            // Default: Direct 302 CDN redirection for full wire speed streaming
            return new Response(null, {
              status: 302,
              statusText: "Found",
              headers: {
                Location: mount.upstream_url,
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers":
                  "Location, Content-Range, Accept-Ranges, Content-Length, Content-Type, DAV",
                "Cache-Control": "private, max-age=1800, stale-while-revalidate=300",
                "Vary": "Range",
                "Keep-Alive": "timeout=60, max=1000",
                DAV: "1, 2",
                "MS-Author-Via": "DAV",
                ...getCorsHeaders(),
              },
            });
          }

          // Fallback / Explicit Proxy: Range streaming proxy via Cloudflare Worker
          const upstreamHeaders = new Headers();
          upstreamHeaders.set(
            "User-Agent",
            request.headers.get("User-Agent") || "CloudStream-Edge-Proxy/1.0"
          );
          upstreamHeaders.set("Accept", "*/*");
          upstreamHeaders.set("Accept-Encoding", "identity");

          const clientRange = request.headers.get("Range");
          if (clientRange) {
            upstreamHeaders.set("Range", clientRange);
          }
          const clientIfRange = request.headers.get("If-Range");
          if (clientIfRange) {
            upstreamHeaders.set("If-Range", clientIfRange);
          }

          try {
            const upstreamResp = await fetch(mount.upstream_url, {
              method: "GET",
              headers: upstreamHeaders,
              redirect: "follow",
            });

            const responseHeaders = new Headers();
            responseHeaders.set("Accept-Ranges", "bytes");
            responseHeaders.set("Access-Control-Allow-Origin", "*");
            responseHeaders.set(
              "Access-Control-Expose-Headers",
              "Content-Range, Accept-Ranges, Content-Length, Content-Type, Content-Disposition, DAV"
            );
            responseHeaders.set("DAV", "1, 2");
            responseHeaders.set("MS-Author-Via", "DAV");

            const upstreamContentType = upstreamResp.headers.get("Content-Type") || "";
            const isInvalidType = (ct?: string | null): boolean =>
              !ct ||
              ct.toLowerCase().includes("application/octet-stream") ||
              ct.toLowerCase().includes("text/html");

            let effectiveContentType = "";
            if (mount.content_type && !isInvalidType(mount.content_type)) {
              effectiveContentType = mount.content_type;
            } else if (upstreamContentType && !isInvalidType(upstreamContentType)) {
              effectiveContentType = upstreamContentType;
            } else {
              effectiveContentType = inferContentType(mount.filename);
            }
            if (isInvalidType(effectiveContentType)) {
              effectiveContentType = "video/mp4";
            }

            responseHeaders.set("Content-Type", effectiveContentType);

            const totalBytes = mount.size_bytes > 0 ? mount.size_bytes : SYNTHETIC_FLOOR_BYTES;
            let responseStatus = upstreamResp.status;
            let responseStatusText = upstreamResp.statusText;
            let responseBody: ReadableStream<Uint8Array> | null = upstreamResp.body;

            let reqStart: number | null = null;
            let reqEnd: number | null = null;
            let isSuffixRange = false;
            if (clientRange) {
              const match = clientRange.match(/bytes=(\d*)-(\d*)/i);
              if (match) {
                if (match[1] !== undefined && match[1] !== "") {
                  reqStart = parseInt(match[1], 10);
                }
                if (match[2] !== undefined && match[2] !== "") {
                  reqEnd = parseInt(match[2], 10);
                }
                if ((match[1] === undefined || match[1] === "") && reqEnd !== null) {
                  isSuffixRange = true;
                }
              }
            }

            const upstreamContentRange = upstreamResp.headers.get("Content-Range");
            const upstreamContentLength = upstreamResp.headers.get("Content-Length");

            if (upstreamResp.status === 206) {
              if (upstreamContentRange) {
                responseHeaders.set("Content-Range", upstreamContentRange);
              }
              if (upstreamContentLength) {
                responseHeaders.set("Content-Length", upstreamContentLength);
              }
            } else if (upstreamResp.status === 200 && clientRange) {
              if (!isSuffixRange && (reqStart === null || reqStart === 0)) {
                responseStatus = 206;
                responseStatusText = "Partial Content";
                const endByte =
                  reqEnd !== null && reqEnd < totalBytes ? reqEnd : totalBytes - 1;
                const contentLength = endByte + 1;
                responseHeaders.set("Content-Range", `bytes 0-${endByte}/${totalBytes}`);
                responseHeaders.set("Content-Length", contentLength.toString());
                if (reqEnd !== null && reqEnd < totalBytes - 1 && upstreamResp.body) {
                  responseBody = createRangeStream(upstreamResp.body, 0, contentLength);
                }
              } else if (!isSuffixRange && reqStart !== null && reqStart > 0) {
                // Seek offset on progressive upstream: stream from reqStart using createRangeStream
                responseStatus = 206;
                responseStatusText = "Partial Content";
                const endByte =
                  reqEnd !== null && reqEnd < totalBytes ? reqEnd : totalBytes - 1;
                const contentLength = endByte - reqStart + 1;
                responseHeaders.set(
                  "Content-Range",
                  `bytes ${reqStart}-${endByte}/${totalBytes}`
                );
                responseHeaders.set("Content-Length", contentLength.toString());
                if (upstreamResp.body) {
                  const take =
                    reqEnd !== null && reqEnd < totalBytes - 1 ? contentLength : -1;
                  responseBody = createRangeStream(upstreamResp.body, reqStart, take);
                }
              } else {
                // Suffix range on non-range upstream:
                // RFC 9110 Section 14.2: When origin cannot satisfy byte range, the server MAY ignore
                // the Range header and return the entire representation with 200 OK.
                // This satisfies OkHttp validation in CX File Explorer (isSuccessful() === true)
                // and avoids crashing WebDAV playback with 416 Range Not Satisfiable.
                responseStatus = 200;
                responseStatusText = "OK";
                responseHeaders.delete("Content-Range");
                responseHeaders.set("Content-Length", totalBytes.toString());
                responseBody = upstreamResp.body;
              }
            } else {
              // RFC 9110 Section 14.4: NEVER emit Content-Range on HTTP 200 OK
              responseStatus = 200;
              responseStatusText = "OK";
              responseHeaders.delete("Content-Range");
              responseHeaders.set("Content-Length", totalBytes.toString());
            }

            responseHeaders.set(
              "Content-Disposition",
              `inline; filename="${encodeURIComponent(mount.filename)}"`
            );

            const upstreamEtag = upstreamResp.headers.get("ETag");
            if (upstreamEtag || mount.etag) {
              responseHeaders.set("ETag", mount.etag || upstreamEtag || "");
            }

            responseHeaders.set("Cache-Control", "public, max-age=3600");

            return new Response(responseBody, {
              status: responseStatus,
              statusText: responseStatusText,
              headers: responseHeaders,
            });
          } catch (err: any) {
            // Upstream proxy failed - fallback to 302 redirect
            return new Response(null, {
              status: 302,
              statusText: "Found",
              headers: {
                Location: mount.upstream_url,
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Expose-Headers": "Location, Content-Range, Accept-Ranges",
                "Cache-Control": "private, max-age=1800, stale-while-revalidate=300",
                "Vary": "Range",
                "Keep-Alive": "timeout=60, max=1000",
                DAV: "1, 2",
                "MS-Author-Via": "DAV",
                ...getCorsHeaders(),
              },
            });
          }
        }
      }

      // 4. DELETE /dav/:userId/:filename (Standalone Mount Removal)
      if (method === "DELETE" && cleanPath) {
        const removed = await removeMount(env, userId, decodedTargetFilename);
        if (removed) {
          return new Response(null, {
            status: 204,
            statusText: "No Content",
            headers: {
              DAV: "1, 2",
              "MS-Author-Via": "DAV",
              ...getCorsHeaders(),
            },
          });
        }
      }

      // =======================================================================
      // Fallback: Dynamic Cloud Shell Tunnel Reverse-Proxy Layer
      // =======================================================================

      const tunnelUrl = await getActiveTunnel(hubUrl, userId);

      // If tunnel is dormant or offline:
      if (!tunnelUrl) {
        // If user has a standalone mounts record with 0 items, return empty collection 207
        if (method === "PROPFIND" && !cleanPath && userRecord) {
          const depth = request.headers.get("Depth") || "1";
          const xml = buildWebDavMultiStatus(userId, userRecord, depth);
          return new Response(xml, {
            status: 207,
            statusText: "Multi-Status",
            headers: {
              "Content-Type": "application/xml; charset=utf-8",
              DAV: "1, 2",
              "MS-Author-Via": "DAV",
              ...getCorsHeaders(),
            },
          });
        }

        return makeDormantResponse(userId);
      }

      const encodedPath = cleanPath
        ? cleanPath
            .split("/")
            .map((seg) => encodeURIComponent(decodeURIComponent(seg)))
            .join("/")
        : "";

      // Tunnel GET / HEAD: HTTP 302 Found redirect (0 bytes video proxying)
      if (method === "HEAD") {
        if (
          isGoogleCdn(tunnelUrl) ||
          isGoogleCdn(cleanPath) ||
          isGoogleCdn(decodedTargetFilename) ||
          isGoogleCdn(url.searchParams.get("url") || "")
        ) {
          return new Response(null, {
            status: 200,
            statusText: "OK",
            headers: {
              "Accept-Ranges": "bytes",
              "Content-Type": "video/mp4",
              "Content-Length": SYNTHETIC_FLOOR_BYTES.toString(),
              "Last-Modified": formatWebDavDate(),
              ETag: `W/"google-cdn-${SYNTHETIC_FLOOR_BYTES}"`,
              "Access-Control-Allow-Origin": "*",
              "Access-Control-Expose-Headers":
                "Location, Content-Range, Accept-Ranges, Content-Length, Content-Type, DAV",
              "Cache-Control": "no-cache, no-store, must-revalidate",
              DAV: "1, 2",
              "MS-Author-Via": "DAV",
              ...getCorsHeaders(),
            },
          });
        }
        return makeRedirectResponse(tunnelUrl, encodedPath, url.search);
      }

      if (method === "GET") {
        return makeRedirectResponse(tunnelUrl, encodedPath, url.search);
      }

      // Tunnel PROPFIND, OPTIONS, PROPPATCH, MKCOL, DELETE: Reverse-proxy metadata
      let targetUrl = encodedPath
        ? `${tunnelUrl}/dav/${encodedPath}`
        : `${tunnelUrl}/dav/`;

      if (url.search) {
        targetUrl += url.search;
      }

      const fwdHeaders = new Headers(request.headers);
      fwdHeaders.delete("host");

      const reqBody = await request.arrayBuffer();

      const handleFallback = async (): Promise<Response> => {
        tunnelCache.delete(userId);
        const newTunnelUrl = await getActiveTunnel(hubUrl, userId, true);
        if (newTunnelUrl && newTunnelUrl !== tunnelUrl) {
          if (method === "HEAD") {
            if (
              isGoogleCdn(newTunnelUrl) ||
              isGoogleCdn(cleanPath) ||
              isGoogleCdn(decodedTargetFilename) ||
              isGoogleCdn(url.searchParams.get("url") || "")
            ) {
              return new Response(null, {
                status: 200,
                statusText: "OK",
                headers: {
                  "Accept-Ranges": "bytes",
                  "Content-Type": "video/mp4",
                  "Content-Length": SYNTHETIC_FLOOR_BYTES.toString(),
                  "Last-Modified": formatWebDavDate(),
                  ETag: `W/"google-cdn-${SYNTHETIC_FLOOR_BYTES}"`,
                  "Access-Control-Allow-Origin": "*",
                  "Access-Control-Expose-Headers":
                    "Location, Content-Range, Accept-Ranges, Content-Length, Content-Type, DAV",
                  "Cache-Control": "no-cache, no-store, must-revalidate",
                  DAV: "1, 2",
                  "MS-Author-Via": "DAV",
                  ...getCorsHeaders(),
                },
              });
            }
            return makeRedirectResponse(newTunnelUrl, encodedPath, url.search);
          }

          if (method === "GET") {
            return makeRedirectResponse(newTunnelUrl, encodedPath, url.search);
          }

          try {
            let retryTargetUrl = encodedPath
              ? `${newTunnelUrl}/dav/${encodedPath}`
              : `${newTunnelUrl}/dav/`;
            if (url.search) {
              retryTargetUrl += url.search;
            }

            const retryResp = await fetch(retryTargetUrl, {
              method,
              headers: fwdHeaders,
              body: reqBody && reqBody.byteLength > 0 ? reqBody : undefined,
            });

            const retryStatus = retryResp.status;
            if (retryStatus >= 500) {
              const retryErrText = await retryResp.text();
              const retryContentType = retryResp.headers.get("content-type") || "";
              if (isDeadTunnelError(retryStatus, retryContentType, retryErrText)) {
                tunnelCache.delete(userId);
                return makeDormantResponse(userId, "unreachable or disconnected");
              }
              return new Response(retryErrText, {
                status: retryStatus,
                headers: retryResp.headers,
              });
            }

            return await formatUpstreamResponse(retryResp, newTunnelUrl, userId);
          } catch (_retryErr) {
            tunnelCache.delete(userId);
            return makeDormantResponse(userId, "unreachable or disconnected");
          }
        }
        return makeDormantResponse(userId, "unreachable or disconnected");
      };

      try {
        const upstreamResp = await fetch(targetUrl, {
          method,
          headers: fwdHeaders,
          body: reqBody && reqBody.byteLength > 0 ? reqBody : undefined,
        });

        const status = upstreamResp.status;

        if (status >= 500) {
          const errText = await upstreamResp.text();
          const contentType = upstreamResp.headers.get("content-type") || "";
          if (isDeadTunnelError(status, contentType, errText)) {
            return await handleFallback();
          }
          return new Response(errText, {
            status,
            headers: upstreamResp.headers,
          });
        }

        return await formatUpstreamResponse(upstreamResp, tunnelUrl, userId);
      } catch (_fetchErr) {
        return await handleFallback();
      }
    }

    // Unmatched routes -> 404 Not Found
    return new Response(
      JSON.stringify({
        error: "Not Found",
        message: `Endpoint '${pathname}' not found. Supported: /health, /dav/{user_id}/, /api/mount/{user_id}, /api/mounts/{user_id}, /api/unmount-all/{user_id}`,
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
