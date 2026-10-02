"""
Audit Test Script for WebDAV Authentication and Protocol Handling
Tests:
1. Requests with and without Authorization header (valid, invalid, none).
2. PROPFIND with empty mounts vs active mounts.
3. Path variations: /, /dav, /dav/ with Depth: 0 and Depth: 1.
4. Single-file PROPFIND and HEAD.
"""

import os
import sys
import base64
import xml.etree.ElementTree as ET

# Ensure parent directory is in Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from main import app
from webdav_engine import mount_manager

client = TestClient(app)

def run_audit():
    print("=" * 70)
    print("WEBDAV AUTHENTICATION & PATH AUDIT SUITE")
    print("=" * 70)

    # Backup existing mounts
    original_mounts = dict(mount_manager.mounts)

    # -----------------------------------------------------------------
    # Test 1: Authentication Header Permutations
    # -----------------------------------------------------------------
    print("\n--- TEST 1: AUTHENTICATION HANDLING ---")
    auth_cases = [
        ("No Authorization Header", None),
        ("Basic admin:none (Suggested in UI)", "Basic " + base64.b64encode(b"admin:none").decode("ascii")),
        ("Basic wrong:password (Invalid credentials)", "Basic " + base64.b64encode(b"wrong:password").decode("ascii")),
        ("Bearer Token (Unsupported Scheme)", "Bearer eyJhbGciOiJIUzI1NiJ9.test"),
    ]

    for label, auth_hdr in auth_cases:
        headers = {"Authorization": auth_hdr} if auth_hdr else {}
        resp_opt = client.options("/dav/", headers=headers)
        resp_pf = client.request("PROPFIND", "/dav/", headers={"Depth": "1", **headers})
        www_auth = resp_opt.headers.get("WWW-Authenticate")
        print(f"[{label}]")
        print(f"  OPTIONS /dav/   -> Status: {resp_opt.status_code} | WWW-Authenticate: {www_auth}")
        print(f"  PROPFIND /dav/  -> Status: {resp_pf.status_code}  | Content-Type: {resp_pf.headers.get('Content-Type')}")

    # -----------------------------------------------------------------
    # Test 2: Empty Mounts
    # -----------------------------------------------------------------
    print("\n--- TEST 2: PROPFIND WITH EMPTY MOUNTS ---")
    mount_manager.mounts = {}
    paths = ["/", "/dav", "/dav/"]
    for path in paths:
        for depth in ["0", "1"]:
            resp = client.request("PROPFIND", path, headers={"Depth": depth})
            tree = ET.fromstring(resp.content)
            responses = tree.findall("{DAV:}response")
            hrefs = [r.findtext("{DAV:}href") for r in responses]
            cl = resp.headers.get("Content-Length")
            print(f"  Path: {path:<6} Depth: {depth} -> Status: {resp.status_code} | Elements: {len(responses)} | CL: {cl} | Hrefs: {hrefs}")

    # -----------------------------------------------------------------
    # Test 3: Active Mounts
    # -----------------------------------------------------------------
    print("\n--- TEST 3: PROPFIND WITH ACTIVE MOUNTS ---")
    mount_manager.add_mount(
        movie_id="test_audit_1",
        filename="Dune.Part.Two.2024.2160p.UHD.Remux.mkv",
        upstream_url="https://storage.example.com/dune2.mkv",
        total_bytes=75161927680,
        content_type="video/x-matroska",
        formatted_size="70.00 GB",
        title="Dune: Part Two (2024)"
    )

    for path in paths:
        for depth in ["0", "1"]:
            resp = client.request("PROPFIND", path, headers={"Depth": depth})
            tree = ET.fromstring(resp.content)
            responses = tree.findall("{DAV:}response")
            hrefs = [r.findtext("{DAV:}href") for r in responses]
            cl = resp.headers.get("Content-Length")
            contains_movie = any("Dune.Part.Two" in h for h in hrefs)
            print(f"  Path: {path:<6} Depth: {depth} -> Status: {resp.status_code} | Elements: {len(responses)} | CL: {cl} | Contains File: {contains_movie} | Hrefs: {hrefs}")

    # -----------------------------------------------------------------
    # Test 4: Single-File PROPFIND and HEAD
    # -----------------------------------------------------------------
    print("\n--- TEST 4: SINGLE FILE PROPFIND & HEAD ---")
    filename = "Dune.Part.Two.2024.2160p.UHD.Remux.mkv"
    resp_sf = client.request("PROPFIND", f"/dav/{filename}", headers={"Depth": "0"})
    tree_sf = ET.fromstring(resp_sf.content)
    sf_responses = tree_sf.findall("{DAV:}response")
    prop = sf_responses[0].find("{DAV:}propstat/{DAV:}prop")
    file_len = prop.findtext("{DAV:}getcontentlength")
    mime = prop.findtext("{DAV:}getcontenttype")
    print(f"  PROPFIND /dav/{filename} -> Status: {resp_sf.status_code} | Elements: {len(sf_responses)} | Size: {file_len} | MIME: {mime}")

    resp_head = client.head(f"/dav/{filename}")
    print(f"  HEAD /dav/{filename}     -> Status: {resp_head.status_code} | Accept-Ranges: {resp_head.headers.get('Accept-Ranges')} | Content-Length: {resp_head.headers.get('Content-Length')}")

    # Restore mounts
    mount_manager.mounts = original_mounts
    mount_manager.save()
    print("\n[+] Audit tests completed. Original mounts restored.")

if __name__ == "__main__":
    run_audit()
