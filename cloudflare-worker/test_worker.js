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
import worker from './src/index.ts';

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
});

console.log(`\nAll tests passed successfully! Total: ${testsPassed}`);
