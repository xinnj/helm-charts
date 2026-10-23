#!/usr/bin/env python3
"""
LiveKit + STUN/TURN real-protocol connectivity tester.

Unlike a port scan, this performs the *actual* protocols end to end:

  Phase 1  LiveKit signaling  - open the WSS /rtc session, parse the JoinResponse,
                                prove signaling works and DISCOVER the STUN/TURN
                                servers (URLs + credentials) the server hands clients.
                                Also collect the ICE candidates the SFU advertises,
                                to compare against TURN relay addresses in Phase 5.
  Phase 2  STUN binding       - send a real Binding request and read back the
                                server-reflexive (public) address.
  Phase 3  TURN relay / UDP   - full authenticated Allocate -> CreatePermission ->
                                Send/Data: relay a random token between two
                                allocations and verify it arrives. Proves the TURN
                                server actually relays media over UDP.
  Phase 4  TURN relay / TCP   - the same relay proof, but the control channel runs
                                over TLS/TCP (turns:...?transport=tcp).
  Phase 5  ICE connectivity   - two real ICE agents (aioice) gather relay
                                candidates from each advertised TURN server and
                                run RFC 8445 connectivity checks. Both agents
                                are forced to use their relay candidate, so a
                                nominated relay<->relay pair proves ICE works
                                end to end through TURN. It also compares the
                                relay addresses with the ICE candidates the SFU
                                advertises. If TURN hands out a private relay
                                while the SFU only advertises public candidates,
                                the run fails when the client is also seen via a
                                private (SNAT) address (the usual NAT failure),
                                and warns otherwise (direct connectivity may not
                                need TURN).
  Phase 6  Direct connectivity - a real WebRTC client (aiortc) answers the SFU's
                                subscriber offer with TURN deliberately absent
                                from its ICE server list, so only host/srflx
                                candidates exist. Reaching "connected" proves a
                                client on this machine's network can use the SFU
                                without a relay.
  Phase 7  Path selection     - the same real client, now given both STUN and
                                the advertised TURN servers, so ICE itself picks
                                between a direct and a relayed candidate pair.
                                The winning path is reported.
  Phase 8  Browser path       - a real Chrome (Playwright) running the official
                                livekit-client SDK joins the room, publishes a
                                synthetic track, and reports the selected pair
                                and ICE role for every PeerConnection the
                                browser created. It prints the actual path
                                (direct/relay, candidate types and addresses,
                                private-address facts). The test runs anywhere
                                and has no network-location mode; a relay or
                                private-address path is reported, not failed.

Everything the tool tests is discovered from the LiveKit JoinResponse, so the
STUN/TURN servers and TURN credentials are exactly what a real client would use.
No secrets are hard-coded: connection details come from environment variables
(or CLI flags), and the TURN MESSAGE-INTEGRITY key is derived from the
server-issued username/credential. The browser phase injects its token through
the page driver and never prints it.

Configuration (env var  ->  CLI flag override):
    LIVEKIT_URL          --url            wss://host[:port]
    LIVEKIT_API_KEY      --api-key
    LIVEKIT_API_SECRET   --api-secret

Dependencies:
    pip install livekit-api websockets aioice aiortc playwright
    (cd browser && npm ci)   # livekit-client bundle for Phase 8

Exit code: 0 if every attempted phase passed, non-zero otherwise.
"""

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ─── Optional dependencies (fail with a helpful message) ─────────────
try:
    from livekit.api import AccessToken, VideoGrants
    from livekit.protocol.rtc import SignalResponse
    import websockets
except ImportError as exc:  # pragma: no cover - environment guard
    sys.stderr.write(
        f"Missing dependency: {exc.name}\n"
        "Install with:  pip install livekit-api websockets aioice\n"
    )
    sys.exit(3)

# aioice powers the optional Phase 5 ICE check. Without it the rest of the
# test still runs and Phase 5 reports SKIP.
try:
    from aioice import Connection as IceConnection, TransportPolicy
    HAVE_AIOICE = True
except ImportError:  # pragma: no cover - environment guard
    IceConnection = None
    TransportPolicy = None
    HAVE_AIOICE = False

# aiortc powers the optional Phase 6 direct-connect check. It is a full WebRTC
# stack (and pulls in aioice), so it stays optional in the same way.
try:
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.sdp import candidate_from_sdp
    HAVE_AIORTC = True
except ImportError:  # pragma: no cover - environment guard
    RTCConfiguration = None
    RTCIceServer = None
    RTCPeerConnection = None
    RTCSessionDescription = None
    candidate_from_sdp = None
    HAVE_AIORTC = False

# ─── STUN / TURN protocol constants (RFC 5389 / RFC 5766) ────────────
MAGIC = 0x2112A442

BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
ALLOCATE = 0x0003
ALLOCATE_OK = 0x0103
ALLOCATE_ERR = 0x0113
CREATE_PERM = 0x0008
CREATE_PERM_OK = 0x0108
SEND_INDICATION = 0x0016
DATA_INDICATION = 0x0017

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009
ATTR_XOR_PEER_ADDRESS = 0x0012
ATTR_DATA = 0x0013
ATTR_REALM = 0x0014
ATTR_NONCE = 0x0015
ATTR_XOR_RELAYED_ADDRESS = 0x0016
ATTR_REQUESTED_TRANSPORT = 0x0019
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_SOFTWARE = 0x8022

REQUESTED_TRANSPORT_UDP = b"\x11\x00\x00\x00"  # protocol 17 (UDP) + 3 reserved bytes

# Colours (disabled when not a TTY)
_TTY = sys.stdout.isatty()
def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _TTY else s
def green(s): return _c("32", s)
def red(s): return _c("31", s)
def yellow(s): return _c("33", s)
def bold(s): return _c("1", s)


def _enable_ice_logging():
    """Route aioice's ICE logs through the phase output (used with -v)."""
    logger = logging.getLogger("aioice")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("       [ice] %(message)s"))
        logger.addHandler(handler)

OK = green("PASS")
FAIL = red("FAIL")


# ─── STUN/TURN message helpers ───────────────────────────────────────
def _pad4(n):
    return (4 - n % 4) % 4


def _attr(attr_type, value):
    return struct.pack(">HH", attr_type, len(value)) + value + b"\x00" * _pad4(len(value))


def _mi_key(username, realm, credential):
    """Long-term credential key = MD5(username:realm:password)."""
    return hashlib.md5(f"{username}:{realm}:{credential}".encode()).digest()


def build_message(method, txn_id, attrs, integrity_key=None):
    """Build a STUN/TURN message. Appends MESSAGE-INTEGRITY when a key is given."""
    body = b"".join(attrs)
    if integrity_key is not None:
        # HMAC-SHA1 over the message with the length field set to include the
        # 24-byte MESSAGE-INTEGRITY attribute that is about to be appended.
        prefix = struct.pack(">HH", method, len(body) + 24) + struct.pack(">I", MAGIC) + txn_id + body
        mac = hmac.new(integrity_key, prefix, hashlib.sha1).digest()
        body += _attr(ATTR_MESSAGE_INTEGRITY, mac)
    return struct.pack(">HH", method, len(body)) + struct.pack(">I", MAGIC) + txn_id + body


def parse_message(data):
    """Parse a STUN/TURN message into {'type', 'txn', 'attrs': {type: value}}."""
    if len(data) < 20:
        return None
    msg_type = struct.unpack(">H", data[:2])[0]
    msg_len = struct.unpack(">H", data[2:4])[0]
    if struct.unpack(">I", data[4:8])[0] != MAGIC:
        return None
    txn = data[8:20]
    attrs = {}
    pos, end = 20, 20 + msg_len
    while pos + 4 <= min(end, len(data)):
        at, al = struct.unpack(">HH", data[pos:pos + 4])
        attrs[at] = data[pos + 4:pos + 4 + al]
        pos += 4 + al + _pad4(al)
    return {"type": msg_type, "txn": txn, "attrs": attrs}


def encode_xor_address(ip, port):
    """Encode an IPv4 XOR-{PEER,RELAYED,MAPPED}-ADDRESS value."""
    xport = port ^ (MAGIC >> 16)
    xip = struct.unpack(">I", socket.inet_aton(ip))[0] ^ MAGIC
    return b"\x00\x01" + struct.pack(">H", xport) + struct.pack(">I", xip)


def decode_xor_address(raw):
    if len(raw) < 8 or raw[1] != 0x01:  # IPv4 only
        return None, None
    port = struct.unpack(">H", raw[2:4])[0] ^ (MAGIC >> 16)
    ip = struct.unpack(">I", raw[4:8])[0] ^ MAGIC
    return socket.inet_ntoa(struct.pack(">I", ip)), port


def error_code(attrs):
    raw = attrs.get(ATTR_ERROR_CODE, b"")
    if len(raw) < 4:
        return 0, ""
    code = (raw[2] & 0x07) * 100 + raw[3]
    reason = raw[4:].decode("utf-8", errors="replace")
    return code, reason


# ─── Transport abstraction (UDP datagrams vs TLS/TCP stream) ─────────
class TurnError(Exception):
    pass


class UdpTransport:
    kind = "udp"

    def __init__(self, host, port, timeout):
        self.addr = (host, port)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def send(self, data):
        self.sock.sendto(data, self.addr)

    def recv(self):
        """Return one STUN message or None on timeout (datagram = one message)."""
        try:
            return self.sock.recvfrom(4096)[0]
        except socket.timeout:
            return None

    def close(self):
        self.sock.close()


class TlsTransport:
    kind = "tcp/tls"

    def __init__(self, host, port, timeout, insecure=False):
        self.timeout = timeout
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        self.peer_cert = self.sock.getpeercert()

    def send(self, data):
        self.sock.sendall(data)

    def recv(self):
        """Read one length-framed STUN message off the TLS stream."""
        try:
            header = self._read_exact(20)
            if header is None:
                return None
            body_len = struct.unpack(">H", header[2:4])[0]
            body = self._read_exact(body_len) if body_len else b""
            if body is None:
                return None
            return header + body
        except socket.timeout:
            return None

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None  # EOF
            buf += chunk
        return buf

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def request(transport, method, attrs, integrity_key=None, retransmit=3):
    """Send a STUN/TURN request and return the parsed matching response.

    Retransmits on UDP loss; a single attempt suffices for the reliable TLS
    stream but we still loop to skip any unrelated stray datagram.
    """
    txn = os.urandom(12)
    msg = build_message(method, txn, attrs, integrity_key)
    attempts = retransmit if transport.kind == "udp" else 1
    for _ in range(attempts):
        transport.send(msg)
        # Read messages until we see our transaction id (ignore stray packets).
        deadline = time.monotonic() + transport.timeout
        while time.monotonic() < deadline:
            data = transport.recv()
            if data is None:
                break
            parsed = parse_message(data)
            if parsed and parsed["txn"] == txn:
                return parsed
    raise TurnError("no response (timeout)")


# ─── TURN operations ─────────────────────────────────────────────────
def turn_allocate(transport, username, credential):
    """Full authenticated Allocate. Returns (relay_ip, relay_port, realm, nonce, key)."""
    # 1) unauthenticated Allocate -> 401 challenge with realm + nonce
    challenge = request(transport, ALLOCATE, [_attr(ATTR_REQUESTED_TRANSPORT, REQUESTED_TRANSPORT_UDP)])
    if challenge["type"] != ALLOCATE_ERR:
        raise TurnError(f"expected 401 challenge, got 0x{challenge['type']:04x}")
    code, _ = error_code(challenge["attrs"])
    if code != 401 or ATTR_NONCE not in challenge["attrs"]:
        raise TurnError(f"unexpected challenge (code={code})")
    realm = challenge["attrs"][ATTR_REALM].decode("utf-8", "replace")
    nonce = challenge["attrs"][ATTR_NONCE]
    key = _mi_key(username, realm, credential)

    # 2) authenticated Allocate -> success with XOR-RELAYED-ADDRESS
    resp = request(
        transport, ALLOCATE,
        [_attr(ATTR_REQUESTED_TRANSPORT, REQUESTED_TRANSPORT_UDP),
         _attr(ATTR_USERNAME, username.encode()),
         _attr(ATTR_REALM, realm.encode()),
         _attr(ATTR_NONCE, nonce)],
        integrity_key=key,
    )
    if resp["type"] != ALLOCATE_OK:
        code, reason = error_code(resp["attrs"])
        raise TurnError(f"Allocate rejected: {code} {reason}".rstrip())
    relay_ip, relay_port = decode_xor_address(resp["attrs"].get(ATTR_XOR_RELAYED_ADDRESS, b""))
    if relay_ip is None:
        raise TurnError("Allocate response missing XOR-RELAYED-ADDRESS")
    return relay_ip, relay_port, realm, nonce, key


def turn_create_permission(transport, peer_ip, username, realm, nonce, key):
    resp = request(
        transport, CREATE_PERM,
        [_attr(ATTR_XOR_PEER_ADDRESS, encode_xor_address(peer_ip, 0)),
         _attr(ATTR_USERNAME, username.encode()),
         _attr(ATTR_REALM, realm.encode()),
         _attr(ATTR_NONCE, nonce)],
        integrity_key=key,
    )
    if resp["type"] != CREATE_PERM_OK:
        code, reason = error_code(resp["attrs"])
        raise TurnError(f"CreatePermission rejected: {code} {reason}".rstrip())


# ─── Discovered-server model ─────────────────────────────────────────
class TurnEndpoint:
    def __init__(self, scheme, host, port, transport, username, credential):
        self.scheme = scheme          # turn | turns
        self.host = host
        self.port = port
        self.transport = transport    # udp | tcp
        self.username = username
        self.credential = credential

    def __str__(self):
        return f"{self.scheme}:{self.host}:{self.port}?transport={self.transport}"


def _split_host_port(url_no_scheme):
    hostport = url_no_scheme.split("?", 1)[0]
    host, port = hostport.rsplit(":", 1)
    return host, int(port)


def classify_ice_servers(ice_servers):
    """Return (stun_urls, [TurnEndpoint...]) from JoinResponse ice_servers."""
    stun_urls, turns = [], []
    for srv in ice_servers:
        for url in srv.urls:
            scheme = url.split(":", 1)[0]
            if scheme == "stun" or scheme == "stuns":
                stun_urls.append(url)
                continue
            rest = url.split(":", 1)[1]
            host, port = _split_host_port(rest)
            transport = "tcp" if "transport=tcp" in url else "udp"
            turns.append(TurnEndpoint(scheme, host, port, transport, srv.username, srv.credential))
    return stun_urls, turns


def ttl_note(username):
    """For REST (timestamp:...) usernames, show remaining validity."""
    if ":" in username and username.split(":")[0].isdigit():
        remaining = int(username.split(":")[0]) - int(time.time())
        return f"REST auth, ~{remaining}s remaining"
    return "static/token auth"


# ─── Phase 1: LiveKit signaling + discovery ──────────────────────────
def parse_candidate(candidate_init):
    """Parse a trickled candidateInit JSON string into a small dict."""
    try:
        line = json.loads(candidate_init).get("candidate", "")
    except (TypeError, ValueError):
        line = candidate_init or ""
    if line.startswith("candidate:"):
        line = line[len("candidate:"):]
    parts = line.split()
    # foundation component transport priority address port typ type ...
    if len(parts) < 8 or parts[6] != "typ":
        return None
    info = {
        "transport": parts[2],
        "ip": parts[4],
        "port": int(parts[5]),
        "type": parts[7],
    }
    if "tcptype" in parts:
        info["tcptype"] = parts[parts.index("tcptype") + 1]
    return info


async def collect_server_candidates(ws, grace):
    """Collect the ICE candidates the SFU advertises right after the join.

    LiveKit trickles the SFU's candidates immediately after the offer, without
    waiting for anything from the client, so a short read window is enough.
    """
    candidates = []
    deadline = time.monotonic() + grace
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if isinstance(raw, str):
            continue
        resp = SignalResponse()
        resp.ParseFromString(raw)
        kind = resp.WhichOneof("message")
        if kind == "trickle":
            info = parse_candidate(resp.trickle.candidateInit)
            if info:
                candidates.append(info)
        elif kind == "offer":
            # Some servers put candidates directly in the SDP instead of
            # trickling them.
            for line in resp.offer.sdp.splitlines():
                line = line.strip()
                if line.startswith("a=candidate:"):
                    info = parse_candidate(line[len("a="):])
                    if info:
                        candidates.append(info)
    # De-duplicate (a candidate may arrive both inline and via trickle).
    unique = {}
    for c in candidates:
        unique[(c["ip"], c["port"], c["transport"], c["type"])] = c
    return list(unique.values())


def make_token(api_key, api_secret, room, identity):
    """Create a room-join JWT for one participant."""
    return (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room))
        .to_jwt()
    )


def signal_url(url, api_key, api_secret, room, identity, protocol):
    """Build the signaling WebSocket URL, including a freshly signed token."""
    token = make_token(api_key, api_secret, room, identity)
    query = urllib.parse.urlencode({
        "access_token": token,
        "auto_subscribe": "0",
        "protocol": str(protocol),
        "sdk": "python",
        "version": "1.0.0",
    })
    return f"{url.rstrip('/')}/rtc?{query}"


async def discover(url, api_key, api_secret, room, identity, protocol, timeout):
    ws_url = signal_url(url, api_key, api_secret, room, identity, protocol)
    async with websockets.connect(ws_url, subprotocols=["livekit"], max_size=None,
                                  open_timeout=timeout) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if not isinstance(raw, (bytes, bytearray)):
            raise RuntimeError(f"unexpected text frame from server: {raw[:120]!r}")
        resp = SignalResponse()
        resp.ParseFromString(raw)
        which = resp.WhichOneof("message")
        if which != "join":
            raise RuntimeError(f"server did not send a JoinResponse (got {which!r})")
        # The SFU offers its subscriber connection and trickles its candidates
        # right away; collect them to learn the addresses it advertises.
        server_candidates = await collect_server_candidates(ws, min(timeout, 1.5))
    return resp.join, server_candidates


def print_discovery(join, stun_urls, turns, verbose, server_candidates):
    print(bold("Phase 1: LiveKit signaling + discovery"))
    print(f"   server version : {join.server_version}")
    print(f"   room sid       : {join.room.sid}")
    print(f"   result         : {OK}  (JoinResponse received - signaling works)")
    print()
    print(bold("   Discovered STUN/TURN servers (what clients use):"))
    if stun_urls:
        for u in stun_urls:
            print(f"     STUN  {u}")
    else:
        print("     STUN  (none advertised - clients fall back to defaults)")
    for ep in turns:
        label = "TURN/UDP " if ep.transport == "udp" else "TURN/TCP "
        print(f"     {label} {ep}")
        if verbose:
            print(f"              auth: {ttl_note(ep.username)}  username={ep.username}")
    print()
    print(bold("   SFU advertised ICE candidates (from signaling):"))
    if server_candidates:
        for c in server_candidates:
            extra = f"/{c['tcptype']}" if c.get("tcptype") else ""
            print(f"     {c['transport'].upper():3} {c['ip']}:{c['port']} "
                  f"({c['type']}{extra})")
    else:
        print("     (none received - server may not use trickle ICE)")
    print()


def run_discovery(url, api_key, api_secret, room, identity, protocol, timeout):
    """Run the async discovery on a private event loop.

    A quiet exception handler suppresses the noisy 'connection_lost' callback
    logging that websockets/asyncio emit during teardown of a *failed* connect;
    the real error still propagates through run_until_complete.
    """
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda loop, context: None)
    try:
        return loop.run_until_complete(
            discover(url, api_key, api_secret, room, identity, protocol, timeout))
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()


# ─── Phase 2: STUN binding ───────────────────────────────────────────
def run_stun(stun_urls, turns, timeout):
    print(bold("Phase 2: STUN binding (server-reflexive address)"))
    targets = []
    for u in stun_urls:
        host, port = _split_host_port(u.split(":", 1)[1])
        targets.append((f"{u}", host, port))
    if not targets:
        # No stun: advertised - TURN servers also answer STUN Binding.
        seen = set()
        for ep in turns:
            if ep.transport == "udp" and (ep.host, ep.port) not in seen:
                seen.add((ep.host, ep.port))
                targets.append((f"{ep.host}:{ep.port} (TURN host)", ep.host, ep.port))
        if targets:
            print("   (no stun: URL advertised; testing STUN Binding against TURN UDP host)")

    if not targets:
        print(f"   result         : {yellow('SKIP')}  (no STUN-capable UDP target)")
        print()
        return None, []

    ok = False
    used = []
    for label, host, port in targets:
        t = UdpTransport(host, port, timeout)
        try:
            resp = request(t, BINDING_REQUEST, [])
            raw = resp["attrs"].get(ATTR_XOR_MAPPED_ADDRESS) or resp["attrs"].get(ATTR_MAPPED_ADDRESS)
            ip, mport = decode_xor_address(raw) if raw else (None, None)
            if ip:
                print(f"   {label}")
                print(f"       reflexive : {ip}:{mport}   {OK}")
                used.append((f"{host}:{port}", f"{ip}:{mport}"))
                ok = True
            else:
                print(f"   {label}: response without mapped address   {FAIL}")
                used.append((f"{host}:{port}", None))
        except TurnError as e:
            print(f"   {label}: {e}   {FAIL}")
            used.append((f"{host}:{port}", None))
        finally:
            t.close()
    print(f"   result         : {OK if ok else FAIL}")
    print()
    return ok, used


# ─── Phase 3/4: TURN relay data path ─────────────────────────────────
def open_transport(ep, timeout, insecure):
    if ep.transport == "udp":
        return UdpTransport(ep.host, ep.port, timeout)
    return TlsTransport(ep.host, ep.port, timeout, insecure=insecure)


def run_turn_relay(ep, timeout, insecure):
    """Full relay data path: two allocations relay a random token to each other."""
    a = b = None
    try:
        a = open_transport(ep, timeout, insecure)
        b = open_transport(ep, timeout, insecure)

        a_ip, a_port, a_realm, a_nonce, a_key = turn_allocate(a, ep.username, ep.credential)
        b_ip, b_port, b_realm, b_nonce, b_key = turn_allocate(b, ep.username, ep.credential)
        print(f"       allocate  : relay A {a_ip}:{a_port}   relay B {b_ip}:{b_port}   {OK}")

        turn_create_permission(a, b_ip, ep.username, a_realm, a_nonce, a_key)
        turn_create_permission(b, a_ip, ep.username, b_realm, b_nonce, b_key)
        print(f"       permission: A->B and B->A installed   {OK}")

        token = os.urandom(16)
        send = build_message(SEND_INDICATION, os.urandom(12),
                             [_attr(ATTR_XOR_PEER_ADDRESS, encode_xor_address(b_ip, b_port)),
                              _attr(ATTR_DATA, token)])
        received = False
        for _ in range(4):
            a.send(send)
            data = b.recv()
            parsed = parse_message(data) if data else None
            if parsed and parsed["type"] == DATA_INDICATION and parsed["attrs"].get(ATTR_DATA) == token:
                peer_ip, peer_port = decode_xor_address(parsed["attrs"].get(ATTR_XOR_PEER_ADDRESS, b""))
                print(f"       relay data: token echoed via relay from {peer_ip}:{peer_port}   {OK}")
                received = True
                break
        if not received:
            print(f"       relay data: token NOT received at B   {FAIL}")
            return False
        return True
    except (TurnError, OSError, ssl.SSLError) as e:
        print(f"       error     : {e}   {FAIL}")
        return False
    finally:
        if a:
            a.close()
        if b:
            b.close()


def run_turn_phase(turns, timeout, insecure):
    results = {}
    for transport, phase_no in (("udp", 3), ("tcp", 4)):
        eps = [ep for ep in turns if ep.transport == transport]
        proto = "UDP" if transport == "udp" else "TCP/TLS"
        print(bold(f"Phase {phase_no}: TURN relay over {proto}"))
        if not eps:
            print(f"   result         : {yellow('SKIP')}  (no {proto} TURN server advertised)")
            print()
            continue
        phase_ok = False
        for ep in eps:
            print(f"   {ep}")
            ok = run_turn_relay(ep, timeout, insecure)
            results[str(ep)] = ok
            phase_ok = phase_ok or ok
        print(f"   result         : {OK if phase_ok else FAIL}")
        print()
    return results


# ─── Phase 5: real ICE (RFC 8445) relay-only connectivity checks ─────
def _ip_scope(ip):
    """Classify an IP literal as 'private' or 'public' (None if not an IP)."""
    try:
        addr = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return None
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return "private"
    return "public"


def _client_reflexive_is_private(stun_used):
    """True if STUN saw this client behind a private/SNAT address."""
    for _, reflexive in stun_used:
        if not reflexive:
            continue
        if _ip_scope(reflexive.rsplit(":", 1)[0]) == "private":
            return True
    return False


def _relay_address_mismatch(relay_ips, server_candidates):
    """Spot the NAT mismatch that makes a real browser's ICE fail.

    A browser creates TURN permissions for the peer addresses the SFU
    advertises. If the SFU only advertises public candidates while TURN hands
    out private relay addresses, the SFU's checks reach the TURN server from a
    private source address that was never permitted, and TURN drops them.
    """
    private_relays = sorted({ip for ip in relay_ips if _ip_scope(ip) == "private"})
    public_servers = sorted({
        c["ip"] for c in server_candidates
        if c.get("ip") and _ip_scope(c["ip"]) == "public"
    })
    server_has_private = any(
        _ip_scope(c.get("ip")) == "private" for c in server_candidates
    )
    if private_relays and public_servers and not server_has_private:
        return private_relays, public_servers
    return None


def _ice_config(ep, insecure_tls):
    """Map a discovered TurnEndpoint to aioice Connection keyword arguments."""
    # A turns: URL means TLS over TCP even if the query string omits transport.
    use_tcp = ep.transport == "tcp" or ep.scheme == "turns"
    turn_ssl = ep.scheme == "turns"
    if turn_ssl and insecure_tls:
        # aioice accepts an SSLContext even though the type hint says bool.
        turn_ssl = ssl.create_default_context()
        turn_ssl.check_hostname = False
        turn_ssl.verify_mode = ssl.CERT_NONE
    return {
        "turn_server": (ep.host, ep.port),
        "turn_username": ep.username,
        "turn_password": ep.credential,
        "turn_ssl": turn_ssl,
        "turn_transport": "tcp" if use_tcp else "udp",
        "transport_policy": TransportPolicy.RELAY,
    }


def _restrict_to_relay(conn):
    """Drop every non-relay transport so all checks leave via the TURN allocation.

    TransportPolicy.RELAY hides host candidates from the advertised list, but
    aioice still builds pairs on those hidden host transports in connect(),
    and they normally win nomination on a LAN. Trimming the protocol list is
    what makes this a true relay<->relay check. This touches a private
    aioice attribute, so it degrades to a no-op if the internals change.
    """
    protocols = getattr(conn, "_protocols", None)
    if protocols is None:
        return []
    relay = [p for p in protocols if p.local_candidate.type == "relay"]
    if not relay:
        return []
    conn._protocols = relay
    return [p for p in protocols if p not in relay]


async def _close_protocols(protocols):
    for protocol in protocols:
        try:
            await protocol.close()
        except Exception:
            pass


async def _ice_relay_check(ep, timeout, insecure_tls, verbose, server_candidates,
                           client_reflexive_private):
    """Run real ICE between two agents whose only candidates are TURN relays."""
    a = IceConnection(ice_controlling=True, **_ice_config(ep, insecure_tls))
    b = IceConnection(ice_controlling=False, **_ice_config(ep, insecure_tls))
    # ICE is multi-step (allocate, permission, checks, nomination), so give it
    # a floor instead of tying it strictly to the per-operation --timeout.
    gather_timeout = max(timeout * 2, 10.0)
    check_timeout = max(timeout * 4, 20.0)
    dropped = []
    try:
        await asyncio.wait_for(
            asyncio.gather(a.gather_candidates(), b.gather_candidates()),
            timeout=gather_timeout,
        )

        for conn, side in ((a, "A"), (b, "B")):
            dropped += _restrict_to_relay(conn)
            relay = [c for c in conn.local_candidates if c.type == "relay"]
            if not relay:
                print(f"       gather    : agent {side}: no relay candidate   {FAIL}")
                return False
            print(f"       gather    : agent {side} relay {relay[0].host}:{relay[0].port}   {OK}")

        # Exchange ICE credentials (ufrag / password) and relay candidates,
        # then signal end-of-candidates.
        a.remote_username, a.remote_password = b.local_username, b.local_password
        b.remote_username, b.remote_password = a.local_username, a.local_password
        for conn, peer in ((a, b), (b, a)):
            for candidate in peer.local_candidates:
                if candidate.type == "relay":
                    await conn.add_remote_candidate(candidate)
            await conn.add_remote_candidate(None)

        if verbose:
            print("       checks    : controlling=A, controlled=B")

        tasks = [asyncio.create_task(a.connect()), asyncio.create_task(b.connect())]
        done, pending = await asyncio.wait(tasks, timeout=check_timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            print(f"       checks    : ICE did not finish within {check_timeout:.0f}s   {FAIL}")
            return False
        for task in done:
            if task.exception():
                raise task.exception()

        pair_a = getattr(a, "_nominated", {}).get(1)
        pair_b = getattr(b, "_nominated", {}).get(1)
        if pair_a is None or pair_b is None:
            print(f"       selected  : no nominated pair   {FAIL}")
            return False

        lc, rc = pair_a.local_candidate, pair_a.remote_candidate
        print(f"       selected  : {lc.type} {lc.host}:{lc.port}  <->  "
              f"{rc.type} {rc.host}:{rc.port}   {OK}")
        if lc.type != "relay" or rc.type != "relay":
            print(f"       warning   : nominated pair is not relay<->relay   {FAIL}")
            return False

        # The relay<->relay hairpin above can pass while a real browser still
        # fails, so also validate the relay address against what the SFU
        # advertises to clients.
        if not server_candidates:
            print("       address   : SKIP (no SFU candidates advertised via trickle)")
            return True
        relay_ips = {
            c.host for conn in (a, b)
            for c in conn.local_candidates if c.type == "relay"
        }
        mismatch = _relay_address_mismatch(relay_ips, server_candidates)
        if mismatch:
            private_relays, public_servers = mismatch
            label = FAIL if client_reflexive_private else yellow("WARN")
            print(f"       address   : TURN relay {', '.join(private_relays)} is private, "
                  f"SFU only advertises {', '.join(public_servers)}   {label}")
            if client_reflexive_private:
                print("                   The client is also seen via a private "
                      "(SNAT) address.")
                print("                   A browser permits only the advertised peer "
                      "addresses on TURN.")
                print("                   If the SFU sends from a private address (the usual "
                      "case behind NAT),")
                print("                   TURN drops those checks and ICE fails.")
                print("                   Fix: advertise a reachable public relay address "
                      "(e.g. coturn")
                print("                   --external-ip=<public>[/<private>]) or have the "
                      "SFU advertise a")
                print("                   private candidate on the relay network.")
                return False
            print("                   The client is seen with a public address, so "
                  "direct connectivity may")
            print("                   not rely on TURN. Relay-only clients can still fail: "
                  "TURN permissions")
            print("                   use the advertised peer address, which may not match "
                  "the SFU's source.")
        return True

    except asyncio.TimeoutError:
        print(f"       error     : gathering timed out   {FAIL}")
        return False
    except Exception as e:
        print(f"       error     : {type(e).__name__}: {e}   {FAIL}")
        return False
    finally:
        await _close_protocols(dropped)
        await asyncio.gather(a.close(), b.close(), return_exceptions=True)


def _run_ice_relay_check(ep, timeout, insecure_tls, verbose, server_candidates,
                         client_reflexive_private):
    """Run one async ICE check on a private event loop."""
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda loop, context: None)
    try:
        return loop.run_until_complete(
            _ice_relay_check(ep, timeout, insecure_tls, verbose, server_candidates,
                             client_reflexive_private))
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()


def run_ice_phase(turns, timeout, insecure_tls, verbose, server_candidates,
                  client_reflexive_private):
    print(bold("Phase 5: ICE connectivity checks (RFC 8445, relay-only)"))
    if not HAVE_AIOICE:
        print(f"   result         : {yellow('SKIP')}  (aioice not installed - "
              f"pip install aioice)")
        print()
        return {}
    if not turns:
        print(f"   result         : {yellow('SKIP')}  (no TURN server advertised)")
        print()
        return {}

    if verbose:
        _enable_ice_logging()

    results = {}
    for ep in turns:
        print(f"   {ep}")
        results[str(ep)] = _run_ice_relay_check(
            ep, timeout, insecure_tls, verbose, server_candidates,
            client_reflexive_private)
    print(f"   result         : {OK if any(results.values()) else FAIL}")
    print()
    return results


# ─── Phase 6: direct connectivity (aiortc, no TURN) ──────────────────
# Let in-flight ICE checks finish after connect so the best-successful
# snapshot is not taken mid-check.
ICE_SETTLE_SECONDS = 1.0


def direct_ice_servers(urls):
    """Build AIORTC ICE servers from STUN URLs only; TURN is never allowed.

    The direct phase proves a client can reach the SFU without a relay, so any
    turn:/turns: URL is filtered out even if a caller passes a mixed list.
    """
    stun = [u for u in urls if u.split(":", 1)[0] in ("stun", "stuns")]
    if not stun:
        return None
    return [RTCIceServer(urls=stun)]


def direct_stun_urls(stun_urls, turns):
    """STUN servers for the direct phase.

    Prefer the advertised stun: URLs. If the deployment advertises none, fall
    back to each TURN server's UDP host: a TURN server answers STUN Binding
    (Phase 2 already proves it), and a Binding request does not allocate a
    relay. The media path stays direct; only our srflx address is discovered.
    """
    if stun_urls:
        return list(stun_urls)
    urls = []
    for endpoint in turns:
        if endpoint.transport == "udp":
            url = f"stun:{endpoint.host}:{endpoint.port}"
            if url not in urls:
                urls.append(url)
    return urls


def combined_ice_servers(stun_urls, turns):
    """ICE servers for the combined phase: STUN plus TURN with credentials.

    One client then offers host, srflx and relay candidates, so ICE itself
    decides between the direct and relayed paths.
    """
    servers = []
    stun = direct_stun_urls(stun_urls, turns)
    if stun:
        servers.append(RTCIceServer(urls=stun))
    for endpoint in turns:
        servers.append(RTCIceServer(
            urls=[str(endpoint)],
            username=endpoint.username,
            credential=endpoint.credential,
        ))
    return servers or None


def parse_remote_candidate(candidate_init):
    """Parse a LiveKit TrickleRequest.candidateInit JSON string.

    Returns an AIORTC RTCIceCandidate, or None for end-of-candidates and
    malformed input (a stray candidate must not abort the whole phase).
    """
    try:
        data = json.loads(candidate_init)
        sdp = data.get("candidate") or ""
        if sdp.startswith("candidate:"):
            sdp = sdp[len("candidate:"):]
        if not sdp:
            return None
        candidate = candidate_from_sdp(sdp)
        candidate.sdpMid = data.get("sdpMid")
        candidate.sdpMLineIndex = data.get("sdpMLineIndex")
        return candidate
    except (AttributeError, TypeError, ValueError, AssertionError):
        return None


def _candidate_summary(candidate):
    return {
        "type": candidate.type, "host": candidate.host,
        "port": candidate.port, "transport": candidate.transport,
    }


def pair_summary(pair):
    """Describe a candidate pair's local and remote candidates."""
    return {
        "local": _candidate_summary(pair.local_candidate),
        "remote": _candidate_summary(pair.remote_candidate),
    }


def pair_line(summary):
    """Format a pair summary for the phase output."""
    return (f"{summary['local']['type']} "
            f"{summary['local']['host']}:{summary['local']['port']}"
            f"  <->  "
            f"{summary['remote']['type']} "
            f"{summary['remote']['host']}:{summary['remote']['port']}")


def selected_pair_summary(ice_transport):
    """Return the nominated local/remote candidate pair of an AIORTC transport.

    AIORTC's getStats() only reports RTP statistics, so the pair is read from
    the underlying aioice connection - the same source Phase 5 uses.
    """
    connection = getattr(ice_transport, "_connection", None)
    nominated = getattr(connection, "_nominated", None)
    if not nominated:
        return None
    pair = nominated.get(1) or next(iter(nominated.values()), None)
    return pair_summary(pair) if pair is not None else None


def controlling_agent(ice_transport):
    """Return which side controls ICE nomination: 'client' or 'SFU'.

    The local agent's `ice_controlling` flag is authoritative: the offerer is
    controlling unless it negotiated ICE-lite, and the SFU sends the subscriber
    offer.
    """
    connection = getattr(ice_transport, "_connection", None)
    if connection is None:
        return None
    return "client" if getattr(connection, "ice_controlling", False) else "SFU"


def pair_priority(local, remote, controlling):
    """RFC 8445 6.1.2.3 candidate pair priority (higher wins)."""
    g = local.priority if controlling else remote.priority
    d = remote.priority if controlling else local.priority
    return (1 << 32) * min(g, d) + 2 * max(g, d) + (1 if g > d else 0)


def successful_pairs(ice_transport):
    """Return (summary, priority) for every successful candidate pair."""
    connection = getattr(ice_transport, "_connection", None)
    check_list = getattr(connection, "_check_list", None)
    if not check_list:
        return []
    controlling = getattr(connection, "ice_controlling", False)
    pairs = []
    for pair in check_list:
        if getattr(getattr(pair, "state", None), "name", None) != "SUCCEEDED":
            continue
        pairs.append((
            pair_summary(pair),
            pair_priority(pair.local_candidate, pair.remote_candidate, controlling),
        ))
    return pairs


def selected_path(summary):
    """Classify a nominated candidate pair as 'direct' or 'relay'."""
    if summary is None:
        return None
    types = {summary["local"]["type"], summary["remote"]["type"]}
    return "relay" if "relay" in types else "direct"


def local_candidate_types(ice_transport):
    """Return the distinct candidate types AIORTC gathered (e.g. host, srflx)."""
    gatherer = getattr(ice_transport, "iceGatherer", None)
    if gatherer is None:
        return []
    return sorted({c.type for c in gatherer.getLocalCandidates()})


def build_answer_message(offer_id, sdp):
    """Build the SignalResponse carrying our answer to the subscriber offer."""
    response = SignalResponse()
    response.answer.type = "answer"
    response.answer.sdp = sdp
    response.answer.id = offer_id
    return response


def _ice_transport(pc):
    """Best-effort access to the ICE transport AIORTC created for the PC."""
    transports = getattr(pc, "_RTCPeerConnection__iceTransports", None)
    if transports:
        return next(iter(transports), None)
    sctp = getattr(pc, "sctp", None)
    if sctp is not None:
        return getattr(getattr(sctp, "transport", None), "transport", None)
    for transceiver in pc.getTransceivers():
        dtls = getattr(transceiver.receiver, "transport", None)
        if dtls is not None:
            return getattr(dtls, "transport", None)
    return None


async def _read_signal(ws, timeout):
    """Read one protobuf signal message; ignore text keep-alives."""
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    if isinstance(raw, str):
        return None
    message = SignalResponse()
    message.ParseFromString(raw)
    return message


async def _wait_for_offer(ws, timeout):
    """Read signaling until the subscriber offer arrives.

    The server trickles candidates right after the offer, but buffering any
    that arrive first keeps this robust if the order changes.
    """
    deadline = time.monotonic() + timeout
    trickles = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("no subscriber offer received")
        message = await _read_signal(ws, remaining)
        if message is None:
            continue
        kind = message.WhichOneof("message")
        if kind == "offer":
            return message.offer, trickles
        if kind == "trickle":
            trickles.append(message.trickle)
        elif kind == "leave":
            raise RuntimeError("server left before sending the offer")


async def _add_trickle(pc, trickle):
    """Add one remote candidate; a bad candidate must not abort the phase."""
    candidate = parse_remote_candidate(trickle.candidateInit)
    try:
        if candidate is not None:
            await pc.addIceCandidate(candidate)
        elif trickle.final:
            await pc.addIceCandidate(None)
    except Exception:
        pass


async def _wait_connected(pc, timeout):
    """Poll until ICE/DTLS settles or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = pc.connectionState
        if state in ("connected", "failed", "closed"):
            return state
        await asyncio.sleep(0.1)
    return pc.connectionState


async def _webrtc_connect(ws_url, ice_servers, timeout, verbose, settle=0.0):
    """Answer the subscriber offer and wait for ICE/DTLS to settle."""
    async with websockets.connect(ws_url, subprotocols=["livekit"], max_size=None,
                                  open_timeout=timeout) as ws:
        offer, early_trickles = await _wait_for_offer(ws, timeout)
        if verbose:
            print(f"       offer     : id={offer.id} ({len(offer.sdp)} bytes of SDP)")

        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        pump_error = None
        try:
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=offer.sdp, type="offer"))
            for trickle in early_trickles:
                await _add_trickle(pc, trickle)

            answer = await pc.createAnswer()
            # AIORTC gathers candidates inside setLocalDescription, so the
            # answer SDP already carries our host/srflx candidates.
            await pc.setLocalDescription(answer)
            await ws.send(build_answer_message(
                offer.id, pc.localDescription.sdp).SerializeToString())

            async def pump():
                nonlocal pump_error
                try:
                    async for raw in ws:
                        if isinstance(raw, str):
                            continue
                        message = SignalResponse()
                        message.ParseFromString(raw)
                        kind = message.WhichOneof("message")
                        if kind == "trickle":
                            await _add_trickle(pc, message.trickle)
                        elif kind == "leave":
                            raise RuntimeError("server terminated the session (leave)")
                except Exception as exc:
                    pump_error = exc

            pump_task = asyncio.create_task(pump())
            try:
                state = await _wait_connected(pc, max(timeout * 4, 20.0))
                if settle and state == "connected":
                    await asyncio.sleep(settle)
            finally:
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)

            transport = _ice_transport(pc)
            return {
                "state": state,
                "pair": selected_pair_summary(transport) if transport else None,
                "successful": successful_pairs(transport) if transport else [],
                "local": local_candidate_types(transport) if transport else [],
                "controller": controlling_agent(transport) if transport else None,
                "error": pump_error,
            }
        finally:
            await pc.close()


def _run_webrtc(ws_url, ice_servers, timeout, verbose, settle=0.0):
    """Run one async WebRTC connect check on a private event loop."""
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda loop, context: None)
    try:
        return loop.run_until_complete(
            _webrtc_connect(ws_url, ice_servers, timeout, verbose, settle))
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()


def run_direct_phase(url, api_key, api_secret, room, identity, protocol,
                     stun_urls, turns, timeout, verbose):
    """Phase 6: connect a real WebRTC client to the SFU without any TURN server."""
    print(bold("Phase 6: Direct connectivity (no TURN)"))
    if not HAVE_AIORTC:
        print(f"   result         : {yellow('SKIP')}  (aiortc not installed - "
              f"pip install aiortc)")
        print()
        return None

    if verbose:
        _enable_ice_logging()

    direct_stun = direct_stun_urls(stun_urls, turns)
    if not stun_urls and direct_stun:
        print("   (no stun: URL advertised; using the TURN host for STUN "
              "Binding only)")

    # A distinct identity avoids racing the still-closing Phase 1 session.
    ws_url = signal_url(url, api_key, api_secret, room,
                        f"{identity}-direct", protocol)
    try:
        result = _run_webrtc(
            ws_url, direct_ice_servers(direct_stun), timeout, verbose)
    except Exception as e:
        print(f"   error          : {type(e).__name__}: {e}   {FAIL}")
        print()
        return False

    print(f"   local          : {', '.join(result['local']) or 'none gathered'}")
    pair = result["pair"]
    if pair:
        print(f"   selected       : {pair_line(pair)}")
    if result["state"] == "connected":
        print(f"   result         : {OK}")
        print()
        return True

    print(f"   result         : {FAIL}  (connection state: {result['state']})")
    if result["error"]:
        print(f"                   signaling: {result['error']}")
    if not direct_stun:
        print("                   No STUN-capable server was available, so only "
              "host candidates were gathered.")
    print("                   Check that UDP to the SFU's advertised address is "
          "allowed from this")
    print("                   machine's network and that the SFU advertises a "
          "reachable address.")
    print()
    return False


def run_combined_phase(url, api_key, api_secret, room, identity, protocol,
                       stun_urls, turns, timeout, verbose):
    """Phase 7: one ICE agent chooses between direct and TURN candidates."""
    print(bold("Phase 7: Path selection (direct vs TURN)"))
    if not HAVE_AIORTC:
        print(f"   result         : {yellow('SKIP')}  (aiortc not installed - "
              f"pip install aiortc)")
        print()
        return None, None

    ice_servers = combined_ice_servers(stun_urls, turns)
    if ice_servers is None:
        print(f"   result         : {yellow('SKIP')}  (no ICE servers advertised)")
        print()
        return None, None

    if verbose:
        _enable_ice_logging()

    # A distinct identity avoids racing the other signaling sessions.
    ws_url = signal_url(url, api_key, api_secret, room,
                        f"{identity}-combined", protocol)
    try:
        result = _run_webrtc(ws_url, ice_servers, timeout, verbose,
                             settle=ICE_SETTLE_SECONDS)
    except Exception as e:
        print(f"   error          : {type(e).__name__}: {e}   {FAIL}")
        print()
        return False, None

    nominated_pair = result["pair"]
    successes = result["successful"]
    best_pair = (
        max(successes, key=lambda item: item[1])[0] if successes else None)
    nominated = selected_path(nominated_pair)
    best = selected_path(best_pair)
    controller = result.get("controller")

    print(f"   local          : {', '.join(result['local']) or 'none gathered'}")
    if controller == "SFU":
        print("   role           : SFU controlling (client controlled)")
    elif controller == "client":
        print("   role           : client controlling (SFU controlled)")
    if nominated_pair:
        print(f"   nominated      : {pair_line(nominated_pair)}   ({nominated})")
    if best_pair and best_pair != nominated_pair:
        print(f"   best successful: {pair_line(best_pair)}   ({best})")
        print("   note           : the controlling agent nominates; a relay pair can "
              "win before a")
        print("                    higher-priority direct pair completes.")
    if result["state"] == "connected":
        print(f"   result         : {OK}")
        print()
        return True, {"best": best, "nominated": nominated,
                      "controller": controller}

    print(f"   result         : {FAIL}  (connection state: {result['state']})")
    if result["error"]:
        print(f"                   signaling: {result['error']}")
    print("                   Check that the advertised TURN server is reachable "
          "and its")
    print("                   credentials are still valid.")
    print()
    return False, {"best": best, "nominated": nominated,
                   "controller": controller}


# ─── Phase 8: real browser (Chrome + livekit-client) ─────────────────
BROWSER_DIR = Path(__file__).resolve().with_name("browser")
BROWSER_SDK_REL = Path("node_modules/livekit-client/dist/livekit-client.esm.mjs")
BROWSER_SDK_VERSION = "2.23.0"
BROWSER_CDN_URL = (
    f"https://cdn.jsdelivr.net/npm/livekit-client@{BROWSER_SDK_VERSION}"
    "/dist/livekit-client.esm.mjs"
)

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
    PLAYWRIGHT_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - environment guard
    sync_playwright = None
    PlaywrightError = Exception
    HAVE_PLAYWRIGHT = False
    PLAYWRIGHT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class BrowserUrlError(ValueError):
    """The browser opened an unexpected signaling URL."""


_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def redact_secrets(text):
    """Remove tokens from anything that may be printed."""
    text = re.sub(r"access_token=[^&\s\"']+", "access_token=<redacted>",
                  str(text))
    return _JWT_RE.sub("<redacted-jwt>", text)


def rtc_url_facts(url):
    """Return safe facts about a signaling URL; never include the token."""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    safe = {
        key: values[0]
        for key, values in query.items()
        if key not in ("access_token", "join_request")
    }
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "path": parsed.path,
        "params": safe,
        "has_token": "access_token" in query,
        "has_join_request": "join_request" in query,
    }


def verify_rtc_url(url, expected_version=None):
    """Check the SDK's signaling URL shape without exposing the token.

    livekit-client 2.23.0 uses /rtc/v1 + join_request in its default
    single-PeerConnection mode; the legacy mode uses /rtc + query params.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("ws", "wss"):
        raise BrowserUrlError(f"unexpected signaling scheme {parsed.scheme!r}")
    query = urllib.parse.parse_qs(parsed.query)
    if "access_token" not in query:
        raise BrowserUrlError("signaling URL has no access_token")
    if parsed.path.endswith("/rtc/v1"):
        if "join_request" not in query:
            raise BrowserUrlError("v1 signaling URL has no join_request")
        return {"mode": "v1", "path": parsed.path}
    if parsed.path.endswith("/rtc"):
        for key in ("auto_subscribe", "protocol", "sdk", "version"):
            if key not in query:
                raise BrowserUrlError(
                    f"legacy signaling URL has no {key}")
        if query["sdk"][0] != "js":
            raise BrowserUrlError(
                f"legacy signaling sdk is {query['sdk'][0]!r}, not 'js'")
        if expected_version and query["version"][0] != expected_version:
            raise BrowserUrlError(
                f"legacy signaling version is {query['version'][0]!r}, "
                f"expected {expected_version!r}")
        return {"mode": "v0", "path": parsed.path}
    raise BrowserUrlError(f"unexpected signaling path {parsed.path!r}")


def stats_index(stats):
    """Index a Chrome getStats() array by stat id."""
    return {stat.get("id"): stat for stat in stats if stat.get("id")}


def select_transport_stat(stats):
    """Pick the transport stat that carries the selected pair."""
    transports = [stat for stat in stats if stat.get("type") == "transport"]
    for stat in transports:
        if stat.get("selectedCandidatePairId"):
            return stat
    for stat in transports:
        if stat.get("dtlsState") or stat.get("iceState"):
            return stat
    return transports[0] if transports else None


def select_candidate_pair(stats, transport):
    """Resolve the selected candidate pair from a getStats() array."""
    index = stats_index(stats)
    if transport:
        pair_id = transport.get("selectedCandidatePairId")
        if pair_id and pair_id in index:
            return index[pair_id]
    pairs = [stat for stat in stats if stat.get("type") == "candidate-pair"]
    for pair in pairs:
        if pair.get("selected") is True:
            return pair
    for pair in pairs:
        if pair.get("nominated") and pair.get("state") == "succeeded":
            return pair
    succeeded = [pair for pair in pairs if pair.get("state") == "succeeded"]
    if succeeded:
        return max(succeeded, key=lambda pair: pair.get("priority") or 0)
    return None


def summarize_candidate(index, candidate_id):
    """Normalize a local/remote candidate stat."""
    stat = index.get(candidate_id) or {}
    return {
        "type": stat.get("candidateType"),
        "address": stat.get("address") or stat.get("ip"),
        "port": stat.get("port"),
        "protocol": stat.get("protocol"),
        "relay_protocol": stat.get("relayProtocol"),
        "url": stat.get("url"),
    }


def ice_role_owner(raw):
    """Map the transport's ICE role to the agent that controls nomination."""
    if raw == "controlling":
        return "client"
    if raw == "controlled":
        return "SFU"
    return None


def browser_path(pair):
    """Classify a selected pair as direct or relay."""
    if not pair:
        return None
    types = {
        pair.get("local", {}).get("type"),
        pair.get("remote", {}).get("type"),
    }
    return "relay" if "relay" in types else "direct"


def address_scope(address):
    """Classify a candidate address as private, public, or unknown."""
    if not address:
        return "unknown"
    value = str(address)
    if value.endswith(".local") or value.endswith(".local."):
        return "unknown"
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return "unknown"
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return "private"
    if ip.is_global:
        return "public"
    return "unknown"


def format_address(address):
    """Show a candidate address, or '<hidden>' when the browser redacts it."""
    return str(address) if address else "<hidden>"


def summarize_browser_pc(payload):
    """Normalize one browser PeerConnection payload into report facts."""
    stats = payload.get("stats") or []
    index = stats_index(stats)
    transport = select_transport_stat(stats)
    pair = select_candidate_pair(stats, transport)
    local_types = sorted({
        stat.get("candidateType")
        for stat in stats
        if stat.get("type") == "local-candidate" and stat.get("candidateType")
    })
    info = {
        "label": payload.get("label") or "pc",
        "association": payload.get("association"),
        "directions": payload.get("directions") or [],
        "connection_state": payload.get("connectionState"),
        "ice_connection_state": payload.get("iceConnectionState"),
        "ice_gathering_state": payload.get("iceGatheringState"),
        "ice_state": transport.get("iceState") if transport else None,
        "dtls_state": transport.get("dtlsState") if transport else None,
        "role": {
            "raw": transport.get("iceRole") if transport else None,
            "owner": ice_role_owner(
                transport.get("iceRole") if transport else None),
        },
        "pair": None,
        "local_types": local_types,
    }
    if pair:
        info["pair"] = {
            "state": pair.get("state"),
            "nominated": pair.get("nominated"),
            "local": summarize_candidate(
                index, pair.get("localCandidateId")),
            "remote": summarize_candidate(
                index, pair.get("remoteCandidateId")),
        }
    return info


def browser_pc_ok(info):
    """A transport is okay when it connected and has a succeeded selected pair."""
    pair = info.get("pair") or {}
    role = info.get("role") or {}
    return (
        pair.get("state") == "succeeded"
        and bool(role.get("owner"))
        and info.get("connection_state") == "connected"
    )


def browser_result_ok(result):
    """Every PeerConnection the browser created must report a usable pair."""
    pcs = result.get("pcs") or []
    return bool(pcs) and all(browser_pc_ok(pc) for pc in pcs)


def browser_summary_label(result):
    """Short summary for the main summary line."""
    pcs = result.get("pcs") or []
    if not pcs:
        return ": no peer connection"
    parts = []
    for pc in pcs:
        parts.append(f"{pc.get('label') or 'pc'} "
                     f"{browser_path(pc.get('pair')) or 'unknown'}")
    return ": " + ", ".join(parts)


def private_address_facts(info):
    """List selected candidates that use a private address."""
    facts = []
    pair = info.get("pair") or {}
    for side in ("local", "remote"):
        candidate = pair.get(side) or {}
        address = candidate.get("address")
        if address_scope(address) == "private":
            facts.append(f"{side} {candidate.get('type')} {address}")
    return facts


def public_server_addresses(server_candidates):
    """Return the SFU's unique public candidate addresses."""
    addresses = []
    for candidate in server_candidates or []:
        address = candidate.get("ip")
        if address_scope(address) == "public" and address not in addresses:
            addresses.append(address)
    return addresses


def inferred_remote_addresses(pair, server_candidates):
    """Infer a hidden remote prflx address from the SFU's public candidates.

    Chrome sanitizes the address of a remote peer-reflexive candidate until the
    application already knows it through signaling. The SFU's public candidate
    address, discovered in Phase 1, is the likely source; it is reported as an
    inference, never as a browser-reported value.
    """
    remote = (pair or {}).get("remote") or {}
    if remote.get("type") != "prflx" or remote.get("address"):
        return []
    return public_server_addresses(server_candidates)


def browser_sdk_path():
    return BROWSER_DIR / BROWSER_SDK_REL


def browser_package_version():
    try:
        data = json.loads((BROWSER_DIR / "package.json").read_text())
        return data.get("dependencies", {}).get(
            "livekit-client", BROWSER_SDK_VERSION)
    except Exception:
        return BROWSER_SDK_VERSION


def browser_skip_reason():
    """Return a human-readable reason to skip Phase 8, or None."""
    if not HAVE_PLAYWRIGHT:
        return (f"playwright not installed ({PLAYWRIGHT_IMPORT_ERROR}) - "
                "pip install playwright")
    if not (BROWSER_DIR / "index.html").exists():
        return f"browser page is missing from {BROWSER_DIR}"
    # A missing local bundle is not fatal: the page can use the pinned CDN
    # and reports sdk_unavailable when neither is available.
    return None


_BROWSER_PC_WRAPPER = """
(() => {
  const Native = window.RTCPeerConnection;
  if (!Native) return;
  window.__lkPeers = [];
  class TrackingRTCPeerConnection extends Native {
    constructor(...args) {
      super(...args);
      window.__lkPeers.push(this);
    }
  }
  window.RTCPeerConnection = TrackingRTCPeerConnection;
})();
"""


class BrowserUnavailable(RuntimeError):
    """The browser or its driver could not be started."""


def _browser_server():
    """Serve the page and the pinned SDK bundle from 127.0.0.1."""
    sdk_path = browser_sdk_path()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = (BROWSER_DIR / "index.html").read_bytes()
                ctype = "text/html; charset=utf-8"
            elif path == "/main.js":
                body = (BROWSER_DIR / "main.js").read_bytes()
                ctype = "text/javascript; charset=utf-8"
            elif path == "/vendor/livekit-client.esm.mjs" and sdk_path.exists():
                body = sdk_path.read_bytes()
                ctype = "text/javascript; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_browser_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _run_browser_check(base_url, config, timeout, insecure):
    """Drive Chrome through the page and return its result and signalling URLs."""
    if not HAVE_PLAYWRIGHT:
        raise BrowserUnavailable(
            f"playwright not installed ({PLAYWRIGHT_IMPORT_ERROR})")
    channel = os.environ.get("PLAYWRIGHT_CHROME_CHANNEL", "chrome")
    chrome_path = os.environ.get(
        "CHROME_PATH",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    headless = os.environ.get("PLAYWRIGHT_HEADED") != "1"
    args = ["--autoplay-policy=no-user-gesture-required"]
    if insecure:
        args.append("--ignore-certificate-errors")
    wait_ms = max(int(timeout * 1000), 10000)
    page_timeout = max(int(timeout * 4000), 20000)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                channel=channel, headless=headless, args=args)
        except PlaywrightError:
            if not os.path.exists(chrome_path):
                raise BrowserUnavailable(
                    f"Chrome channel {channel!r} is unavailable and "
                    f"{chrome_path} does not exist")
            browser = playwright.chromium.launch(
                executable_path=chrome_path, headless=headless, args=args)
        try:
            context = browser.new_context(ignore_https_errors=insecure)
            page = context.new_page()
            seen = []
            page.on(
                "websocket",
                lambda ws: seen.append(ws.url) if "/rtc" in ws.url else None,
            )
            page.add_init_script(
                "window.__lkSdkCdnUrl = "
                f"{json.dumps(config.get('sdkCdnUrl'))};")
            page.add_init_script(_BROWSER_PC_WRAPPER)
            page.goto(base_url + "/", wait_until="domcontentloaded",
                      timeout=wait_ms)
            page.wait_for_function(
                "() => window.__lkStartReady === true", timeout=page_timeout)
            page.evaluate("(cfg) => window.__lkStart(cfg)", config)
            page.wait_for_selector(
                '#status[data-state="ready"], #status[data-state="failed"]',
                timeout=page_timeout)
            result = page.evaluate("() => window.__lkResult")
            return {
                "result": result,
                "websocket_urls": seen,
                "chrome": browser.version,
                "body_text": page.inner_text("body"),
                "content": page.content(),
            }
        finally:
            browser.close()


def run_browser_phase(url, api_key, api_secret, room, identity,
                      server_candidates, timeout, insecure_tls, verbose):
    """Phase 8: a real Chrome/livekit-client browser reports its selected path."""
    print(bold("Phase 8: Browser path (Chrome + livekit-client)"))
    skip = browser_skip_reason()
    if skip:
        print(f"   result         : {yellow('SKIP')}  ({skip})")
        print()
        return None

    token = make_token(api_key, api_secret, room, f"{identity}-browser")
    server = thread = None
    try:
        server, thread, base_url = _browser_server()
        config = {
            "url": url,
            "token": token,
            "sdkVersion": browser_package_version(),
            "sdkCdnUrl": (
                None if browser_sdk_path().exists() else BROWSER_CDN_URL),
            "timeoutMs": max(int(timeout * 1000), 15000),
            "websocketTimeoutMs": max(int(timeout * 1000), 15000),
            "statsTimeoutMs": max(int(timeout * 4000), 20000),
        }
        checked = _run_browser_check(base_url, config, timeout, insecure_tls)
        raw = checked.get("result") or {}
        if raw.get("errorCode") == "sdk_unavailable":
            print(f"   result         : {yellow('SKIP')}  "
                  "(livekit-client unavailable)")
            print()
            return None
        ws_url = next(
            (u for u in checked.get("websocket_urls", []) if "/rtc" in u),
            None)
        if not ws_url:
            print(f"   result         : {FAIL}  "
                  "(no /rtc signaling WebSocket observed)")
            print()
            return {"ok": False, "pcs": [], "error": "no signaling URL"}
        try:
            verify_rtc_url(ws_url, browser_package_version())
        except BrowserUrlError as exc:
            print(f"   signaling      : {FAIL}  ({redact_secrets(exc)})")
            print(f"   result         : {FAIL}")
            print()
            return {"ok": False, "pcs": [], "error": redact_secrets(exc)}

        pcs = [summarize_browser_pc(pc) for pc in raw.get("pcs") or []]
        for pc in pcs:
            pc["inferred_remote"] = inferred_remote_addresses(
                pc.get("pair"), server_candidates)
        result = {
            "ok": browser_result_ok({"pcs": pcs}),
            "chrome": checked.get("chrome"),
            "sdk": raw.get("sdk") or {},
            "signaling": rtc_url_facts(ws_url),
            "pcs": pcs,
            "error": redact_secrets(raw.get("error") or ""),
        }
        signaling = result["signaling"]
        mode = ("join_request" if signaling["has_join_request"]
                else "query params")
        sdk = result["sdk"]
        print(f"   client         : Chrome {result['chrome']} / "
              f"livekit-client {sdk.get('version')} "
              f"({sdk.get('source') or 'unknown'})")
        print(f"   signaling      : {signaling['scheme']}://"
              f"{signaling['host']}{signaling['path']} ({mode})")
        if len(pcs) == 1:
            label = pcs[0]["label"]
            if label == "single":
                label = "publisher+subscriber"
            print(f"   peer connection: 1 ({label})")
        elif pcs:
            print(f"   peer connections: {len(pcs)}")
        gathered = sorted({t for pc in pcs for t in pc["local_types"]})
        print(f"   gathered       : {', '.join(gathered) or 'none'}")
        for pc in pcs:
            label = pc["label"]
            pair = pc["pair"]
            if not pair:
                print(f"   {label:<15}: no selected pair "
                      f"(connection={pc['connection_state']}, "
                      f"ice={pc['ice_state']}, dtls={pc['dtls_state']})")
                continue
            local = pair["local"]
            remote = pair["remote"]
            owner = pc["role"]["owner"] or "unknown"
            print(f"   {label:<15}: {browser_path(pair)}  "
                  f"local {local['type']} "
                  f"{format_address(local['address'])}:{local['port']}  "
                  f"remote {remote['type']} "
                  f"{format_address(remote['address'])}:{remote['port']}  "
                  f"role={owner}/{pc['role']['raw']}")
            print(f"                    pair state={pair['state']} "
                  f"nominated={pair['nominated']} "
                  f"ice={pc['ice_state']} dtls={pc['dtls_state']}")
            inferred = pc.get("inferred_remote") or []
            if inferred:
                print(f"                    inferred       : remote prflx "
                      f"{', '.join(inferred)} "
                      f"(from the SFU's public candidates; "
                      f"port {remote['port']})")
        private = [
            fact for pc in pcs for fact in private_address_facts(pc)
        ]
        print(f"   private addr   : "
              f"{', '.join(private) if private else 'none'}")
        if result["error"]:
            print(f"   error          : {result['error']}")
        print(f"   result         : {OK if result['ok'] else FAIL}")
        print()
        return result
    except BrowserUnavailable as exc:
        print(f"   result         : {yellow('SKIP')}  ({exc})")
        print()
        return None
    except Exception as exc:  # pragma: no cover - live browser failure
        print(f"   error          : {type(exc).__name__}: "
              f"{redact_secrets(exc)}   {FAIL}")
        print()
        return {"ok": False, "pcs": [], "error": redact_secrets(exc)}
    finally:
        if server is not None:
            _stop_browser_server(server, thread)


# ─── Main ────────────────────────────────────────────────────────────
class _SafeDefaultsHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Print defaults for normal options, but never for credentials."""

    SECRET_DESTS = {"api_key", "api_secret"}

    def _get_help_string(self, action):
        if action.dest in self.SECRET_DESTS:
            return action.help or ""
        return super()._get_help_string(action)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Test a LiveKit server and its STUN/TURN relay with real protocol traffic.",
        formatter_class=_SafeDefaultsHelpFormatter,
    )
    p.add_argument("--url", default=os.environ.get("LIVEKIT_URL"),
                   help="LiveKit URL, e.g. wss://host  (env LIVEKIT_URL)")
    p.add_argument("--api-key", default=os.environ.get("LIVEKIT_API_KEY"),
                   help="API key (env LIVEKIT_API_KEY)")
    p.add_argument("--api-secret", default=os.environ.get("LIVEKIT_API_SECRET"),
                   help="API secret (env LIVEKIT_API_SECRET)")
    p.add_argument("--room", default="connectivity-check", help="room name to join")
    p.add_argument("--identity", default="connectivity-check", help="participant identity")
    p.add_argument("--protocol", type=int, default=15, help="LiveKit signal protocol version")
    p.add_argument("--timeout", type=float, default=5.0, help="per-operation timeout (seconds)")
    p.add_argument("--insecure-tls", action="store_true",
                   help="do not verify TURN/TLS certificates (for self-signed test servers)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print TURN credential details and aioice ICE logs")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    missing = [n for n, v in (("--url/LIVEKIT_URL", args.url),
                              ("--api-key/LIVEKIT_API_KEY", args.api_key),
                              ("--api-secret/LIVEKIT_API_SECRET", args.api_secret)) if not v]
    if missing:
        sys.stderr.write("Missing required config: " + ", ".join(missing) + "\n")
        return 2

    print()
    print(bold("LiveKit connectivity test"))
    print(f"   target : {args.url}")
    print(f"   time   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Phase 1
    try:
        join, server_candidates = run_discovery(
            args.url, args.api_key, args.api_secret,
            args.room, args.identity, args.protocol, args.timeout)
    except Exception as e:
        print(bold("Phase 1: LiveKit signaling + discovery"))
        print(f"   result         : {FAIL}  ({type(e).__name__}: {e})")
        print()
        print(bold("Summary"))
        print(f"   {FAIL} LiveKit signaling")
        return 1

    stun_urls, turns = classify_ice_servers(join.ice_servers)
    print_discovery(join, stun_urls, turns, args.verbose, server_candidates)

    # Phases 2-6
    stun_ok, stun_used = run_stun(stun_urls, turns, args.timeout)
    turn_results = run_turn_phase(turns, args.timeout, args.insecure_tls)
    ice_results = run_ice_phase(
        turns, args.timeout, args.insecure_tls, args.verbose, server_candidates,
        _client_reflexive_is_private(stun_used))
    direct_ok = run_direct_phase(
        args.url, args.api_key, args.api_secret, args.room, args.identity,
        args.protocol, stun_urls, turns, args.timeout, args.verbose)
    combined_ok, combined_info = run_combined_phase(
        args.url, args.api_key, args.api_secret, args.room, args.identity,
        args.protocol, stun_urls, turns, args.timeout, args.verbose)
    browser_info = run_browser_phase(
        args.url, args.api_key, args.api_secret, args.room, args.identity,
        server_candidates, args.timeout, args.insecure_tls, args.verbose)

    # Summary
    print(bold("Summary"))
    print(f"   {OK} LiveKit signaling")
    if stun_ok is None:
        print(f"   {yellow('SKIP')} STUN binding")
    else:
        print(f"   {OK if stun_ok else FAIL} STUN binding")
    udp_eps = {k: v for k, v in turn_results.items() if "transport=udp" in k}
    tcp_eps = {k: v for k, v in turn_results.items() if "transport=tcp" in k}

    def summarize(label, eps):
        if not eps:
            print(f"   {yellow('SKIP')} {label} (none advertised)")
            return True  # not a failure
        for name, ok in eps.items():
            print(f"   {OK if ok else FAIL} {label}: {name}")
        return any(eps.values())

    udp_ok = summarize("TURN relay UDP", udp_eps)
    tcp_ok = summarize("TURN relay TCP/TLS", tcp_eps)

    ice_udp = {k: v for k, v in ice_results.items() if "transport=udp" in k}
    ice_tcp = {k: v for k, v in ice_results.items() if "transport=tcp" in k}
    ice_udp_ok = summarize("ICE relay UDP", ice_udp)
    ice_tcp_ok = summarize("ICE relay TCP/TLS", ice_tcp)
    if direct_ok is None:
        print(f"   {yellow('SKIP')} Direct connectivity (no TURN)")
    else:
        print(f"   {OK if direct_ok else FAIL} Direct connectivity (no TURN)")
    if combined_ok is None:
        print(f"   {yellow('SKIP')} Path selection (direct vs TURN)")
    else:
        label = ""
        if combined_info:
            nominated = combined_info["nominated"]
            best = combined_info["best"]
            controller = combined_info.get("controller")
            if nominated:
                label = f": {nominated} nominated"
                if controller:
                    label += f" by {controller}"
                if best and best != nominated:
                    label += f" ({best} available)"
            elif best:
                label = f": {best}"
        print(f"   {OK if combined_ok else FAIL} Path selection (direct vs TURN){label}")
    if browser_info is None:
        print(f"   {yellow('SKIP')} Browser path (Chrome + livekit-client)")
    else:
        print(f"   {OK if browser_info['ok'] else FAIL} Browser path "
              f"(Chrome + livekit-client){browser_summary_label(browser_info)}")
    print()

    # STUN server(s) used by the testing (printed last)
    print(bold("STUN server(s) used:"))
    if stun_used:
        for server, reflexive in stun_used:
            detail = f"reflexive {reflexive}" if reflexive else "no response"
            print(f"   {server}   ({detail})")
    else:
        print("   (none - no STUN-capable target was tested)")
    print()

    all_ok = ((stun_ok is not False) and udp_ok and tcp_ok and ice_udp_ok
              and ice_tcp_ok and (direct_ok is not False)
              and (combined_ok is not False)
              and (browser_info is None or browser_info["ok"]))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
