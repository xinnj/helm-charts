#!/usr/bin/env python3
"""Unit and page tests for the real-browser phase of test-livekit.py.

test-livekit.py is named with a hyphen, so it is loaded through importlib.

Run with:
    .venv/bin/python -m pytest test_browser_phase.py -v
"""

import base64
import importlib.util
import json
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("test-livekit.py")
_spec = importlib.util.spec_from_file_location("test_livekit", SCRIPT)
lk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lk)


def _decode_jwt(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _pc_payload(local_type="srflx", remote_type="prflx",
                local_address="203.0.113.9", remote_address="47.93.232.118",
                role="controlled", connection_state="connected",
                label="single"):
    stats = [
        {"id": "T", "type": "transport", "selectedCandidatePairId": "P",
         "iceRole": role, "iceState": "connected", "dtlsState": "connected"},
        {"id": "P", "type": "candidate-pair", "state": "succeeded",
         "nominated": True, "localCandidateId": "L",
         "remoteCandidateId": "R", "priority": 100},
        {"id": "L", "type": "local-candidate", "candidateType": local_type,
         "address": local_address, "port": 50001, "protocol": "udp"},
        {"id": "R", "type": "remote-candidate", "candidateType": remote_type,
         "address": remote_address, "port": 50002, "protocol": "udp"},
    ]
    return {"label": label, "association": "sdk",
            "connectionState": connection_state,
            "iceConnectionState": "connected",
            "iceGatheringState": "complete", "stats": stats}


def test_make_token_has_room_join_grant():
    token = lk.make_token("key", "s" * 32, "room-1", "identity-1")
    claims = _decode_jwt(token)
    assert claims["sub"] == "identity-1"
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["room"] == "room-1"


def test_rtc_url_facts_omits_access_token():
    facts = lk.rtc_url_facts(
        "wss://lk.example.com/rtc/v1?access_token=SECRET&join_request=abc")
    assert "SECRET" not in json.dumps(facts)
    assert facts["has_token"] is True
    assert facts["has_join_request"] is True
    assert facts["path"] == "/rtc/v1"


def test_verify_rtc_url_accepts_v1():
    mode = lk.verify_rtc_url(
        "wss://lk.example.com/rtc/v1?access_token=SECRET&join_request=abc")
    assert mode["mode"] == "v1"


def test_verify_rtc_url_accepts_v0():
    url = ("wss://lk.example.com/rtc?access_token=SECRET&auto_subscribe=1"
           "&protocol=17&sdk=js&version=2.23.0")
    assert lk.verify_rtc_url(url, "2.23.0")["mode"] == "v0"


def test_verify_rtc_url_rejects_v1_without_join_request():
    with pytest.raises(lk.BrowserUrlError):
        lk.verify_rtc_url("wss://lk.example.com/rtc/v1?access_token=SECRET")


def test_verify_rtc_url_rejects_v0_missing_version():
    with pytest.raises(lk.BrowserUrlError):
        lk.verify_rtc_url(
            "wss://lk.example.com/rtc?access_token=SECRET&auto_subscribe=1"
            "&protocol=17&sdk=js")


def test_select_candidate_pair_uses_transport_id():
    stats = [
        {"id": "T", "type": "transport", "selectedCandidatePairId": "P2"},
        {"id": "P1", "type": "candidate-pair", "state": "succeeded",
         "priority": 10},
        {"id": "P2", "type": "candidate-pair", "state": "succeeded",
         "priority": 5},
    ]
    assert lk.select_candidate_pair(stats, stats[0])["id"] == "P2"


def test_select_candidate_pair_falls_back_to_nominated():
    stats = [{"id": "P1", "type": "candidate-pair", "state": "succeeded",
              "nominated": True, "priority": 1}]
    assert lk.select_candidate_pair(stats, None)["id"] == "P1"


def test_select_candidate_pair_returns_none_when_absent():
    assert lk.select_candidate_pair([{"id": "T", "type": "transport"}],
                                    None) is None


def test_summarize_candidate_uses_address_then_ip():
    index = {"C1": {"id": "C1", "candidateType": "srflx",
                    "address": "203.0.113.9", "port": 1, "protocol": "udp"}}
    assert lk.summarize_candidate(index, "C1")["address"] == "203.0.113.9"
    index = {"C2": {"id": "C2", "candidateType": "host",
                    "ip": "10.0.0.1", "port": 2, "protocol": "udp"}}
    assert lk.summarize_candidate(index, "C2")["address"] == "10.0.0.1"


def test_summarize_browser_pc_direct_and_relay():
    info = lk.summarize_browser_pc(_pc_payload())
    assert info["role"]["owner"] == "SFU"
    assert lk.browser_path(info["pair"]) == "direct"
    assert lk.browser_pc_ok(info) is True

    relay = lk.summarize_browser_pc(_pc_payload("relay", "host"))
    assert lk.browser_path(relay["pair"]) == "relay"
    assert lk.browser_pc_ok(relay) is True


def test_browser_path_and_address_scope():
    assert lk.address_scope("10.0.0.1") == "private"
    assert lk.address_scope("47.93.232.118") == "public"
    assert lk.address_scope("abc.local") == "unknown"
    assert lk.address_scope(None) == "unknown"


def test_ice_role_owner():
    assert lk.ice_role_owner("controlling") == "client"
    assert lk.ice_role_owner("controlled") == "SFU"
    assert lk.ice_role_owner(None) is None


def test_browser_result_ok_requires_all_pcs():
    good = lk.summarize_browser_pc(_pc_payload())
    bad = lk.summarize_browser_pc(
        {"label": "publisher", "connectionState": "connected", "stats": []})
    assert lk.browser_result_ok({"pcs": [good]}) is True
    assert lk.browser_result_ok({"pcs": [good, bad]}) is False
    assert lk.browser_result_ok({"pcs": []}) is False


def test_redact_secrets():
    text = ("failed wss://lk/rtc?access_token=SECRET "
            "eyJhbGciOi.eyJzdWIiOi.sig")
    redacted = lk.redact_secrets(text)
    assert "SECRET" not in redacted
    assert "eyJhbGciOi" not in redacted


def test_browser_summary_label():
    result = {"pcs": [lk.summarize_browser_pc(_pc_payload())]}
    assert lk.browser_summary_label(result) == ": single direct"


def test_private_address_facts():
    info = lk.summarize_browser_pc(
        _pc_payload(local_type="host", local_address="10.184.102.43"))
    assert lk.private_address_facts(info) == ["local host 10.184.102.43"]


def test_format_address_hides_redacted():
    assert lk.format_address("10.0.0.1") == "10.0.0.1"
    assert lk.format_address("") == "<hidden>"
    assert lk.format_address(None) == "<hidden>"


def test_inferred_remote_addresses_hidden_prflx():
    pair = lk.summarize_browser_pc(_pc_payload())["pair"]
    pair["remote"]["address"] = None
    candidates = [{"ip": "10.0.2.49"}, {"ip": "47.93.232.118"}]
    assert lk.inferred_remote_addresses(pair, candidates) == ["47.93.232.118"]
    assert lk.inferred_remote_addresses(pair, [{"ip": "10.0.2.49"}]) == []
    assert lk.inferred_remote_addresses(
        pair, [{"ip": "47.93.232.118"}, {"ip": "8.8.8.8"}]) == [
            "47.93.232.118", "8.8.8.8"]
    visible = lk.summarize_browser_pc(_pc_payload())["pair"]
    assert lk.inferred_remote_addresses(visible, candidates) == []
    assert lk.inferred_remote_addresses(None, candidates) == []


def _start_fake_ws():
    from websockets.sync.server import serve

    seen = []

    def process_request(connection, request):
        seen.append(request.path)
        return None

    def handler(connection):
        return

    server = serve(handler, "127.0.0.1", 0, process_request=process_request)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port, seen


def _browser_ready():
    if not lk.HAVE_PLAYWRIGHT:
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return lk.browser_sdk_path().exists()


def _run_or_skip(base_url, config):
    try:
        return lk._run_browser_check(
            base_url, config, timeout=5, insecure=False)
    except lk.BrowserUnavailable as exc:
        pytest.skip(str(exc))


def test_browser_page_rtc_url_and_failed_dom():
    if not _browser_ready():
        pytest.skip("Playwright, Chrome, or the livekit-client bundle is missing")
    server, thread, base_url = lk._browser_server()
    fake, fake_thread, ws_port, seen = _start_fake_ws()
    try:
        token = lk.make_token("test-key", "s" * 32,
                              "test-room", "test-identity")
        config = {
            "url": f"ws://127.0.0.1:{ws_port}",
            "token": token,
            "sdkVersion": lk.browser_package_version(),
            "sdkCdnUrl": None,
            "timeoutMs": 5000,
            "websocketTimeoutMs": 2000,
            "statsTimeoutMs": 3000,
        }
        checked = _run_or_skip(base_url, config)
        raw = checked["result"]
        assert raw["errorCode"] == "join_failed"
        assert raw["ok"] is False
        assert seen, "the browser did not open a signaling WebSocket"
        assert checked["websocket_urls"], \
            "Playwright did not capture the signaling WebSocket"
        info = lk.rtc_url_facts(checked["websocket_urls"][0])
        assert info["has_token"] is True
        assert info["path"].endswith("/rtc") or \
            info["path"].endswith("/rtc/v1")
        if info["path"].endswith("/rtc/v1"):
            assert info["has_join_request"] is True
        else:
            assert info["params"].get("sdk") == "js"
        assert token not in checked["body_text"]
        assert token not in checked["content"]
    finally:
        lk._stop_browser_server(server, thread)
        fake.shutdown()
        fake_thread.join(timeout=5)


def test_browser_page_reports_sdk_unavailable(monkeypatch):
    if not lk.HAVE_PLAYWRIGHT:
        pytest.skip("playwright is not installed")
    missing = Path("/nonexistent/livekit-client.esm.mjs")
    monkeypatch.setattr(lk, "browser_sdk_path", lambda: missing)
    server, thread, base_url = lk._browser_server()
    try:
        token = lk.make_token("test-key", "s" * 32,
                              "test-room", "test-identity")
        config = {
            "url": "ws://127.0.0.1:1",
            "token": token,
            "sdkVersion": lk.browser_package_version(),
            "sdkCdnUrl": None,
            "timeoutMs": 3000,
            "websocketTimeoutMs": 1000,
            "statsTimeoutMs": 1000,
        }
        checked = _run_or_skip(base_url, config)
        assert checked["result"]["errorCode"] == "sdk_unavailable"
        assert checked["result"]["ok"] is False
        assert token not in checked["body_text"]
    finally:
        lk._stop_browser_server(server, thread)
