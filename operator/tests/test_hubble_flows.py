"""hubble_flows.py 단위 테스트 - subprocess는 MagicMock으로 대체(실제 hubble CLI/relay 불필요)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from k8s_traffic_operator.dashboard import hubble_flows as hf


def _flow_line(
    *,
    verdict="FORWARDED",
    direction="EGRESS",
    src_ns="shop", src_pod="checkout-abc",
    dst_ns="shop", dst_pod="cart-def",
    dst_port=8080,
    proto="TCP",
    time_="2026-07-19T12:00:00.000000000Z",
) -> str:
    flow = {
        "flow": {
            "time": time_,
            "verdict": verdict,
            "traffic_direction": direction,
            "l4": {proto: {"destination_port": dst_port}},
            "source": {"namespace": src_ns, "pod_name": src_pod} if src_ns else {"labels": ["reserved:host"]},
            "destination": {"namespace": dst_ns, "pod_name": dst_pod} if dst_ns else {"labels": ["reserved:world"]},
        }
    }
    import json
    return json.dumps(flow)


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# --------------------------------------------------------------------------- _entity_label
def test_entity_label_prefers_namespace_pod():
    ep = hf._entity_label({"namespace": "shop", "pod_name": "checkout-abc"})
    assert ep.label == "shop/checkout-abc"
    assert ep.namespace == "shop"


def test_entity_label_falls_back_to_reserved_labels():
    ep = hf._entity_label({"labels": ["reserved:host", "reserved:kube-apiserver"]})
    assert ep.label == "host+kube-apiserver"


def test_entity_label_unknown_when_nothing_present():
    ep = hf._entity_label({})
    assert ep.label == "unknown"


# --------------------------------------------------------------------------- _parse_flow_line
def test_parse_flow_line_extracts_core_fields():
    ev = hf._parse_flow_line(_flow_line())
    assert ev.verdict == "FORWARDED"
    assert ev.direction == "EGRESS"
    assert ev.protocol == "TCP"
    assert ev.src.label == "shop/checkout-abc"
    assert ev.dst.label == "shop/cart-def"
    assert ev.dst_port == 8080


def test_parse_flow_line_ignores_non_flow_lines():
    assert hf._parse_flow_line('{"lostEvents": {"numEventsLost": 3}}') is None
    assert hf._parse_flow_line("") is None
    assert hf._parse_flow_line("not json at all") is None


def test_parse_flow_line_handles_udp_and_icmp():
    import json
    udp_line = json.dumps({"flow": {"time": "t", "verdict": "FORWARDED", "l4": {"UDP": {"destination_port": 53}},
                                     "source": {}, "destination": {}}})
    ev = hf._parse_flow_line(udp_line)
    assert ev.protocol == "UDP"
    assert ev.dst_port == 53

    icmp_line = json.dumps({"flow": {"time": "t", "verdict": "FORWARDED", "l4": {"ICMPv4": {}},
                                      "source": {}, "destination": {}}})
    ev = hf._parse_flow_line(icmp_line)
    assert ev.protocol == "ICMP"


# --------------------------------------------------------------------------- fetch_flows
def test_fetch_flows_parses_multiple_lines(monkeypatch):
    lines = "\n".join([_flow_line(dst_pod="cart-def"), _flow_line(dst_pod="cart-def"), _flow_line(verdict="DROPPED")])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(lines))
    events = hf.fetch_flows(last=10)
    assert len(events) == 3
    assert events[2].verdict == "DROPPED"


def test_fetch_flows_binary_missing_raises_hubble_unavailable(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("no such file")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(hf.HubbleUnavailableError, match="찾을 수 없음"):
        hf.fetch_flows()


def test_fetch_flows_timeout_raises_hubble_unavailable(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="hubble", timeout=8)
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(hf.HubbleUnavailableError, match="타임아웃"):
        hf.fetch_flows()


def test_fetch_flows_nonzero_exit_with_no_output_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed("", returncode=1, stderr="connection refused"))
    with pytest.raises(hf.HubbleUnavailableError, match="connection refused"):
        hf.fetch_flows()


def test_fetch_flows_nonzero_exit_but_partial_output_is_tolerated(monkeypatch):
    """일부 노드에서 에러가 나도(returncode!=0) 파싱 가능한 라인이 있으면 그것만이라도 반환."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(_flow_line(), returncode=1, stderr="partial"))
    events = hf.fetch_flows()
    assert len(events) == 1


# --------------------------------------------------------------------------- summarize
def test_summarize_counts_verdicts_and_pairs():
    lines = [
        hf._parse_flow_line(_flow_line(dst_pod="cart-def", verdict="FORWARDED")),
        hf._parse_flow_line(_flow_line(dst_pod="cart-def", verdict="FORWARDED")),
        hf._parse_flow_line(_flow_line(dst_pod="other-pod", verdict="DROPPED")),
    ]
    summary = hf.summarize(lines)
    assert summary.total == 3
    assert summary.verdicts == {"FORWARDED": 2, "DROPPED": 1}
    assert summary.top_pairs[0]["dst"] == "shop/cart-def"
    assert summary.top_pairs[0]["count"] == 2


def test_summarize_top_n_limits_output():
    events = []
    for i in range(20):
        events.append(hf._parse_flow_line(_flow_line(dst_pod=f"pod-{i}")))
    summary = hf.summarize(events, top_n=5)
    assert len(summary.top_pairs) == 5


def test_summarize_empty_list():
    summary = hf.summarize([])
    assert summary.total == 0
    assert summary.top_pairs == []
    assert summary.verdicts == {}


# --------------------------------------------------------------------------- fetch_summary (통합 진입점)
def test_fetch_summary_returns_fetch_error_not_exception(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    summary = hf.fetch_summary()
    assert summary.fetch_error is not None
    assert summary.total == 0


def test_fetch_summary_happy_path(monkeypatch):
    lines = "\n".join([_flow_line(), _flow_line()])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(lines))
    summary = hf.fetch_summary()
    assert summary.fetch_error is None
    assert summary.total == 2
