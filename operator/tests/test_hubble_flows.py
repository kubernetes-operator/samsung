"""dashboard/hubble_flows.py 단위 테스트 - 대시보드 전용 집계(summarize/fetch_summary)만 다룬다.

저수준 조회/파싱(fetch_flows, _parse_flow_line 등)은 hubble_client.py로 옮겨졌고
tests/test_hubble_client.py에서 검증한다.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

from k8s_traffic_operator.dashboard import hubble_flows as hf
from k8s_traffic_operator.hubble_client import _parse_flow_line


def _flow_line(*, verdict="FORWARDED", dst_pod="cart-def", dst_ns="shop") -> str:
    return json.dumps({
        "flow": {
            "time": "2026-07-19T12:00:00.000000000Z",
            "verdict": verdict,
            "l4": {"TCP": {"destination_port": 8080}},
            "source": {"namespace": "shop", "pod_name": "checkout-abc"},
            "destination": {"namespace": dst_ns, "pod_name": dst_pod},
        }
    })


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = stdout, stderr, returncode
    return proc


# --------------------------------------------------------------------------- summarize
def test_summarize_counts_verdicts_and_pairs():
    events = [
        _parse_flow_line(_flow_line(dst_pod="cart-def", verdict="FORWARDED")),
        _parse_flow_line(_flow_line(dst_pod="cart-def", verdict="FORWARDED")),
        _parse_flow_line(_flow_line(dst_pod="other-pod", verdict="DROPPED")),
    ]
    summary = hf.summarize(events)
    assert summary.total == 3
    assert summary.verdicts == {"FORWARDED": 2, "DROPPED": 1}
    assert summary.top_pairs[0]["dst"] == "shop/cart-def"
    assert summary.top_pairs[0]["count"] == 2


def test_summarize_top_n_limits_output():
    events = [_parse_flow_line(_flow_line(dst_pod=f"pod-{i}")) for i in range(20)]
    summary = hf.summarize(events, top_n=5)
    assert len(summary.top_pairs) == 5


def test_summarize_empty_list():
    summary = hf.summarize([])
    assert summary.total == 0
    assert summary.top_pairs == []
    assert summary.verdicts == {}


# --------------------------------------------------------------------------- fetch_summary
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
