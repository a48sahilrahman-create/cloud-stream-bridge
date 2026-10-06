/**
 * CloudStream WebDAV Bridge - Cloudflare Worker Test Suite
 * Automated standalone Node.js test script (zero external dependencies).
 * Validates:
 * 1. /health returns 200 OK
 * 2. Dormant user returns HTTP 503 WebDAV XML
 * 3. PROPFIND proxies upstream and rewrites <D:href> tags
 * 4. GET / HEAD returns HTTP 302 Found pointing to tunnel stream
 * 5. Upstream tunnel error fallback handling
 */

import assert from 'node:assert';
import worker, { warmStreamStorage, isGoogleCdn } from './src/index.ts';
import {
  getUserMounts,
  saveUserMount,
  deleteUserMount,
  clearUserMounts,
  invalidateCache,
  MountManager,
} from './src/mount_manager.ts';

const MOCK_HUB_URL = 'https://mock-hub.local';
const MOCK_TUNNEL_URL = 'https://rapid-stream-1234.trycloudflare.com';

// Mock registry state
const mockUsers = {
  active_user: {
    active: true,
    tunnel_url: MOCK_TUNNEL_URL,
    last_seen: Date.now() / 1000,
  },
  dormant_user: {
    active: false,
    tunnel_url: null,
    last_seen: null,
  },
};

// Intercept global fetch for upstream mock routing
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, init = {}) => {
  const urlStr = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
  const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();

  // 1. Intercept Central Hub status endpoints
  if (urlStr.startsWith(MOCK_HUB_URL)) {
    const statusMatch = urlStr.match(/\/api\/status\/([^/?]+)/);
    if (statusMatch) {
      const userId = decodeURIComponent(statusMatch[1]);
      const user = mockUsers[userId];
      if (user) {
        return new Response(JSON.stringify(user), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ active: false, tunnel_url: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
  }

  // 2. Intercept Upstream Cloudflare Tunnel PROPFIND
  if (urlStr.startsWith(MOCK_TUNNEL_URL)) {
    if (method === 'PROPFIND') {
      const mockWebDavXml = `<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>${MOCK_TUNNEL_URL}/dav/Movies/Inception.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>Inception.mkv</D:displayname>
        <D:getcontentlength>1572864000</D:getcontentlength>
        <D:getcontenttype>video/x-matroska</D:getcontenttype>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/dav/Movies/Interstellar.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>Interstellar.mkv</D:displayname>
        <D:getcontentlength>2147483648</D:getcontentlength>
        <D:getcontenttype>video/x-matroska</D:getcontenttype>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>`;

      return new Response(mockWebDavXml, {
        status: 207,
        statusText: 'Multi-Status',
        headers: {
          'Content-Type': 'application/xml; charset=utf-8',
          DAV: '1, 2',
        },
      });
    }

    if (urlStr.includes('/dead_tunnel/')) {
      return new Response('<html><body>Cloudflare Error 502 Bad Gateway</body></html>', {
        status: 502,
        headers: { 'Content-Type': 'text/html' },
      });
    }
  }

  // 3. Intercept Upstream CDN storage for standalone mounts
  if (urlStr.includes('cdn.upstream.com')) {
    const rangeHeader =
      init.headers instanceof Headers
        ? init.headers.get('Range')
        : init.headers?.Range || init.headers?.range;

    if (rangeHeader) {
      return new Response('fake-chunk-bytes', {
        status: 206,
        statusText: 'Partial Content',
        headers: {
          'Content-Type': 'video/x-matroska',
          'Content-Range': 'bytes 0-15/64424509440',
          'Content-Length': '16',
          'Accept-Ranges': 'bytes',
        },
      });
    }

    return new Response('fake-stream-body', {
      status: 200,
      statusText: 'OK',
      headers: {
        'Content-Type': 'video/x-matroska',
        'Content-Length': '64424509440',
        'Accept-Ranges': 'bytes',
      },
    });
  }

  // 4. Intercept Google CDN stream requests (Google UploadServer ignores Range and returns 200 OK from byte 0)
  if (
    urlStr.includes('googleusercontent.com') ||
    urlStr.includes('googlevideo.com') ||
    urlStr.includes('drive.google.com') ||
    urlStr.includes('photos.google.com')
  ) {
    const returnHtml = urlStr.includes('return_html');
    const returnOctet = urlStr.includes('return_octet');
    const contentType = returnHtml
      ? 'text/html; charset=UTF-8'
      : returnOctet
      ? 'application/octet-stream'
      : 'video/mp4';

    return new Response('fake-google-stream-body', {
      status: 200,
      statusText: 'OK',
      headers: {
        'Content-Type': contentType,
        'Content-Length': '107374182400',
        'Accept-Ranges': 'none',
      },
    });
  }

  // Fallback to real fetch if not intercepted
  return originalFetch(input, init);
};

const mockEnv = {
  RENDER_HUB_URL: MOCK_HUB_URL,
};

const mockCtx = {
  waitUntil: () => {},
  passThroughOnException: () => {},
};

let testsPassed = 0;

async function runTest(name, fn) {
  try {
    await fn();
    testsPassed++;
    console.log(`  PASS: ${name}`);
  } catch (err) {
    console.error(`  FAIL: ${name}`);
    console.error(err);
    process.exitCode = 1;
    throw err;
  }
}

console.log('Running CloudStream Worker Automated Verification Suite...\n');

// Test 1: Health check returns 200 OK
await runTest('1. Test /health returns 200 OK with correct JSON payload', async () => {
  const req = new Request('https://edge.cloudstream.local/health');
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Expected status 200 for /health');
  const contentType = res.headers.get('content-type') || '';
  assert.ok(contentType.includes('application/json'), 'Expected application/json header');

  const json = await res.json();
  assert.strictEqual(json.status, 'ok', 'Expected status: "ok" in JSON body');
  assert.strictEqual(json.service, 'cloudstream-cloudflare-worker', 'Expected correct service name');
  assert.strictEqual(json.render_hub_url, MOCK_HUB_URL, 'Expected matching hub url in payload');
});

// Test 2: Dormant user returns HTTP 503 WebDAV XML
await runTest('2. Test dormant user returns HTTP 503 WebDAV XML error', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/dormant_user/');
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 503, 'Expected status 503 for dormant user');
  const contentType = res.headers.get('content-type') || '';
  assert.ok(contentType.includes('xml'), 'Expected XML content type');
  assert.strictEqual(res.headers.get('DAV'), '1', 'Expected WebDAV DAV header');

  const xmlBody = await res.text();
  assert.ok(xmlBody.includes('<D:error xmlns:D="DAV:">'), 'Expected D:error XML root element');
  assert.ok(xmlBody.includes('dormant or offline'), 'Expected dormant status explanation in description');
  assert.ok(xmlBody.includes('dormant_user'), 'Expected userId mentioned in description');
});

// Test 3: PROPFIND request proxies upstream and rewrites <D:href> tags
await runTest('3. Test PROPFIND proxies upstream and rewrites <D:href> tags to permanent path', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/active_user/Movies/', {
    method: 'PROPFIND',
    headers: {
      Depth: '1',
      'Content-Type': 'application/xml',
    },
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 207, 'Expected 207 Multi-Status for PROPFIND');
  const xmlBody = await res.text();

  // Verify temporary tunnel domain is completely rewritten
  assert.ok(!xmlBody.includes(MOCK_TUNNEL_URL), 'Temporary tunnel URL must NOT exist in rewritten XML');

  // Verify full tunnel URL href tag was rewritten to permanent /dav/{user_id}/... path
  assert.ok(
    xmlBody.includes('<D:href>/dav/active_user/Movies/Inception.mkv</D:href>'),
    'Expected rewritten full URL in <D:href> tag'
  );

  // Verify relative /dav/... href tag was rewritten to permanent /dav/{user_id}/... path
  assert.ok(
    xmlBody.includes('<D:href>/dav/active_user/Movies/Interstellar.mkv</D:href>'),
    'Expected rewritten relative path in <D:href> tag'
  );

  assert.strictEqual(res.headers.get('DAV'), '1', 'Expected WebDAV header');
});

// Test 4: GET / HEAD returns HTTP 302 Found pointing to tunnel stream
await runTest('4. Test GET returns HTTP 302 Found with Location header pointing to tunnel stream', async () => {
  const streamPath = '/dav/active_user/Movies/Inception.mkv?auth=abc123xyz';
  const req = new Request(`https://edge.cloudstream.local${streamPath}`, {
    method: 'GET',
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 302, 'Expected HTTP 302 redirect for GET video streaming');
  const location = res.headers.get('Location');
  const expectedLocation = `${MOCK_TUNNEL_URL}/dav/Movies/Inception.mkv?auth=abc123xyz`;
  assert.strictEqual(location, expectedLocation, 'Expected Location header pointing directly to tunnel stream');
  assert.strictEqual(res.headers.get('DAV'), '1', 'Expected WebDAV header');
  assert.ok(
    res.headers.get('Cache-Control')?.includes('private, max-age=1800'),
    'Expected Cache-Control header to include private, max-age=1800'
  );
  assert.ok(
    res.headers.get('Vary')?.includes('Range'),
    'Expected Vary header to include Range'
  );
  assert.ok(
    res.headers.has('Keep-Alive') || Boolean(res.headers.get('Keep-Alive')),
    'Expected Keep-Alive header to be present'
  );
});

await runTest('5. Test HEAD returns HTTP 302 Found with Location header pointing to tunnel stream', async () => {
  const streamPath = '/dav/active_user/Movies/Inception.mkv';
  const req = new Request(`https://edge.cloudstream.local${streamPath}`, {
    method: 'HEAD',
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 302, 'Expected HTTP 302 redirect for HEAD stream probe');
  const location = res.headers.get('Location');
  const expectedLocation = `${MOCK_TUNNEL_URL}/dav/Movies/Inception.mkv`;
  assert.strictEqual(location, expectedLocation, 'Expected Location header pointing directly to tunnel stream');
  assert.strictEqual(res.headers.get('DAV'), '1', 'Expected WebDAV header');
  assert.ok(
    res.headers.get('Cache-Control')?.includes('private, max-age=1800'),
    'Expected Cache-Control header to include private, max-age=1800'
  );
  assert.ok(
    res.headers.get('Vary')?.includes('Range'),
    'Expected Vary header to include Range'
  );
  assert.ok(
    res.headers.has('Keep-Alive') || Boolean(res.headers.get('Keep-Alive')),
    'Expected Keep-Alive header to be present'
  );
});

// ============================================================================
// Standalone WebDAV RFC 4918 & Edge REST API Verification Tests
// ============================================================================

// Test 6: OPTIONS /dav/:userId/ returns 200 OK with RFC 4918 headers
await runTest('6. Test OPTIONS /dav/:userId/ returns 200 OK with RFC 4918 headers', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/test_user_dav/', {
    method: 'OPTIONS',
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Expected status 200 for WebDAV OPTIONS');
  assert.strictEqual(res.headers.get('DAV'), '1, 2', 'Expected DAV: 1, 2 header');
  assert.strictEqual(res.headers.get('MS-Author-Via'), 'DAV', 'Expected MS-Author-Via: DAV');
  const allow = res.headers.get('Allow') || '';
  assert.ok(allow.includes('OPTIONS'), 'Allow should include OPTIONS');
  assert.ok(allow.includes('PROPFIND'), 'Allow should include PROPFIND');
  assert.ok(allow.includes('GET'), 'Allow should include GET');
  assert.ok(allow.includes('DELETE'), 'Allow should include DELETE');
  assert.strictEqual(res.headers.get('Accept-Ranges'), 'bytes', 'Expected Accept-Ranges: bytes');
});

// Test 7: POST /api/mount/:userId mounts a stream with full metadata
await runTest('7. Test POST /api/mount/:userId mounts stream with full metadata', async () => {
  const req = new Request('https://edge.cloudstream.local/api/mount/user_42', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: 'https://cdn.upstream.com/streams/Oppenheimer.2023.2160p.mkv',
      filename: 'Oppenheimer.2023.2160p.mkv',
      title: 'Oppenheimer (2023)',
      size_bytes: 64424509440,
      content_type: 'video/x-matroska',
    }),
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Expected status 200 for mount');
  const json = await res.json();
  assert.strictEqual(json.status, 'mounted');
  assert.ok(json.mount, 'Expected mount object in response');
  assert.strictEqual(json.mount.filename, 'Oppenheimer.2023.2160p.mkv');
  assert.strictEqual(json.mount.size_bytes, 64424509440);
  assert.strictEqual(json.mount.content_type, 'video/x-matroska');
  assert.ok(json.mount.id.startsWith('mount_'), 'Expected mount_ prefix on id');
  assert.ok(json.mount.etag.includes('64424509440'), 'Expected size in etag');
});

// Test 8: POST /api/mount/:userId with 0 size applies 100 GiB synthetic floor
await runTest('8. Test POST /api/mount/:userId applies 100 GiB synthetic floor when unprobed', async () => {
  const req = new Request('https://edge.cloudstream.local/api/mount/user_42', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: 'https://unreachable.cdn.fake/stream.mp4',
      size_bytes: 0,
    }),
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Expected status 200 for mount with synthetic floor');
  const json = await res.json();
  assert.strictEqual(json.status, 'mounted');
  assert.strictEqual(json.mount.size_bytes, 107374182400, 'Expected 100 GiB (107374182400 bytes) floor');
  assert.strictEqual(json.mount.filename, 'stream.mp4');
  assert.strictEqual(json.mount.content_type, 'video/mp4');
});

// Test 9: GET /api/mounts/:userId returns mounted streams
await runTest('9. Test GET /api/mounts/:userId returns user mounts list', async () => {
  const req = new Request('https://edge.cloudstream.local/api/mounts/user_42');
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Expected status 200 for mounts list');
  const json = await res.json();
  assert.strictEqual(json.user_id, 'user_42');
  assert.strictEqual(json.mounts.length, 2, 'Expected 2 active mounts');
});

// Test 10: PROPFIND /dav/:userId/ Depth: 0 returns root collection only
await runTest('10. Test PROPFIND /dav/:userId/ Depth: 0 returns root collection only', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/user_42/', {
    method: 'PROPFIND',
    headers: { Depth: '0' },
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 207, 'Expected 207 Multi-Status');
  const xml = await res.text();
  assert.ok(xml.includes('<D:multistatus'), 'Expected multistatus element');
  assert.ok(xml.includes('<D:collection/>'), 'Expected collection element in root');
  assert.ok(xml.includes('/dav/user_42/'), 'Expected user root href');
  assert.ok(!xml.includes('Oppenheimer'), 'Depth 0 must not include child items');
});

// Test 11: PROPFIND /dav/:userId/ Depth: 1 returns root collection and child files with CX tags
await runTest('11. Test PROPFIND /dav/:userId/ Depth: 1 returns CX-compliant child file metadata', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/user_42/', {
    method: 'PROPFIND',
    headers: { Depth: '1' },
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 207, 'Expected 207 Multi-Status');
  const xml = await res.text();
  assert.ok(xml.includes('<D:collection/>'), 'Expected root collection tag');
  assert.ok(xml.includes('Oppenheimer.2023.2160p.mkv'), 'Expected Oppenheimer child');
  assert.ok(xml.includes('<D:getcontentlength>64424509440</D:getcontentlength>'), 'Expected getcontentlength');
  assert.ok(xml.includes('<D:getcontenttype>video/x-matroska</D:getcontenttype>'), 'Expected getcontenttype');
  assert.ok(xml.includes('<D:resourcetype/>'), 'Expected empty resourcetype tag for file');
  assert.ok(xml.includes('<D:supportedlock>'), 'Expected supportedlock tag');
  assert.ok(xml.includes('<D:getetag>'), 'Expected getetag tag');
  assert.ok(xml.includes('<D:getlastmodified>'), 'Expected getlastmodified tag');
});

// Test 12: GET /dav/:userId/:filename defaults to HTTP 302 direct redirect and proxies with ?proxy=1
await runTest('12. Test GET /dav/:userId/:filename defaults to HTTP 302 direct redirect and proxies with ?proxy=1', async () => {
  // 12a: Default is Direct 302 Found Redirection (matches Google Cloud webdav_engine.py for smooth 4K Remux line speed)
  const reqDefault = new Request('https://edge.cloudstream.local/dav/user_42/Oppenheimer.2023.2160p.mkv', {
    method: 'GET',
  });
  const resDefault = await worker.fetch(reqDefault, mockEnv, mockCtx);
  assert.strictEqual(resDefault.status, 302, 'Expected HTTP 302 redirect by default for direct CDN line-speed streaming');
  assert.strictEqual(resDefault.headers.get('Location'), 'https://cdn.upstream.com/streams/Oppenheimer.2023.2160p.mkv');
  assert.strictEqual(resDefault.headers.get('Accept-Ranges'), 'bytes');
  assert.ok(
    resDefault.headers.get('Cache-Control')?.includes('private, max-age=1800'),
    'Expected Cache-Control header to include private, max-age=1800'
  );
  assert.ok(
    resDefault.headers.get('Vary')?.includes('Range'),
    'Expected Vary header to include Range'
  );
  assert.ok(
    resDefault.headers.has('Keep-Alive') || Boolean(resDefault.headers.get('Keep-Alive')),
    'Expected Keep-Alive header to be present'
  );

  // 12b: Transparent Edge Range Streaming Proxy when ?proxy=1 is requested
  const reqProxy = new Request('https://edge.cloudstream.local/dav/user_42/Oppenheimer.2023.2160p.mkv?proxy=1', {
    method: 'GET',
    headers: {
      Range: 'bytes=0-15',
    },
  });
  const resProxy = await worker.fetch(reqProxy, mockEnv, mockCtx);
  assert.strictEqual(resProxy.status, 206, 'Expected HTTP 206 Partial Content when ?proxy=1 is passed');
  assert.strictEqual(resProxy.headers.get('Accept-Ranges'), 'bytes');
  assert.strictEqual(resProxy.headers.get('Content-Range'), 'bytes 0-15/64424509440');
  assert.strictEqual(resProxy.headers.get('Content-Type'), 'video/x-matroska');
});

// Test 13: HEAD /dav/:userId/:filename returns HTTP 200 OK synthetic probe
await runTest('13. Test HEAD /dav/:userId/:filename returns HTTP 200 OK synthetic probe with exact content length', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/user_42/Oppenheimer.2023.2160p.mkv', {
    method: 'HEAD',
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Expected HTTP 200 OK synthetic probe');
  assert.strictEqual(res.headers.get('Content-Length'), '64424509440');
  assert.strictEqual(res.headers.get('Content-Type'), 'video/x-matroska');
  assert.strictEqual(res.headers.get('Accept-Ranges'), 'bytes');
  assert.strictEqual(res.headers.get('DAV'), '1, 2');
});

// Test 14: DELETE /dav/:userId/:filename removes stream from KV and returns 204 No Content
await runTest('14. Test DELETE /dav/:userId/:filename unmounts and returns 204 No Content', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/user_42/stream.mp4', {
    method: 'DELETE',
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 204, 'Expected status 204 for DELETE');

  // Verify mount count is now 1
  const listReq = new Request('https://edge.cloudstream.local/api/mounts/user_42');
  const listRes = await worker.fetch(listReq, mockEnv, mockCtx);
  const json = await listRes.json();
  assert.strictEqual(json.mounts.length, 1, 'Expected 1 remaining mount after DELETE');
  assert.strictEqual(json.mounts[0].filename, 'Oppenheimer.2023.2160p.mkv');
});

// Test 15: DELETE /api/mounts/:userId/:filename unmounts stream via REST
await runTest('15. Test DELETE /api/mounts/:userId/:filename unmounts file via REST', async () => {
  const req = new Request('https://edge.cloudstream.local/api/mounts/user_42/Oppenheimer.2023.2160p.mkv', {
    method: 'DELETE',
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Expected status 200 for REST unmount');
  const json = await res.json();
  assert.strictEqual(json.status, 'unmounted');
  assert.strictEqual(json.filename, 'Oppenheimer.2023.2160p.mkv');
});

// Test 16: POST /api/unmount-all/:userId clears all mounts
await runTest('16. Test POST /api/unmount-all/:userId clears all mounts', async () => {
  // Mount 2 items first
  for (const name of ['movie1.mkv', 'movie2.mkv']) {
    const mountReq = new Request('https://edge.cloudstream.local/api/mount/user_batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: `https://cdn.example.com/${name}`, size_bytes: 1000 }),
    });
    await worker.fetch(mountReq, mockEnv, mockCtx);
  }

  // Clear all
  const clearReq = new Request('https://edge.cloudstream.local/api/unmount-all/user_batch', {
    method: 'POST',
  });
  const clearRes = await worker.fetch(clearReq, mockEnv, mockCtx);
  assert.strictEqual(clearRes.status, 200);
  const clearJson = await clearRes.json();
  assert.strictEqual(clearJson.status, 'cleared');
  assert.strictEqual(clearJson.mounts_cleared, 2);

  // Verify empty
  const verifyReq = new Request('https://edge.cloudstream.local/api/mounts/user_batch');
  const verifyRes = await worker.fetch(verifyReq, mockEnv, mockCtx);
  const verifyJson = await verifyRes.json();
  assert.strictEqual(verifyJson.mounts.length, 0);
});

// Test 17: GET /health returns serverless-edge engine and kv_bound status
await runTest('17. Test GET /health returns serverless-edge engine and kv_bound', async () => {
  const mockEnvWithKv = {
    ...mockEnv,
    MOUNTS_KV: {
      get: async () => null,
      put: async () => {},
      delete: async () => {},
    },
  };
  const req = new Request('https://edge.cloudstream.local/health');
  const res = await worker.fetch(req, mockEnvWithKv, mockCtx);

  assert.strictEqual(res.status, 200);
  const json = await res.json();
  assert.strictEqual(json.status, 'ok');
  assert.strictEqual(json.engine, 'serverless-edge');
  assert.strictEqual(json.kv_bound, true);
});

// Test 18: MountManager saveUserMount writes to KV mounts:{userId} with 86400s TTL
await runTest('18. Test MountManager saveUserMount writes to KV mounts:{userId} with TTL 86400s', async () => {
  const kvStore = new Map();
  const putCalls = [];
  const deleteCalls = [];

  const mockKv = {
    get: async (key, opts) => {
      const val = kvStore.get(key);
      if (!val) return null;
      if (opts === 'json' || opts?.type === 'json') return JSON.parse(val);
      return val;
    },
    put: async (key, val, opts) => {
      putCalls.push({ key, val, opts });
      kvStore.set(key, val);
    },
    delete: async (key) => {
      deleteCalls.push(key);
      kvStore.delete(key);
    },
  };

  const env = { MOUNTS_KV: mockKv };
  const userId = 'user_kv_spec';

  const mountItem = {
    id: 'spec_mount_1',
    filename: 'dune.2024.mkv',
    title: 'Dune Part Two',
    upstream_url: 'https://cdn.upstream.com/dune.mkv',
    size_bytes: 42949672960,
    content_type: 'video/x-matroska',
    created_at: Date.now(),
    etag: 'etag_dune_123',
  };

  const saved = await saveUserMount(env, userId, mountItem);
  assert.strictEqual(saved.length, 1);
  assert.strictEqual(saved[0].id, 'spec_mount_1');
  assert.strictEqual(putCalls.length, 1);
  assert.strictEqual(putCalls[0].key, 'mounts:user_kv_spec');
  assert.strictEqual(putCalls[0].opts.expirationTtl, 86400);
});

// Test 19: MountManager getUserMounts uses 15s in-memory isolate cache
await runTest('19. Test MountManager getUserMounts uses 15s in-memory isolate cache', async () => {
  let kvReadCount = 0;
  const mockKv = {
    get: async () => {
      kvReadCount++;
      return null;
    },
    put: async () => {},
    delete: async () => {},
  };

  const env = { MOUNTS_KV: mockKv };
  const userId = 'user_kv_cache_spec';

  // First call hits cache miss
  await getUserMounts(env, userId);
  // Second call within 15s hits in-memory cache
  await getUserMounts(env, userId);
  assert.strictEqual(kvReadCount, 1, 'KV read count must be 1 due to 15s isolate cache');

  // Invalidate cache allows subsequent KV read
  invalidateCache(userId);
  await getUserMounts(env, userId);
  assert.strictEqual(kvReadCount, 2, 'KV read count must increment after invalidateCache');
});

// Test 20: MountManager deleteUserMount by id and by filename
await runTest('20. Test MountManager deleteUserMount by id and filename', async () => {
  const kvStore = new Map();
  const mockKv = {
    get: async (key, opts) => {
      const val = kvStore.get(key);
      if (!val) return null;
      if (opts === 'json' || opts?.type === 'json') return JSON.parse(val);
      return val;
    },
    put: async (key, val) => kvStore.set(key, val),
    delete: async (key) => kvStore.delete(key),
  };

  const env = { MOUNTS_KV: mockKv };
  const userId = 'user_del_spec';

  await saveUserMount(env, userId, {
    id: 'mount_alpha',
    filename: 'alpha.mkv',
    title: 'Alpha',
    upstream_url: 'https://cdn.example.com/alpha.mkv',
    size_bytes: 1000,
    content_type: 'video/x-matroska',
    created_at: Date.now(),
    etag: 'etag_alpha',
  });

  await saveUserMount(env, userId, {
    id: 'mount_beta',
    filename: 'beta.mkv',
    title: 'Beta',
    upstream_url: 'https://cdn.example.com/beta.mkv',
    size_bytes: 2000,
    content_type: 'video/x-matroska',
    created_at: Date.now(),
    etag: 'etag_beta',
  });

  // Delete by id
  const deletedById = await deleteUserMount(env, userId, 'mount_alpha');
  assert.strictEqual(deletedById, true);

  // Delete by filename
  const deletedByName = await deleteUserMount(env, userId, 'beta.mkv');
  assert.strictEqual(deletedByName, true);

  // Delete non-existent
  const deletedNone = await deleteUserMount(env, userId, 'gamma.mkv');
  assert.strictEqual(deletedNone, false);

  const finalMounts = await getUserMounts(env, userId);
  assert.strictEqual(finalMounts.length, 0);
});

// Test 21: MountManager clearUserMounts deletes key from KV
await runTest('21. Test MountManager clearUserMounts deletes key from KV', async () => {
  const deletedKeys = [];
  const mockKv = {
    get: async () => null,
    put: async () => {},
    delete: async (key) => { deletedKeys.push(key); },
  };

  const env = { MOUNTS_KV: mockKv };
  const userId = 'user_clear_spec';

  await clearUserMounts(env, userId);
  assert.ok(deletedKeys.includes('mounts:user_clear_spec'));
});

// Test 22: MountManager class methods parity
await runTest('22. Test MountManager class static and instance methods', async () => {
  assert.strictEqual(typeof MountManager.getUserMounts, 'function');
  assert.strictEqual(typeof MountManager.saveUserMount, 'function');
  assert.strictEqual(typeof MountManager.deleteUserMount, 'function');
  assert.strictEqual(typeof MountManager.clearUserMounts, 'function');
  assert.strictEqual(typeof MountManager.invalidateCache, 'function');

  const instance = new MountManager();
  assert.strictEqual(typeof instance.getUserMounts, 'function');
  assert.strictEqual(typeof instance.saveUserMount, 'function');
  assert.strictEqual(typeof instance.deleteUserMount, 'function');
  assert.strictEqual(typeof instance.clearUserMounts, 'function');
  assert.strictEqual(typeof instance.invalidateCache, 'function');
});

// Test 23: warmStreamStorage helper dispatches head 32KB and tail 64KB background fetches
await runTest('23. Test warmStreamStorage helper dispatches head 32KB and tail 64KB background fetches', async () => {
  const recordedWarmers = [];
  const prevFetch = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    const headers = init.headers || {};
    const range = headers instanceof Headers ? headers.get('Range') : headers.Range || headers.range;
    const ua = headers instanceof Headers ? headers.get('User-Agent') : headers['User-Agent'] || headers['user-agent'];
    recordedWarmers.push({ url, range, ua });
    return new Response('chunk', { status: 206, headers: { 'Content-Range': 'bytes 0-10/1000' } });
  };

  try {
    // 23a: Large file (>64KB) triggers both head (0-32767) and tail (64KB)
    const largeSize = 100_000_000;
    warmStreamStorage('https://cdn.example.com/test_video.mkv', largeSize);

    assert.strictEqual(recordedWarmers.length, 2, 'Expected 2 background fetches for large file');
    assert.strictEqual(recordedWarmers[0].range, 'bytes=0-32767', 'Expected head 32KB fetch');
    assert.strictEqual(recordedWarmers[0].ua, 'CloudStream-Edge-Warmer/1.0', 'Expected CloudStream-Edge-Warmer User-Agent');
    assert.strictEqual(
      recordedWarmers[1].range,
      `bytes=${largeSize - 65536}-${largeSize - 1}`,
      'Expected tail 64KB fetch'
    );
    assert.strictEqual(recordedWarmers[1].ua, 'CloudStream-Edge-Warmer/1.0');

    // 23b: Small file (<=64KB) triggers only head fetch
    recordedWarmers.length = 0;
    const smallSize = 40_000;
    warmStreamStorage('https://cdn.example.com/small_video.mkv', smallSize);

    assert.strictEqual(recordedWarmers.length, 1, 'Expected only head fetch for size <= 64KB');
    assert.strictEqual(recordedWarmers[0].range, 'bytes=0-32767');
  } finally {
    globalThis.fetch = prevFetch;
  }
});

// Test 24: POST /api/mount triggers storage pre-warming
await runTest('24. Test POST /api/mount triggers storage pre-warming', async () => {
  const recordedWarmers = [];
  const prevFetch = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    const headers = init.headers || {};
    const range = headers instanceof Headers ? headers.get('Range') : headers.Range || headers.range;
    const ua = headers instanceof Headers ? headers.get('User-Agent') : headers['User-Agent'] || headers['user-agent'];
    if (ua === 'CloudStream-Edge-Warmer/1.0') {
      recordedWarmers.push({ url, range, ua });
      return new Response('warm', { status: 206 });
    }
    return prevFetch(input, init);
  };

  try {
    const req = new Request('https://edge.cloudstream.local/api/mount/user_warm_test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: 'https://cdn.upstream.com/streams/WarmingTest.2024.mkv',
        size_bytes: 50_000_000,
        filename: 'WarmingTest.2024.mkv',
      }),
    });
    const res = await worker.fetch(req, mockEnv, mockCtx);
    assert.strictEqual(res.status, 200);

    assert.strictEqual(recordedWarmers.length, 2, 'Expected head and tail pre-warming fetches on mount');
    assert.strictEqual(recordedWarmers[0].range, 'bytes=0-32767');
    assert.strictEqual(recordedWarmers[1].range, `bytes=${50_000_000 - 65536}-${50_000_000 - 1}`);
  } finally {
    globalThis.fetch = prevFetch;
  }
});

// Test 25: isGoogleCdn helper detects all specified Google CDN domains and rejects others
await runTest('25. Test isGoogleCdn helper detects Google CDN domains correctly', async () => {
  assert.strictEqual(isGoogleCdn('https://video-downloads.googleusercontent.com/test_video'), true);
  assert.strictEqual(isGoogleCdn('https://doc-0k-9k-docs.googleusercontent.com/download'), true);
  assert.strictEqual(isGoogleCdn('https://googleusercontent.com/stream'), true);
  assert.strictEqual(isGoogleCdn('https://drive.google.com/uc?id=12345'), true);
  assert.strictEqual(isGoogleCdn('https://photos.google.com/u/0/video'), true);
  assert.strictEqual(isGoogleCdn('https://rr1---sn-4g5ednks.googlevideo.com/videoplayback?id=abc'), true);
  assert.strictEqual(isGoogleCdn('https://googlevideo.com/videoplayback'), true);

  // Non-Google domains
  assert.strictEqual(isGoogleCdn('https://cdn.upstream.com/streams/video.mkv'), false);
  assert.strictEqual(isGoogleCdn('https://rapid-stream-1234.trycloudflare.com/dav/video.mkv'), false);
  assert.strictEqual(isGoogleCdn('https://example.com/movie.mp4'), false);
  assert.strictEqual(isGoogleCdn(''), false);
});

// Test 26: WebDAV GET on mounted Google CDN stream forces proxying (no 302 redirect)
await runTest('26. Test WebDAV GET on Google CDN stream forces proxying bypassing 302 redirect', async () => {
  // Mount a stream from Google CDN
  const mountReq = new Request('https://edge.cloudstream.local/api/mount/user_google_test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: 'https://video-downloads.googleusercontent.com/sample_video',
      filename: 'SampleGoogleMovie.mp4',
      size_bytes: 107374182400,
      content_type: 'video/mp4',
    }),
  });
  const mountRes = await worker.fetch(mountReq, mockEnv, mockCtx);
  assert.strictEqual(mountRes.status, 200);

  // Normal GET request without ?proxy=1 should be forced into proxy mode (not 302 redirect!)
  const getReq = new Request('https://edge.cloudstream.local/dav/user_google_test/SampleGoogleMovie.mp4', {
    method: 'GET',
    headers: { Range: 'bytes=0-15' },
  });
  const getRes = await worker.fetch(getReq, mockEnv, mockCtx);
  assert.notStrictEqual(getRes.status, 302, 'GET on Google CDN URL must NOT return 302 redirect');
  assert.strictEqual(getRes.status, 206, 'GET on Google CDN URL must return proxied response');
  assert.strictEqual(getRes.headers.get('Accept-Ranges'), 'none');
  assert.strictEqual(getRes.headers.get('Content-Type'), 'video/mp4');
});

// Test 27: Proxy response forces Content-Type to video/mp4 or inferred video MIME type
await runTest('27. Test proxy response forces video Content-Type overriding octet-stream and text/html', async () => {
  // 27a: Mount with application/octet-stream and upstream returning text/html
  const mountReqHtml = new Request('https://edge.cloudstream.local/api/mount/user_google_mime', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: 'https://video-downloads.googleusercontent.com/test_video?return_html=1',
      filename: 'MovieHtml.mkv',
      size_bytes: 50000,
      content_type: 'application/octet-stream',
    }),
  });
  await worker.fetch(mountReqHtml, mockEnv, mockCtx);

  const getReqHtml = new Request('https://edge.cloudstream.local/dav/user_google_mime/MovieHtml.mkv', {
    method: 'GET',
  });
  const getResHtml = await worker.fetch(getReqHtml, mockEnv, mockCtx);
  assert.strictEqual(getResHtml.status, 200);
  assert.strictEqual(getResHtml.headers.get('Content-Type'), 'video/x-matroska', 'Should infer video/x-matroska from .mkv');
  assert.strictEqual(getResHtml.headers.get('Accept-Ranges'), 'none', 'Google CDN proxy response must advertise Accept-Ranges: none');

  // 27b: Mount with unknown extension and upstream returning application/octet-stream -> defaults to video/mp4
  const mountReqOctet = new Request('https://edge.cloudstream.local/api/mount/user_google_mime', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: 'https://video-downloads.googleusercontent.com/test_video?return_octet=1',
      filename: 'ClipUnknown',
      size_bytes: 50000,
      content_type: 'application/octet-stream',
    }),
  });
  await worker.fetch(mountReqOctet, mockEnv, mockCtx);

  const getReqOctet = new Request('https://edge.cloudstream.local/dav/user_google_mime/ClipUnknown.mkv', {
    method: 'GET',
  });
  const getResOctet = await worker.fetch(getReqOctet, mockEnv, mockCtx);
  assert.strictEqual(getResOctet.status, 200);
  assert.strictEqual(getResOctet.headers.get('Content-Type'), 'video/x-matroska');
  assert.strictEqual(getResOctet.headers.get('Accept-Ranges'), 'none');
});

// Test 28: HEAD on mounted Google CDN stream returns synthetic 200 OK
await runTest('28. Test HEAD on mounted Google CDN stream returns synthetic 200 OK', async () => {
  const headReq = new Request('https://edge.cloudstream.local/dav/user_google_test/SampleGoogleMovie.mp4', {
    method: 'HEAD',
  });
  const headRes = await worker.fetch(headReq, mockEnv, mockCtx);
  assert.strictEqual(headRes.status, 200, 'HEAD must return 200 OK synthetic response');
  assert.strictEqual(headRes.headers.get('Accept-Ranges'), 'none');
  assert.strictEqual(headRes.headers.get('Content-Type'), 'video/mp4');
  assert.strictEqual(headRes.headers.get('Content-Length'), '107374182400');
  assert.strictEqual(headRes.headers.get('DAV'), '1, 2');
});

// Test 29: HEAD on tunnel fallback never redirects Google CDN URLs and returns synthetic 200 OK
await runTest('29. Test HEAD on tunnel fallback never redirects Google CDN URLs', async () => {
  // If target filename is a Google CDN URL or tunnel URL points to Google CDN
  const headReq = new Request(
    'https://edge.cloudstream.local/dav/active_user/https%3A%2F%2Fvideo-downloads.googleusercontent.com%2Fstream.mp4',
    {
      method: 'HEAD',
    }
  );
  const headRes = await worker.fetch(headReq, mockEnv, mockCtx);
  assert.strictEqual(headRes.status, 200, 'HEAD must return synthetic 200 OK for Google CDN target');
  assert.strictEqual(headRes.headers.get('Accept-Ranges'), 'bytes');
  assert.strictEqual(headRes.headers.get('Content-Type'), 'video/mp4');
  assert.strictEqual(headRes.headers.get('Content-Length'), '107374182400');
});

// Test 30: GET on Google CDN stream with Range: bytes=0- synthesizes HTTP 206 with content-range
await runTest('30. Test GET on Google CDN stream with Range: bytes=0- synthesizes HTTP 206 with content-range', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/user_google_test/SampleGoogleMovie.mp4', {
    method: 'GET',
    headers: {
      Range: 'bytes=0-',
    },
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 206, 'Must synthesize HTTP 206 Partial Content when Range is requested');
  assert.strictEqual(res.headers.get('Accept-Ranges'), 'none');
  const contentRange = res.headers.get('Content-Range');
  assert.ok(contentRange, 'Content-Range header must be present on 206 response');
  assert.ok(
    contentRange.startsWith('bytes 0-'),
    `Expected Content-Range starting with 'bytes 0-', got: ${contentRange}`
  );
  assert.strictEqual(res.headers.get('Content-Length'), '107374182400');
});

// Test 31: GET on Google CDN stream without Range returns HTTP 200 with NO content-range
await runTest('31. Test GET on Google CDN stream without Range returns HTTP 200 with NO content-range', async () => {
  const req = new Request('https://edge.cloudstream.local/dav/user_google_test/SampleGoogleMovie.mp4', {
    method: 'GET',
  });
  const res = await worker.fetch(req, mockEnv, mockCtx);

  assert.strictEqual(res.status, 200, 'Must return HTTP 200 OK when no Range header is sent');
  assert.strictEqual(res.headers.get('Accept-Ranges'), 'none');
  assert.strictEqual(res.headers.get('Content-Range'), null, 'Content-Range must NEVER be present on HTTP 200 OK (RFC 9110 Section 14.4)');
  assert.strictEqual(res.headers.get('Content-Length'), '107374182400');
});

// Test 32: HEAD on Google CDN stream returns accept-ranges: none
await runTest('32. Test HEAD on Google CDN stream returns accept-ranges: none', async () => {
  const mountReq = new Request('https://edge.cloudstream.local/api/mount/user_google_mkv_test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: 'https://video-downloads.googleusercontent.com/4k_remux.mkv',
      filename: 'Remux4K.mkv',
      size_bytes: 20000000000,
    }),
  });
  await worker.fetch(mountReq, mockEnv, mockCtx);

  const headReq = new Request('https://edge.cloudstream.local/dav/user_google_mkv_test/Remux4K.mkv', {
    method: 'HEAD',
  });
  const headRes = await worker.fetch(headReq, mockEnv, mockCtx);

  assert.strictEqual(headRes.status, 200);
  assert.strictEqual(headRes.headers.get('Accept-Ranges'), 'none', 'Google CDN HEAD must return Accept-Ranges: none');
  assert.strictEqual(headRes.headers.get('Content-Type'), 'video/x-matroska', 'Must infer video/x-matroska from .mkv');
  assert.strictEqual(headRes.headers.get('Content-Length'), '20000000000');
});

console.log(`\nAll tests passed successfully! Total: ${testsPassed}`);
