"""
CX File Explorer & WebDAV Client Verification Suite
Simulates authentic CX File Explorer / Android TV WebDAV client requests against
CloudStream WebDAV Bridge (local or remote tunnel URL).
"""

import sys
import os
import argparse
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict, Any
import httpx

# Namespaces used in RFC 4918 WebDAV
DAV_NS = {"D": "DAV:"}


def verify_cx_client(base_url: str = "http://localhost:7860", timeout: float = 15.0) -> bool:
    """
    Simulates CX File Explorer client requests against the target WebDAV server.
    Returns True if all checks succeed, False otherwise.
    """
    base_url = base_url.rstrip("/")
    print("=" * 70)
    print(f"CX File Explorer Verification Target: {base_url}")
    print("=" * 70)

    # Use follow_redirects=False to catch unwanted 307/308 redirects
    client = httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=False, verify=False)
    all_passed = True

    try:
        # -------------------------------------------------------------
        # 1. OPTIONS / -> Status 200, DAV: 1, Allow contains PROPFIND, Content-Length: 0
        # -------------------------------------------------------------
        print("\n[1/9] Verifying OPTIONS / (Root WebDAV Discovery)...")
        r_opt_root = client.options("/")
        print(f"      Status: {r_opt_root.status_code}")
        print(f"      DAV Header: {r_opt_root.headers.get('DAV')}")
        print(f"      Allow: {r_opt_root.headers.get('Allow')}")
        print(f"      Content-Length: {r_opt_root.headers.get('Content-Length')}")

        dav_hdr = r_opt_root.headers.get("DAV", "")
        allow_hdr = r_opt_root.headers.get("Allow", "")
        cl_hdr = r_opt_root.headers.get("Content-Length", "")

        if r_opt_root.status_code != 200:
            print(f"  [!] FAILED: Expected status 200, got {r_opt_root.status_code}")
            all_passed = False
        elif "1" not in dav_hdr:
            print(f"  [!] FAILED: 'DAV: 1' missing in headers: {dav_hdr}")
            all_passed = False
        elif "PROPFIND" not in allow_hdr.upper():
            print(f"  [!] FAILED: 'PROPFIND' missing in Allow header: {allow_hdr}")
            all_passed = False
        elif cl_hdr != "0":
            print(f"  [!] FAILED: Expected Content-Length: 0, got {cl_hdr}")
            all_passed = False
        else:
            print("  [+] PASSED: OPTIONS / complies with RFC 4918 WebDAV discovery.")

        # -------------------------------------------------------------
        # 2. OPTIONS /dav and OPTIONS /dav/ -> Status 200, no redirects (status != 307/308)
        # -------------------------------------------------------------
        print("\n[2/9] Verifying OPTIONS /dav and /dav/ (No Redirects)...")
        for path in ["/dav", "/dav/"]:
            r_opt_dav = client.options(path)
            print(f"      {path} -> Status: {r_opt_dav.status_code}")
            if r_opt_dav.status_code in (301, 302, 307, 308):
                print(f"  [!] FAILED: OPTIONS {path} caused HTTP redirect {r_opt_dav.status_code}!")
                all_passed = False
            elif r_opt_dav.status_code != 200:
                print(f"  [!] FAILED: Expected 200 for OPTIONS {path}, got {r_opt_dav.status_code}")
                all_passed = False
            else:
                print(f"  [+] PASSED: OPTIONS {path} returned 200 without redirects.")

        # -------------------------------------------------------------
        # 3. PROPFIND / with Depth: 0 and Depth: 1 -> Status 207, XML contains <D:multistatus, Content-Length present
        # -------------------------------------------------------------
        print("\n[3/9] Verifying PROPFIND / with Depth: 0 and Depth: 1...")
        for depth in ["0", "1"]:
            r_pf_root = client.request("PROPFIND", "/", headers={"Depth": depth})
            print(f"      Depth: {depth} -> Status: {r_pf_root.status_code}, Length: {r_pf_root.headers.get('Content-Length')}")
            if r_pf_root.status_code != 207:
                print(f"  [!] FAILED: Expected status 207 for PROPFIND / (Depth {depth}), got {r_pf_root.status_code}")
                all_passed = False
            elif "<D:multistatus" not in r_pf_root.text and "multistatus" not in r_pf_root.text.lower():
                print(f"  [!] FAILED: Missing <D:multistatus in response body for Depth {depth}")
                all_passed = False
            elif not r_pf_root.headers.get("Content-Length"):
                print(f"  [!] FAILED: Missing Content-Length header for Depth {depth}")
                all_passed = False
            else:
                try:
                    ET.fromstring(r_pf_root.content)
                    print(f"  [+] PASSED: PROPFIND / (Depth {depth}) returned valid 207 Multi-Status XML.")
                except ET.ParseError as e:
                    print(f"  [!] FAILED: XML parse error for Depth {depth}: {e}")
                    all_passed = False

        # -------------------------------------------------------------
        # 4. PROPFIND /dav and PROPFIND /dav/ with Depth: 0 and Depth: 1 -> Status 207, valid XML, Content-Length present
        # -------------------------------------------------------------
        print("\n[4/9] Verifying PROPFIND /dav and /dav/ with Depth: 0 and Depth: 1...")
        for path in ["/dav", "/dav/"]:
            for depth in ["0", "1"]:
                r_pf = client.request("PROPFIND", path, headers={"Depth": depth})
                print(f"      {path} (Depth {depth}) -> Status: {r_pf.status_code}, Length: {r_pf.headers.get('Content-Length')}")
                if r_pf.status_code in (301, 302, 307, 308):
                    print(f"  [!] FAILED: PROPFIND {path} caused HTTP redirect {r_pf.status_code}!")
                    all_passed = False
                elif r_pf.status_code != 207:
                    print(f"  [!] FAILED: Expected status 207 for {path} (Depth {depth}), got {r_pf.status_code}")
                    all_passed = False
                elif "<D:multistatus" not in r_pf.text and "multistatus" not in r_pf.text.lower():
                    print(f"  [!] FAILED: Missing <D:multistatus in {path} (Depth {depth})")
                    all_passed = False
                elif not r_pf.headers.get("Content-Length"):
                    print(f"  [!] FAILED: Missing Content-Length in {path} (Depth {depth})")
                    all_passed = False
                else:
                    try:
                        ET.fromstring(r_pf.content)
                        print(f"  [+] PASSED: PROPFIND {path} (Depth {depth}) valid.")
                    except ET.ParseError as e:
                        print(f"  [!] FAILED: XML parse error for {path} (Depth {depth}): {e}")
                        all_passed = False

        # -------------------------------------------------------------
        # 5. Check if mounts exist; if mounted, do PROPFIND /dav/<filename> (single-file PROPFIND)
        # -------------------------------------------------------------
        print("\n[5/9] Checking virtual mounts & single-file PROPFIND...")
        mount_filename = None
        # Check /api/mounts
        try:
            r_mounts = client.get("/api/mounts")
            if r_mounts.status_code == 200:
                mount_list = r_mounts.json().get("mounts", [])
                if mount_list:
                    mount_filename = mount_list[0].get("filename")
                    print(f"      Discovered active mount from API: {mount_filename}")
        except Exception:
            pass

        # If not found via API, check PROPFIND /dav/ listing
        if not mount_filename:
            r_list = client.request("PROPFIND", "/dav/", headers={"Depth": "1"})
            if r_list.status_code == 207:
                try:
                    tree = ET.fromstring(r_list.content)
                    for resp in tree.findall(".//{DAV:}response"):
                        href = resp.findtext("{DAV:}href") or ""
                        cand = href.rstrip("/").split("/")[-1]
                        if cand and cand != "dav":
                            mount_filename = cand
                            print(f"      Discovered active mount from XML listing: {mount_filename}")
                            break
                except Exception:
                    pass

        if mount_filename:
            r_single = client.request("PROPFIND", f"/dav/{mount_filename}", headers={"Depth": "0"})
            print(f"      PROPFIND /dav/{mount_filename} -> Status: {r_single.status_code}")
            if r_single.status_code != 207:
                print(f"  [!] FAILED: Expected 207 for single-file PROPFIND, got {r_single.status_code}")
                all_passed = False
            else:
                try:
                    s_tree = ET.fromstring(r_single.content)
                    responses = [elem for elem in s_tree.iter() if elem.tag.endswith("response")]
                    if len(responses) != 1:
                        print(f"  [!] FAILED: Expected exactly 1 <D:response> for single file, got {len(responses)}")
                        all_passed = False
                    else:
                        print(f"  [+] PASSED: Single-file PROPFIND returned 207 with exactly 1 response element.")
                except ET.ParseError as e:
                    print(f"  [!] FAILED: Single-file XML parse error: {e}")
                    all_passed = False
        else:
            print("  [*] NOTICE: No active mounts found. Skipping steps 5-8 live stream probes.")

        # -------------------------------------------------------------
        # 6. HEAD /dav/<filename> -> Status 200, Accept-Ranges: bytes, Content-Length > 0
        # -------------------------------------------------------------
        print("\n[6/9] Verifying HEAD /dav/<filename>...")
        if mount_filename:
            r_head = client.head(f"/dav/{mount_filename}")
            print(f"      Status: {r_head.status_code}")
            print(f"      Accept-Ranges: {r_head.headers.get('Accept-Ranges')}")
            print(f"      Content-Length: {r_head.headers.get('Content-Length')}")
            cl_int = int(r_head.headers.get("Content-Length", 0))
            if r_head.status_code != 200:
                print(f"  [!] FAILED: Expected 200 for HEAD, got {r_head.status_code}")
                all_passed = False
            elif r_head.headers.get("Accept-Ranges") != "bytes":
                print(f"  [!] FAILED: Accept-Ranges header != 'bytes' ({r_head.headers.get('Accept-Ranges')})")
                all_passed = False
            elif cl_int <= 0:
                print(f"  [!] FAILED: Content-Length must be > 0, got {cl_int}")
                all_passed = False
            else:
                print("  [+] PASSED: HEAD returned 200, Accept-Ranges: bytes, Content-Length > 0.")
        else:
            print("  [*] SKIPPED: No mounted file present to HEAD.")

        # -------------------------------------------------------------
        # 7. GET /dav/<filename> with Range: bytes=0-1023 -> Status 206, exactly 1024 bytes returned
        # -------------------------------------------------------------
        print("\n[7/9] Verifying GET initial chunk (Range: bytes=0-1023)...")
        if mount_filename:
            try:
                r_get = client.get(f"/dav/{mount_filename}", headers={"Range": "bytes=0-1023"})
                print(f"      Status: {r_get.status_code}, Bytes returned: {len(r_get.content)}")
                if r_get.status_code != 206:
                    print(f"  [!] FAILED: Expected 206 Partial Content, got {r_get.status_code}")
                    all_passed = False
                elif len(r_get.content) != 1024:
                    print(f"  [!] FAILED: Expected exactly 1024 bytes, got {len(r_get.content)}")
                    all_passed = False
                else:
                    print("  [+] PASSED: GET initial chunk returned 206 with exactly 1024 bytes.")
            except Exception as e:
                print(f"  [!] FAILED: GET initial chunk error: {e}")
                all_passed = False
        else:
            print("  [*] SKIPPED: No mounted file present to test Range GET.")

        # -------------------------------------------------------------
        # 8. GET /dav/<filename> with seek Range: bytes=1048576000-1049624575 (1 MB seek) -> Status 206, 1048576 bytes
        # -------------------------------------------------------------
        print("\n[8/9] Verifying GET seek 1 MB chunk (Range: bytes=1048576000-1049624575)...")
        if mount_filename:
            try:
                r_seek = client.get(f"/dav/{mount_filename}", headers={"Range": "bytes=1048576000-1049624575"})
                print(f"      Status: {r_seek.status_code}, Bytes returned: {len(r_seek.content)}")
                if r_seek.status_code != 206:
                    print(f"  [!] FAILED: Expected 206 Partial Content for 1MB seek, got {r_seek.status_code}")
                    all_passed = False
                elif len(r_seek.content) != 1048576:
                    print(f"  [!] FAILED: Expected exactly 1048576 bytes (1 MB), got {len(r_seek.content)}")
                    all_passed = False
                else:
                    print("  [+] PASSED: 1 MB seek returned 206 with exactly 1048576 bytes.")
            except Exception as e:
                print(f"  [!] FAILED: GET seek 1MB chunk error: {e}")
                all_passed = False
        else:
            print("  [*] SKIPPED: No mounted file present to test 1 MB seek.")

        # -------------------------------------------------------------
        # 9. Parse XML using xml.etree.ElementTree to verify compliance with <D:getetag>, <D:creationdate>, <D:getlastmodified>
        # -------------------------------------------------------------
        print("\n[9/9] Verifying XML compliance with RFC 4918 properties (<D:getetag>, <D:creationdate>, <D:getlastmodified>)...")
        r_xml = client.request("PROPFIND", "/dav/", headers={"Depth": "0"})
        if r_xml.status_code != 207:
            print(f"  [!] FAILED: Expected 207, got {r_xml.status_code}")
            all_passed = False
        else:
            try:
                root = ET.fromstring(r_xml.content)
                tags = [elem.tag for elem in root.iter()]
                has_etag = any("getetag" in t for t in tags)
                has_creation = any("creationdate" in t for t in tags)
                has_lastmod = any("getlastmodified" in t for t in tags)

                print(f"      Found getetag: {has_etag}")
                print(f"      Found creationdate: {has_creation}")
                print(f"      Found getlastmodified: {has_lastmod}")

                if not has_etag:
                    print("  [!] FAILED: <D:getetag> not found in XML elements.")
                    all_passed = False
                elif not has_creation:
                    print("  [!] FAILED: <D:creationdate> not found in XML elements.")
                    all_passed = False
                elif not has_lastmod:
                    print("  [!] FAILED: <D:getlastmodified> not found in XML elements.")
                    all_passed = False
                else:
                    print("  [+] PASSED: XML elements strictly conform to RFC 4918 specification.")
            except ET.ParseError as e:
                print(f"  [!] FAILED: XML parse error: {e}")
                all_passed = False

    except Exception as ex:
        print(f"\n[!] Connection / Verification error: {ex}")
        all_passed = False
    finally:
        client.close()

    print("\n" + "=" * 70)
    if all_passed:
        print("VERIFICATION RESULT: ALL CX FILE EXPLORER CHECKS PASSED [SUCCESS]")
    else:
        print("VERIFICATION RESULT: ONE OR MORE CHECKS FAILED [FAILED]")
    print("=" * 70)
    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CX File Explorer & WebDAV Compliance Tester")
    parser.add_argument("url", nargs="?", default=os.getenv("BRIDGE_URL", "http://localhost:7860"), help="Bridge server URL (default: http://localhost:7860)")
    args = parser.parse_args()

    success = verify_cx_client(base_url=args.url)
    sys.exit(0 if success else 1)
