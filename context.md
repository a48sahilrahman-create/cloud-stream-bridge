# CloudStream WebDAV Bridge — Master AI Context

> **Executive Summary**: High-velocity cloud-to-cloud WebDAV streaming proxy bypassing 50 Mbps home internet bottlenecks for 50-100GB 4K UHD Remuxes with 0 bytes local disk storage. Modular domain sub-specs reside in `context/`.

---

## 1. Project Identity & Execution Environment

- **Project Name**: CloudStream WebDAV Bridge
- **Repository Root**: `C:\Users\sahil\workspaces\cloud-stream-bridge`
- **Active Endpoints / URLs**: `http://localhost:7860` (Local/Docker), `https://*.trycloudflare.com` (Cloudflare Tunnel), Hugging Face Space Docker
- **Ports & Protocols**: Port `7860`, Protocols `HTTP/1.1`, `RFC 4918 WebDAV (DAV: 1, 2)`, `RFC 7233 HTTP Range`
- **Execution Surfaces**: FastAPI / Uvicorn, Google Colab (`colab_run.py`), Docker (`Dockerfile`), Standalone (`standalone_server.py`)
- **Key Client Integrations**: CX File Explorer (Android/Fire TV), VLC Media Player, Nova Video Player, Kodi

---

## 2. Modular Architecture Specifications

Inspect dedicated modular sub-specs under `context/`:
- [Architecture & Directory Map](context/architecture-map.md) — System modules, streaming pipeline data flow, routing, and cloud deployment topology.
- [Invariants & Safety Guardrails](context/invariants.md) — 0-byte local disk storage, RFC 4918 WebDAV compliance, RFC 7233 byte-range rules, and <10ms seek cancellation.
- [Key File Responsibilities](context/file-responsibilities.md) — Source code roles, controller contracts, and component boundaries.
- [API & Protocol Contracts](context/api-contracts.md) — WebDAV methods (OPTIONS, PROPFIND, HEAD, GET), REST mounting endpoints, and XML schemas.
- [Current Project State](context/project-state.md) — Verified milestones, resolved CX File Explorer defects, and active public streaming tests.

---

## 3. Standard Commands for AI Agents

```bash
# 1. Run canonical test suite
python -m pytest tests/test_webdav.py

# 2. Syntax validation
python -m py_compile main.py webdav_engine.py range_proxy.py stream_probe.py standalone_server.py

# 3. Pack codebase with Repomix (AST compressed)
repomix --compress
```
