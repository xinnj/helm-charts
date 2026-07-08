#!/usr/bin/env python3
"""
LiveKit + STUN/TURN real-protocol connectivity tester.

Unlike a port scan, this performs the *actual* protocols end to end:

  Phase 1  LiveKit signaling  - open the WSS /rtc session, parse the JoinResponse,
                                prove signaling works and DISCOVER the STUN/TURN
                                servers (URLs + credentials) the server hands clients.
  Phase 2  STUN binding       - send a real Binding request and read back the
                                server-reflexive (public) address.
  Phase 3  TURN relay / UDP   - full authenticated Allocate -> CreatePermission ->
                                Send/Data: relay a random token between two
                                allocations and verify it arrives. Proves the TURN
                                server actually relays media over UDP.
  Phase 4  TURN relay / TCP   - the same relay proof, but the control channel runs
                                over TLS/TCP (turns:...?transport=tcp).

Everything the tool tests is discovered from the LiveKit JoinResponse, so the
STUN/TURN servers and TURN credentials are exactly what a real client would use.
No secrets are hard-coded: connection details come from environment variables
(or CLI flags), and the TURN MESSAGE-INTEGRITY key is derived from the
server-issued username/credential.

Configuration (env var  ->  CLI flag override):
    LIVEKIT_URL          --url            wss://host[:port]
    LIVEKIT_API_KEY      --api-key
    LIVEKIT_API_SECRET   --api-secret

Dependencies:
    pip install livekit websockets

Exit code: 0 if every attempted phase passed, non-zero otherwise.
"""

import argparse
import asyncio
import hashlib
import hmac
import os
import socket
import ssl
import struct
import sys
import time
import urllib.parse

# ─── Optional dependencies (fail with a helpful message) ─────────────
try:
    from livekit.api import AccessToken, VideoGrants
    from livekit.protocol.rtc import SignalResponse
    import websockets
except ImportError as exc:  # pragma: no cover - environment guard
    sys.stderr.write(
        f"Missing dependency: {exc.name}\n"
        "Install with:  pip install livekit websockets\n"
    )
    sys.exit(3)

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
async def discover(url, api_key, api_secret, room, identity, protocol, timeout):
    token = (
        AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_grants(VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    query = urllib.parse.urlencode({
        "access_token": token,
        "auto_subscribe": "0",
        "protocol": str(protocol),
        "sdk": "python",
        "version": "1.0.0",
    })
    ws_url = f"{url.rstrip('/')}/rtc?{query}"
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
    return resp.join


def print_discovery(join, stun_urls, turns, verbose):
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


# ─── Main ────────────────────────────────────────────────────────────
def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Test a LiveKit server and its STUN/TURN relay with real protocol traffic.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
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
    p.add_argument("-v", "--verbose", action="store_true", help="print TURN credentials/auth details")
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
        join = run_discovery(args.url, args.api_key, args.api_secret,
                             args.room, args.identity, args.protocol, args.timeout)
    except Exception as e:
        print(bold("Phase 1: LiveKit signaling + discovery"))
        print(f"   result         : {FAIL}  ({type(e).__name__}: {e})")
        print()
        print(bold("Summary"))
        print(f"   {FAIL} LiveKit signaling")
        return 1

    stun_urls, turns = classify_ice_servers(join.ice_servers)
    print_discovery(join, stun_urls, turns, args.verbose)

    # Phases 2-4
    stun_ok, stun_used = run_stun(stun_urls, turns, args.timeout)
    turn_results = run_turn_phase(turns, args.timeout, args.insecure_tls)

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

    all_ok = (stun_ok is not False) and udp_ok and tcp_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
