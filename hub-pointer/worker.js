/**
 * CloudStream Pointer Hub - Cloudflare Worker Edge Router
 *
 * Free serverless edge router running on Cloudflare Workers.
 * Features:
 * - Permanent CX File Explorer WebDAV redirection (HTTP 302 Found)
 * - Zero video byte proxying (direct cloud-to-client video streaming)
 * - Multi-user Cloud Shell tunnel mapping with 12-hour TTL
 * - Cloudflare KV persistence with in-memory fallback
 * - Dormant detection with helpful XML responses for CX File Explorer
 */

const DEFAULT_TTL_SEC = 43200; // 12 hours
const DORMANT_XML_RESPONSE =
  '<?xml version="1.0" encoding="utf-8"?>' +
  '<error>' +
  '<message>Cloud Shell is dormant. Run your command in Google Cloud Shell to activate.</message>' +
  '<status>dormant</status>' +
  '</error>';

// In-memory fallback map for environments without Cloudflare KV bound
const memoryRegistry = new Map();

/**
 * Standard CORS headers for cross-origin media and API requests
 */
function getCorsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS, PROPFIND",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
  };
}

/**
 * Return JSON response with CORS headers
 */
function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...getCorsHeaders(),
      ...extraHeaders,
    },
  });
}

/**
 * Retrieve user record from Cloudflare KV or in-memory fallback
 */
async function getUser(env, userId) {
  const cleanId = userId.trim();
  if (env && env.USER_REGISTRY) {
    try {
      const data = await env.USER_REGISTRY.get(cleanId, { type: "json" });
      if (data) return data;
    } catch (e) {
      console.error(`KV get error for ${cleanId}:`, e);
    }
  }

  // Fallback to in-memory store
  const entry = memoryRegistry.get(cleanId);
  if (!entry) return null;

  const now = Date.now() / 1000;
  if ((now - entry.last_seen) >= entry.ttl_sec) {
    memoryRegistry.delete(cleanId);
    return null;
  }
  return entry;
}

/**
 * Save user record to Cloudflare KV and in-memory fallback
 */
async function saveUser(env, userId, entry) {
  const cleanId = userId.trim();
  const ttl = entry.ttl_sec || DEFAULT_TTL_SEC;

  if (env && env.USER_REGISTRY) {
    try {
      await env.USER_REGISTRY.put(cleanId, JSON.stringify(entry), {
        // Cloudflare KV requires minimum expirationTtl of 60 seconds
        expirationTtl: Math.max(60, ttl),
      });
    } catch (e) {
      console.error(`KV put error for ${cleanId}:`, e);
    }
  }

  memoryRegistry.set(cleanId, entry);
}

/**
 * Update user last_seen timestamp
 */
async function touchUser(env, userId, entry) {
  const now = Date.now() / 1000;
  entry.last_seen = now;
  await saveUser(env, userId, entry);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const pathname = url.pathname;
    const method = request.method.toUpperCase();

    // 1. Handle CORS preflight for non-DAV endpoints
    if (method === "OPTIONS" && !pathname.startsWith("/dav/")) {
      return new Response(null, {
        status: 204,
        headers: getCorsHeaders(),
      });
    }

    // 2. Health & Root Endpoints
    if (pathname === "/" || pathname === "/health") {
      let activeCount = 0;
      const now = Date.now() / 1000;
      for (const [_, entry] of memoryRegistry) {
        if ((now - entry.last_seen) < entry.ttl_sec) {
          activeCount++;
        }
      }
      return jsonResponse({
        status: "healthy",
        service: "cloudstream-central-hub-edge",
        edge_runtime: "cloudflare-workers",
        active_users_in_memory: activeCount,
        timestamp: now,
      });
    }

    // 3. POST /api/register
    if (pathname === "/api/register" && method === "POST") {
      let payload;
      try {
        payload = await request.json();
      } catch (e) {
        return jsonResponse({ error: "Invalid JSON payload" }, 400);
      }

      if (!payload.user_id || !payload.tunnel_url) {
        return jsonResponse({ error: "Missing required fields: user_id, tunnel_url" }, 400);
      }

      const userId = String(payload.user_id).trim();
      const rawTunnel = String(payload.tunnel_url).trim();
      const cleanTunnel = rawTunnel.replace(/\/+$/, "");
      const now = Date.now() / 1000;
      const ttlSec = Number(payload.ttl_sec) || DEFAULT_TTL_SEC;

      const existing = await getUser(env, userId);
      const registeredAt = existing ? existing.registered_at : now;

      const entry = {
        user_id: userId,
        tunnel_url: cleanTunnel,
        token: payload.token || (existing ? existing.token : null),
        registered_at: registeredAt,
        last_seen: now,
        ttl_sec: ttlSec,
      };

      await saveUser(env, userId, entry);

      return jsonResponse({
        status: "registered",
        user_id: userId,
        tunnel_url: cleanTunnel,
        active: true,
      });
    }

    // 4. GET /api/status/:userId
    const statusMatch = pathname.match(/^\/api\/status\/([^/]+)$/);
    if (statusMatch && method === "GET") {
      const userId = decodeURIComponent(statusMatch[1]).trim();
      const user = await getUser(env, userId);

      if (!user) {
        return jsonResponse({
          active: false,
          user_id: userId,
          tunnel_url: null,
          last_seen: null,
          ttl_remaining_sec: 0,
        });
      }

      const now = Date.now() / 1000;
      const isActive = (now - user.last_seen) < user.ttl_sec;
      const ttlRemaining = isActive ? Math.max(0, Math.floor(user.last_seen + user.ttl_sec - now)) : 0;

      return jsonResponse({
        active: isActive,
        user_id: userId,
        tunnel_url: isActive ? user.tunnel_url : null,
        last_seen: user.last_seen,
        ttl_remaining_sec: ttlRemaining,
      });
    }

    // 5. POST /api/mount/:userId
    const mountMatch = pathname.match(/^\/api\/mount\/([^/]+)$/);
    if (mountMatch && method === "POST") {
      const userId = decodeURIComponent(mountMatch[1]).trim();
      const user = await getUser(env, userId);

      if (!user) {
        return jsonResponse({
          error: "Cloud Shell is dormant. Run your command in Google Cloud Shell to activate.",
          status: "dormant",
          user_id: userId,
        }, 503);
      }

      let payload;
      try {
        payload = await request.json();
      } catch (e) {
        return jsonResponse({ error: "Invalid JSON payload" }, 400);
      }

      const targetUrl = `${user.tunnel_url.replace(/\/+$/, "")}/api/mount`;

      try {
        const upstreamResp = await fetch(targetUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          signal: AbortSignal.timeout(10000), // 10s timeout
        });

        const respBody = await upstreamResp.text();
        const contentType = upstreamResp.headers.get("content-type") || "application/json";

        return new Response(respBody, {
          status: upstreamResp.status,
          headers: {
            "Content-Type": contentType,
            ...getCorsHeaders(),
          },
        });
      } catch (e) {
        return jsonResponse({
          error: `Failed to forward mount to Cloud Shell: ${e.message}`,
          status: "forward_failed",
          user_id: userId,
        }, 502);
      }
    }

    // 6. WebDAV Dynamic Redirect: /dav/:userId and /dav/:userId/*
    const davMatch = pathname.match(/^\/dav\/([^/]+)(?:\/(.*))?$/);
    if (davMatch) {
      const userId = decodeURIComponent(davMatch[1]).trim();
      const subpath = davMatch[2] || "";
      const user = await getUser(env, userId);

      // Dormant session -> Return 503 with friendly XML for CX File Explorer
      if (!user) {
        return new Response(DORMANT_XML_RESPONSE, {
          status: 503,
          headers: {
            "Content-Type": "application/xml; charset=utf-8",
            "DAV": "1",
            ...getCorsHeaders(),
          },
        });
      }

      // Active session -> Touch last_seen
      if (ctx && ctx.waitUntil) {
        ctx.waitUntil(touchUser(env, userId, user));
      } else {
        await touchUser(env, userId, user);
      }

      const cleanSub = subpath.replace(/^\/+/, "");
      const davPath = cleanSub ? `/dav/${cleanSub}` : "/dav/";
      let targetUrl = `${user.tunnel_url.replace(/\/+$/, "")}${davPath}`;

      if (url.search) {
        targetUrl += url.search;
      }

      // GET and HEAD: HTTP 302 Found redirect (preserving zero video byte hub bandwidth)
      if (request.method === "GET" || request.method === "HEAD") {
        return new Response(null, {
          status: 302,
          headers: {
            "Location": targetUrl,
            "DAV": "1",
            ...getCorsHeaders(),
          },
        });
      }

      // WebDAV metadata methods (PROPFIND, OPTIONS, PROPPATCH, MKCOL, DELETE): reverse-proxy to targetUrl
      try {
        const body = ['GET', 'HEAD'].includes(request.method) ? undefined : await request.arrayBuffer();

        const headers = new Headers(request.headers);
        headers.delete("host");

        const upstreamResp = await fetch(targetUrl, {
          method: request.method,
          headers,
          body,
        });

        const respHeaders = new Headers(upstreamResp.headers);
        respHeaders.set("DAV", "1");
        for (const [key, value] of Object.entries(getCorsHeaders())) {
          respHeaders.set(key, value);
        }

        const respBody = [204, 205, 304].includes(upstreamResp.status) ? null : upstreamResp.body;

        return new Response(respBody, {
          status: upstreamResp.status,
          headers: respHeaders,
        });
      } catch (e) {
        return jsonResponse({
          error: `Failed to proxy WebDAV request: ${e.message}`,
          status: "dav_proxy_failed",
          user_id: userId,
        }, 502);
      }
    }

    // 7. Not Found fallback
    return jsonResponse({ error: "Endpoint not found" }, 404);
  },
};
