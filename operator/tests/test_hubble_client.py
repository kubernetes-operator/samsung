"""hubble_client.py 단위 테스트 - subprocess는 MagicMock으로 대체(실제 hubble CLI/relay 불필요).

dashboard(hubble_flows.py)와 metrics(hubble_collector.py) 양쪽이 공유하는 저수준 모듈이므로,
여기서 검증된 파싱 정확성은 두 소비자 모두에게 적용된다.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from k8s_traffic_operator import hubble_client as hc


def _flow_line(
    *,
    verdict="FORWARDED",
    direction="EGRESS",
    src_ns="shop", src_pod="checkout-abc",
    dst_ns="shop", dst_pod="cart-def",
    dst_workload=None,
    dst_port=8080,
    proto="TCP",
    time_="2026-07-19T12:00:00.000000000Z",
) -> str:
    dst = {"namespace": dst_ns, "pod_name": dst_pod} if dst_ns else {"labels": ["reserved:world"]}
    if dst_workload:
        dst["workloads"] = [{"name": dst_workload, "kind": "Deployment"}]
    flow = {
        "flow": {
            "time": time_,
            "verdict": verdict,
            "traffic_direction": direction,
            "l4": {proto: {"destination_port": dst_port}},
            "source": {"namespace": src_ns, "pod_name": src_pod} if src_ns else {"labels": ["reserved:host"]},
            "destination": dst,
        }
    }
    return json.dumps(flow)


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# --------------------------------------------------------------------------- _parse_flow_time
def test_parse_flow_time_handles_nanosecond_precision():
    epoch = hc._parse_flow_time("2026-07-19T13:58:07.035205452Z")
    assert epoch is not None
    assert abs(epoch - 1784469487.035205) < 1e-3


def test_parse_flow_time_handles_no_fraction():
    epoch = hc._parse_flow_time("2026-07-19T13:58:07Z")
    assert epoch == 1784469487.0


def test_parse_flow_time_none_on_garbage():
    assert hc._parse_flow_time("") is None
    assert hc._parse_flow_time("not-a-timestamp") is None


# --------------------------------------------------------------------------- _entity_label
def test_entity_label_prefers_namespace_pod():
    ep = hc._entity_label({"namespace": "shop", "pod_name": "checkout-abc"})
    assert ep.label == "shop/checkout-abc"
    assert ep.namespace == "shop"


def test_entity_label_captures_workload_name():
    ep = hc._entity_label({"namespace": "shop", "pod_name": "checkout-abc-7f9d8",
                            "workloads": [{"name": "checkout", "kind": "Deployment"}]})
    assert ep.workload == "checkout"


def test_entity_label_falls_back_to_reserved_labels():
    ep = hc._entity_label({"labels": ["reserved:host", "reserved:kube-apiserver"]})
    assert ep.label == "host+kube-apiserver"


def test_entity_label_unknown_when_nothing_present():
    ep = hc._entity_label({})
    assert ep.label == "unknown"


# --------------------------------------------------------------------------- _parse_flow_line
def test_parse_flow_line_extracts_core_fields():
    ev = hc._parse_flow_line(_flow_line())
    assert ev.verdict == "FORWARDED"
    assert ev.direction == "EGRESS"
    assert ev.protocol == "TCP"
    assert ev.src.label == "shop/checkout-abc"
    assert ev.dst.label == "shop/cart-def"
    assert ev.dst_port == 8080
    assert ev.epoch is not None


def test_parse_flow_line_ignores_non_flow_lines():
    assert hc._parse_flow_line('{"lostEvents": {"numEventsLost": 3}}') is None
    assert hc._parse_flow_line("") is None
    assert hc._parse_flow_line("not json at all") is None


def test_parse_flow_line_handles_udp_and_icmp():
    udp_line = json.dumps({"flow": {"time": "t", "verdict": "FORWARDED", "l4": {"UDP": {"destination_port": 53}},
                                     "source": {}, "destination": {}}})
    ev = hc._parse_flow_line(udp_line)
    assert ev.protocol == "UDP"
    assert ev.dst_port == 53

    icmp_line = json.dumps({"flow": {"time": "t", "verdict": "FORWARDED", "l4": {"ICMPv4": {}},
                                      "source": {}, "destination": {}}})
    ev = hc._parse_flow_line(icmp_line)
    assert ev.protocol == "ICMP"


# --------------------------------------------------------------------------- fetch_flows
def test_fetch_flows_parses_multiple_lines(monkeypatch):
    lines = "\n".join([_flow_line(dst_pod="cart-def"), _flow_line(dst_pod="cart-def"), _flow_line(verdict="DROPPED")])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(lines))
    events = hc.fetch_flows(last=10)
    assert len(events) == 3
    assert events[2].verdict == "DROPPED"


def test_fetch_flows_forwards_extra_args_to_cli(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _fake_completed(_flow_line())

    monkeypatch.setattr(subprocess, "run", fake_run)
    hc.fetch_flows(last=10, extra_args=["--to-namespace", "shop", "--to-workload", "checkout"])
    assert "--to-namespace" in captured["cmd"]
    assert "shop" in captured["cmd"]
    assert "--to-workload" in captured["cmd"]
    assert "checkout" in captured["cmd"]


def test_fetch_flows_binary_missing_raises_hubble_unavailable(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("no such file")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(hc.HubbleUnavailableError, match="찾을 수 없음"):
        hc.fetch_flows(last=10)


def test_fetch_flows_timeout_raises_hubble_unavailable(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="hubble", timeout=8)
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(hc.HubbleUnavailableError, match="타임아웃"):
        hc.fetch_flows(last=10)


def test_fetch_flows_nonzero_exit_with_no_output_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed("", returncode=1, stderr="connection refused"))
    with pytest.raises(hc.HubbleUnavailableError, match="connection refused"):
        hc.fetch_flows(last=10)


def test_fetch_flows_nonzero_exit_but_partial_output_is_tolerated(monkeypatch):
    """일부 노드에서 에러가 나도(returncode!=0) 파싱 가능한 라인이 있으면 그것만이라도 반환."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(_flow_line(), returncode=1, stderr="partial"))
    events = hc.fetch_flows(last=10)
    assert len(events) == 1
