#!/usr/bin/env python3
"""Unit tests for the direct-connect phase of test-livekit.py.

test-livekit.py is named with a hyphen, so it cannot be imported normally;
it is loaded here through importlib.

Run with:
    .venv/bin/python -m pytest test_direct_connect.py -v
"""

import importlib.util
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).with_name("test-livekit.py")
_spec = importlib.util.spec_from_file_location("test_livekit", SCRIPT)
lk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lk)


def test_signal_url_carries_token_and_query():
    url = lk.signal_url(
        "wss://lk.example.com/", "api-key",
        "0123456789abcdef0123456789abcdef",
        "room-1", "identity-1", 15,
    )
    assert url.startswith("wss://lk.example.com/rtc?")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["auto_subscribe"] == ["0"]
    assert query["protocol"] == ["15"]
    assert query["sdk"] == ["python"]
    assert query["version"] == ["1.0.0"]
    # access_token must be a signed JWT (three dot-separated parts).
    assert len(query["access_token"][0].split(".")) == 3


def test_direct_ice_servers_keeps_only_stun():
    servers = lk.direct_ice_servers([
        "stun:stun.example.com:3478",
        "turn:turn.example.com:3478?transport=udp",
        "turns:turn.example.com:443?transport=tcp",
    ])
    urls = [
        u
        for server in servers
        for u in (server.urls if isinstance(server.urls, list) else [server.urls])
    ]
    assert urls == ["stun:stun.example.com:3478"]


def test_direct_ice_servers_none_when_no_stun():
    assert lk.direct_ice_servers([]) is None


def test_combined_ice_servers_includes_stun_and_turn_with_credentials():
    turns = [
        lk.TurnEndpoint("turn", "turn.example.com", 3478, "udp", "user-a", "cred-a"),
        lk.TurnEndpoint("turns", "turn.example.com", 5349, "tcp", "user-a", "cred-a"),
    ]
    servers = lk.combined_ice_servers([], turns)
    # Fallback STUN first (the TURN host answers Binding), then the TURN servers.
    assert servers[0].urls == ["stun:turn.example.com:3478"]
    assert servers[1].urls == ["turn:turn.example.com:3478?transport=udp"]
    assert servers[1].username == "user-a"
    assert servers[1].credential == "cred-a"
    assert servers[2].urls == ["turns:turn.example.com:5349?transport=tcp"]
    assert servers[2].username == "user-a"
    assert servers[2].credential == "cred-a"


def test_combined_ice_servers_uses_advertised_stun():
    turns = [lk.TurnEndpoint("turn", "turn.example.com", 3478, "udp", "u", "c")]
    servers = lk.combined_ice_servers(["stun:stun.example.com:3478"], turns)
    assert servers[0].urls == ["stun:stun.example.com:3478"]
    assert servers[1].urls == ["turn:turn.example.com:3478?transport=udp"]


def test_combined_ice_servers_none_without_servers():
    assert lk.combined_ice_servers([], []) is None


def test_pair_priority_matches_rfc8445():
    local = SimpleNamespace(priority=1000)
    remote = SimpleNamespace(priority=2000)
    # G=1000, D=2000 -> 2^32*1000 + 2*2000 + 0
    assert lk.pair_priority(local, remote, True) == (1 << 32) * 1000 + 2 * 2000
    # Controlling=False swaps G and D, adding the +1 tie-breaker.
    assert lk.pair_priority(local, remote, False) == (1 << 32) * 1000 + 2 * 2000 + 1


def _fake_pair(state_name, local_priority, remote_priority, local_type="host"):
    return SimpleNamespace(
        state=SimpleNamespace(name=state_name),
        local_candidate=SimpleNamespace(
            type=local_type, host="10.0.0.1", port=1111,
            transport="udp", priority=local_priority),
        remote_candidate=SimpleNamespace(
            type="host", host="10.0.0.2", port=2222,
            transport="udp", priority=remote_priority),
    )


def test_successful_pairs_returns_only_succeeded_with_priority():
    host_priority = 2130706431
    relay_priority = 16777215
    transport = SimpleNamespace(_connection=SimpleNamespace(
        ice_controlling=True,
        _check_list=[
            _fake_pair("SUCCEEDED", host_priority, host_priority),
            _fake_pair("IN_PROGRESS", host_priority, host_priority),
            _fake_pair("SUCCEEDED", relay_priority, host_priority,
                       local_type="relay"),
        ],
    ))
    pairs = lk.successful_pairs(transport)
    assert len(pairs) == 2
    by_type = {summary["local"]["type"]: priority for summary, priority in pairs}
    assert by_type["host"] > by_type["relay"]


def test_successful_pairs_empty_without_internals():
    assert lk.successful_pairs(None) == []
    assert lk.successful_pairs(SimpleNamespace()) == []


def test_controlling_agent_reports_local_or_remote():
    assert lk.controlling_agent(SimpleNamespace(
        _connection=SimpleNamespace(ice_controlling=True))) == "client"
    assert lk.controlling_agent(SimpleNamespace(
        _connection=SimpleNamespace(ice_controlling=False))) == "SFU"
    assert lk.controlling_agent(None) is None
    assert lk.controlling_agent(SimpleNamespace()) is None


def test_selected_path_classifies_direct_and_relay():
    direct = {"local": {"type": "srflx"}, "remote": {"type": "host"}}
    relay_local = {"local": {"type": "relay"}, "remote": {"type": "host"}}
    relay_remote = {"local": {"type": "host"}, "remote": {"type": "relay"}}
    assert lk.selected_path(direct) == "direct"
    assert lk.selected_path(relay_local) == "relay"
    assert lk.selected_path(relay_remote) == "relay"
    assert lk.selected_path(None) is None


def test_direct_stun_urls_prefers_advertised_stun():
    turns = [lk.TurnEndpoint("turn", "turn.example.com", 3478, "udp", "u", "c")]
    assert lk.direct_stun_urls(["stun:stun.example.com:3478"], turns) == [
        "stun:stun.example.com:3478"]


def test_direct_stun_urls_falls_back_to_turn_udp_host():
    turns = [
        lk.TurnEndpoint("turn", "turn.example.com", 3478, "udp", "u", "c"),
        lk.TurnEndpoint("turns", "turn.example.com", 5349, "tcp", "u", "c"),
    ]
    assert lk.direct_stun_urls([], turns) == ["stun:turn.example.com:3478"]
    assert lk.direct_stun_urls([], []) == []


def test_parse_remote_candidate_udp_host():
    candidate_init = json.dumps({
        "candidate": "candidate:1 1 udp 2122260223 10.0.0.5 7882 typ host generation 0",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
        "usernameFragment": "abcd",
    })
    candidate = lk.parse_remote_candidate(candidate_init)
    assert candidate.type == "host"
    assert candidate.protocol == "udp"
    assert candidate.ip == "10.0.0.5"
    assert candidate.port == 7882
    assert candidate.sdpMid == "0"
    assert candidate.sdpMLineIndex == 0


def test_parse_remote_candidate_rejects_empty_and_malformed():
    assert lk.parse_remote_candidate(json.dumps({"candidate": ""})) is None
    assert lk.parse_remote_candidate("not json") is None
    assert lk.parse_remote_candidate(json.dumps({"candidate": "candidate:junk"})) is None


def _nominated_transport():
    pair = SimpleNamespace(
        local_candidate=SimpleNamespace(
            type="srflx", host="203.0.113.9", port=54321, transport="udp"),
        remote_candidate=SimpleNamespace(
            type="host", host="10.0.0.7", port=7882, transport="udp"),
    )
    return SimpleNamespace(
        _connection=SimpleNamespace(_nominated={1: pair}))


def test_selected_pair_summary_reads_nominated_pair():
    summary = lk.selected_pair_summary(_nominated_transport())
    assert summary["local"] == {
        "type": "srflx", "host": "203.0.113.9", "port": 54321, "transport": "udp",
    }
    assert summary["remote"] == {
        "type": "host", "host": "10.0.0.7", "port": 7882, "transport": "udp",
    }


def test_selected_pair_summary_none_without_nomination():
    assert lk.selected_pair_summary(
        SimpleNamespace(_connection=SimpleNamespace(_nominated={}))) is None
    assert lk.selected_pair_summary(SimpleNamespace()) is None
    assert lk.selected_pair_summary(None) is None


def test_local_candidate_types_deduplicates_and_sorts():
    gatherer = SimpleNamespace(getLocalCandidates=lambda: [
        SimpleNamespace(type="srflx"),
        SimpleNamespace(type="host"),
        SimpleNamespace(type="host"),
    ])
    transport = SimpleNamespace(iceGatherer=gatherer)
    assert lk.local_candidate_types(transport) == ["host", "srflx"]
    assert lk.local_candidate_types(None) == []


def test_build_answer_message_carries_offer_id_and_sdp():
    message = lk.build_answer_message(7, "v=0\r\n")
    assert message.WhichOneof("message") == "answer"
    assert message.answer.type == "answer"
    assert message.answer.id == 7
    assert message.answer.sdp == "v=0\r\n"

    parsed = lk.SignalResponse()
    parsed.ParseFromString(message.SerializeToString())
    assert parsed.answer.id == 7
    assert parsed.answer.sdp == "v=0\r\n"
