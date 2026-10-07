"""
Diagnostics Script: 1DM vs Video Player Streaming Latency Breakdown
Simulates and measures:
1. 1DM Multi-Threaded Parallel Download Pattern (8 connections, 4MB chunks)
2. Video Player (ExoPlayer/VLC) Serial Probe Pattern:
   - Probe 1: EBML Header (bytes=0-1023)
   - Probe 2: EOF Tail Seek (Cues/SeekHead) (bytes=-65536)
   - Probe 3: Track Info (bytes=1024-32767)
   - Probe 4: Initial Playback Buffer (bytes=32768-1048575)
3. Quantitative Comparison:
   - Time-to-First-Byte (TTFB)
   - Time-to-Playable-Buffer (TTPB)
   - Protocol Round-Trip Multiplication Factor
"""

import time
import asyncio
from typing import Dict, Any, List

class StreamingSimulator:
    def __init__(
        self,
        base_rtt_ms: float = 60.0,          # Base network ping to edge/origin
        tls_handshake_ms: float = 120.0,    # TLS 1.3 handshake time
        worker_kv_lookup_ms: float = 35.0,  # Cloudflare Worker KV lookup
        origin_ttfb_ms: float = 150.0,      # Origin server response generation time
        single_stream_mbps: float = 18.0,   # Single TCP stream speed (throttled/slow-start)
        multi_stream_mbps: float = 120.0,   # Aggregate 1DM speed (8 parallel streams)
    ):
        self.base_rtt = base_rtt_ms / 1000.0
        self.tls_handshake = tls_handshake_ms / 1000.0
        self.worker_kv = worker_kv_lookup_ms / 1000.0
        self.origin_ttfb = origin_ttfb_ms / 1000.0
        self.single_speed = (single_stream_mbps * 1024 * 1024) / 8.0  # bytes/sec
        self.multi_speed = (multi_stream_mbps * 1024 * 1024) / 8.0   # bytes/sec

    def simulate_1dm_download(self, initial_buffer_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
        """
        1DM opens 8 parallel connections immediately to upstream URL.
        No WebDAV, no KV lookup, no container probing, no tail seeks.
        """
        start = time.perf_counter()
        # Parallel TCP/TLS connection setup (all 8 establish in parallel)
        connect_time = self.base_rtt + self.tls_handshake + self.origin_ttfb
        # Data transfer time across 8 streams
        transfer_time = initial_buffer_bytes / self.multi_speed
        total_time = connect_time + transfer_time

        return {
            "mode": "1DM Multi-threaded Raw Download",
            "connections": 8,
            "probes_count": 0,
            "connect_and_handshake_ms": round(connect_time * 1000, 2),
            "transfer_time_ms": round(transfer_time * 1000, 2),
            "total_initial_ready_ms": round(total_time * 1000, 2),
            "effective_speed_mbps": round((self.multi_speed * 8) / (1024 * 1024), 2),
            "stalls": 0,
        }

    def simulate_player_direct_cdn(self, initial_buffer_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
        """
        Player connects directly to origin CDN (no bridge).
        Must still perform serial container probes.
        """
        start = time.perf_counter()
        timeline = []
        elapsed = 0.0

        # Probe 1: EBML Header (0-1023)
        t_probe1 = (self.base_rtt + self.tls_handshake + self.origin_ttfb) + (1024 / self.single_speed)
        elapsed += t_probe1
        timeline.append(("Probe 1 (EBML Header 1KB)", round(t_probe1 * 1000, 1)))

        # Probe 2: EOF Tail Seek (Cues/Index) (new connection or re-request)
        t_probe2 = (self.base_rtt + self.origin_ttfb) + (65536 / self.single_speed)
        elapsed += t_probe2
        timeline.append(("Probe 2 (EOF Cues Seek 64KB)", round(t_probe2 * 1000, 1)))

        # Probe 3: Track Info (1024-32767)
        t_probe3 = (self.base_rtt + self.origin_ttfb) + (31744 / self.single_speed)
        elapsed += t_probe3
        timeline.append(("Probe 3 (Track Info 32KB)", round(t_probe3 * 1000, 1)))

        # Playback Stream: Continuous initial playback buffer (e.g. 10MB of 4K video)
        t_buffer = (self.base_rtt + self.origin_ttfb) + (initial_buffer_bytes / self.single_speed)
        elapsed += t_buffer
        timeline.append(("Playback Stream (10MB Video Buffer)", round(t_buffer * 1000, 1)))

        return {
            "mode": "Direct CDN Video Player",
            "connections": "1 (Serial)",
            "probes_count": 3,
            "total_initial_ready_ms": round(elapsed * 1000, 2),
            "timeline": timeline,
            "effective_speed_mbps": round((self.single_speed * 8) / (1024 * 1024), 2),
        }

    def simulate_player_through_cloudstream_302(self, initial_buffer_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
        """
        Player connects through Cloudflare Worker WebDAV with HTTP 302 Redirection.
        Each probe hits Worker -> Worker does KV lookup -> Returns 302 -> Player redirects to Origin.
        """
        timeline = []
        elapsed = 0.0

        # Each probe incurs:
        # 1. Player -> Worker request + TLS (first time) + Worker KV lookup + 302 Response
        # 2. Player -> Origin CDN request + Origin TLS (if new socket) + Origin TTFB + transfer
        worker_hop = self.base_rtt + self.worker_kv

        # Probe 1: EBML Header
        t_p1 = (self.base_rtt + self.tls_handshake + self.worker_kv) + (self.base_rtt + self.tls_handshake + self.origin_ttfb) + (1024 / self.single_speed)
        elapsed += t_p1
        timeline.append(("Probe 1 (Worker 302 -> Origin EBML 1KB)", round(t_p1 * 1000, 1)))

        # Probe 2: EOF Tail Seek (Cues)
        # Player makes new GET to Worker -> Worker KV -> 302 -> Origin
        t_p2 = (self.base_rtt + self.worker_kv) + (self.base_rtt + self.origin_ttfb) + (65536 / self.single_speed)
        elapsed += t_p2
        timeline.append(("Probe 2 (Worker 302 -> Origin EOF Cues 64KB)", round(t_p2 * 1000, 1)))

        # Probe 3: Track Info
        t_p3 = (self.base_rtt + self.worker_kv) + (self.base_rtt + self.origin_ttfb) + (31744 / self.single_speed)
        elapsed += t_p3
        timeline.append(("Probe 3 (Worker 302 -> Origin Track Info 32KB)", round(t_p3 * 1000, 1)))

        # Probe 4: Initial Playback Stream
        t_stream = (self.base_rtt + self.worker_kv) + (self.base_rtt + self.origin_ttfb) + (initial_buffer_bytes / self.single_speed)
        elapsed += t_stream
        timeline.append(("Playback Stream (Worker 302 -> Origin 10MB Video)", round(t_stream * 1000, 1)))

        return {
            "mode": "CloudStream Bridge (302 Redirect)",
            "connections": "Serial + Double-Hop (Worker -> 302 -> CDN)",
            "probes_count": 3,
            "total_initial_ready_ms": round(elapsed * 1000, 2),
            "timeline": timeline,
            "latency_penalty_vs_1dm_ms": round((elapsed - (self.base_rtt + self.tls_handshake + self.origin_ttfb + (initial_buffer_bytes / self.multi_speed))) * 1000, 2),
        }

    def simulate_player_through_cloudstream_proxy_or_google(self, initial_buffer_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
        """
        Player connects through Cloudflare Worker Edge Proxy or Google CDN mode.
        Worker proxies data. If Google CDN ignores Range, tail seek fails or discards data.
        """
        timeline = []
        elapsed = 0.0

        # Probe 1: EBML Header
        t_p1 = (self.base_rtt + self.tls_handshake) + self.worker_kv + (self.base_rtt + self.origin_ttfb) + (1024 / self.single_speed)
        elapsed += t_p1
        timeline.append(("Probe 1 (Worker Edge Proxy EBML 1KB)", round(t_p1 * 1000, 1)))

        # Probe 2: EOF Tail Seek (Cues)
        # On Google CDN or non-range upstream:
        # Worker has to either return 200 OK from byte 0 or abort.
        # This causes ExoPlayer demuxer retry or parsing stall (typically 2-4 seconds timeout/retry).
        t_p2_stall = (self.base_rtt + self.origin_ttfb) + 2.5  # 2.5s stall/retry penalty
        elapsed += t_p2_stall
        timeline.append(("Probe 2 (EOF Cues Seek STALL / Range Invariant Retry)", round(t_p2_stall * 1000, 1)))

        # Probe 3: Video Stream Start (byte 0 to 10MB)
        t_stream = (self.base_rtt + self.origin_ttfb) + (initial_buffer_bytes / self.single_speed)
        elapsed += t_stream
        timeline.append(("Playback Stream (Pipelined Video Stream 10MB)", round(t_stream * 1000, 1)))

        return {
            "mode": "CloudStream Bridge (Edge Proxy / Google CDN)",
            "connections": "Serverless Proxy + Probe Retries",
            "probes_count": 3,
            "total_initial_ready_ms": round(elapsed * 1000, 2),
            "timeline": timeline,
        }

def run_diagnostics():
    sim = StreamingSimulator()
    print("=" * 70)
    print("STREAMING PERFORMANCE & LATENCY DIAGNOSTICS: 1DM VS VIDEO PLAYER")
    print("=" * 70)

    res_1dm = sim.simulate_1dm_download()
    print(f"\n1. [1DM Download Manager]:")
    print(f"   - Concurrency: {res_1dm['connections']} parallel streams")
    print(f"   - Effective Bandwidth: {res_1dm['effective_speed_mbps']} Mbps")
    print(f"   - Initial 10MB Buffer Time: {res_1dm['total_initial_ready_ms']} ms ({res_1dm['total_initial_ready_ms']/1000:.2f}s)")
    print(f"   - Bottlenecks: NONE (monotonic TCP saturation)")

    res_direct = sim.simulate_player_direct_cdn()
    print(f"\n2. [Video Player Direct to CDN (No Bridge)]:")
    print(f"   - Concurrency: Single TCP Stream")
    print(f"   - Effective Bandwidth: {res_direct['effective_speed_mbps']} Mbps (single stream throttled)")
    print(f"   - Total Probes: {res_direct['probes_count']}")
    print(f"   - Initial 10MB Buffer Time: {res_direct['total_initial_ready_ms']} ms ({res_direct['total_initial_ready_ms']/1000:.2f}s)")
    for name, dur in res_direct['timeline']:
        print(f"     * {name}: {dur} ms")

    res_302 = sim.simulate_player_through_cloudstream_302()
    print(f"\n3. [Video Player via CloudStream Bridge (302 Redirect)]:")
    print(f"   - Architecture: Double-Hop (Player -> Worker KV -> 302 -> Origin CDN)")
    print(f"   - Initial 10MB Buffer Time: {res_302['total_initial_ready_ms']} ms ({res_302['total_initial_ready_ms']/1000:.2f}s)")
    print(f"   - Latency Penalty vs 1DM: +{res_302['latency_penalty_vs_1dm_ms']} ms (+{res_302['latency_penalty_vs_1dm_ms']/1000:.2f}s lag)")
    for name, dur in res_302['timeline']:
        print(f"     * {name}: {dur} ms")

    res_proxy = sim.simulate_player_through_cloudstream_proxy_or_google()
    print(f"\n4. [Video Player via CloudStream Bridge (Edge Proxy / Google CDN)]:")
    print(f"   - Architecture: Serverless Edge Proxy / Discard Stream")
    print(f"   - Initial 10MB Buffer Time: {res_proxy['total_initial_ready_ms']} ms ({res_proxy['total_initial_ready_ms']/1000:.2f}s)")
    for name, dur in res_proxy['timeline']:
        print(f"     * {name}: {dur} ms")

    print("\n" + "=" * 70)
    print("SUMMARY VERDICT:")
    print("=" * 70)
    print(f"• 1DM Ready Time:        {res_1dm['total_initial_ready_ms']/1000:.2f}s  (Super fast, multi-threaded)")
    print(f"• CloudStream 302 Ready: {res_302['total_initial_ready_ms']/1000:.2f}s  (Serial probe storms + double-hop 302s)")
    print(f"• CloudStream Proxy Ready:{res_proxy['total_initial_ready_ms']/1000:.2f}s (Tail seek thrashing + buffer starvation)")
    print("=" * 70)

if __name__ == "__main__":
    run_diagnostics()
