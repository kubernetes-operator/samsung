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


def _flow_line(*, verdict="FORWARDED", dst_pod="cart-def", dst_ns="shop",
               src_pod="checkout-abc", src_ns="shop", src_reserved=None, dst_reserved=None) -> str:
    src = {"labels": [f"reserved:{src_reserved}"]} if src_reserved else {"namespace": src_ns, "pod_name": src_pod}
    dst = {"labels": [f"reserved:{dst_reserved}"]} if dst_reserved else {"namespace": dst_ns, "pod_name": dst_pod}
    return json.dumps({
        "flow": {
            "time": "2026-07-19T12:00:00.000000000Z",
            "verdict": verdict,
            "l4": {"TCP": {"destination_port": 8080}},
            "source": src,
            "destination": dst,
        }
    })


def _ev(**kw):
    return _parse_flow_line(_flow_line(**kw))


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = stdout, stderr, returncode
    return proc


# --------------------------------------------------------------------------- is_application_flow
def test_app_flow_when_both_endpoints_are_app_namespace():
    assert hf.is_application_flow(_ev(src_ns="shop", dst_ns="shop")) is True


def test_infra_flow_when_both_endpoints_are_system():
    # kube-system -> kube-system (둘 다 시스템 ns) => 인프라
    assert hf.is_application_flow(_ev(src_ns="kube-system", dst_ns="kube-system")) is False


def test_infra_flow_when_both_endpoints_reserved():
    # remote-node -> health (둘 다 reserved) => 인프라
    assert hf.is_application_flow(_ev(src_reserved="remote-node", dst_reserved="health")) is False


def test_app_flow_when_one_side_is_app_even_if_other_is_infra():
    # my-app -> kube-dns(kube-system): 한쪽이 앱이면 사용자 관심사 => 애플리케이션 흐름
    assert hf.is_application_flow(_ev(src_ns="shop", dst_ns="kube-system")) is True
    assert hf.is_application_flow(_ev(src_reserved="world", dst_ns="shop")) is True


# --------------------------------------------------------------------------- summarize scope
def test_summarize_default_scope_app_excludes_infra():
    events = [
        _ev(src_ns="shop", dst_ns="shop"),                       # app
        _ev(src_ns="shop", dst_ns="shop"),                       # app
        _ev(src_ns="kube-system", dst_ns="kube-system"),         # infra
        _ev(src_reserved="remote-node", dst_reserved="health"),  # infra
    ]
    summary = hf.summarize(events)  # 기본 scope="app"
    assert summary.total == 4         # 원본 전체
    assert summary.app_flows == 2     # 앱으로 분류된 수
    assert summary.shown == 2         # 집계에 실제로 쓰인 수(app만)
    assert summary.scope == "app"
    # top_pairs에 인프라 쌍이 없어야 한다.
    dsts = {p["dst"] for p in summary.top_pairs}
    assert "shop/cart-def" in dsts
    assert all("kube-system" not in d and "health" not in d for d in dsts)


def test_summarize_scope_all_includes_infra():
    events = [
        _ev(src_ns="shop", dst_ns="shop"),
        _ev(src_ns="kube-system", dst_ns="kube-system"),
        _ev(src_reserved="remote-node", dst_reserved="health"),
    ]
    summary = hf.summarize(events, scope="all")
    assert summary.total == 3
    assert summary.shown == 3
    assert summary.scope == "all"


def test_summarize_counts_verdicts_and_pairs():
    events = [
        _ev(dst_pod="cart-def", verdict="FORWARDED"),
        _ev(dst_pod="cart-def", verdict="FORWARDED"),
        _ev(dst_pod="other-pod", verdict="DROPPED"),
    ]
    summary = hf.summarize(events)
    assert summary.total == 3
    assert summary.verdicts == {"FORWARDED": 2, "DROPPED": 1}
    assert summary.top_pairs[0]["dst"] == "shop/cart-def"
    assert summary.top_pairs[0]["count"] == 2


def test_summarize_top_n_limits_output():
    events = [_ev(dst_pod=f"pod-{i}") for i in range(20)]
    summary = hf.summarize(events, top_n=5)
    assert len(summary.top_pairs) == 5


def test_summarize_empty_list():
    summary = hf.summarize([])
    assert summary.total == 0
    assert summary.shown == 0
    assert summary.top_pairs == []
    assert summary.verdicts == {}


def test_summarize_app_scope_zero_when_only_infra():
    """전체 흐름은 있는데 앱 흐름이 0인 경우 — shown=0, total>0로 구분 가능해야 한다."""
    events = [_ev(src_ns="kube-system", dst_ns="kube-system") for _ in range(3)]
    summary = hf.summarize(events)  # scope="app"
    assert summary.total == 3
    assert summary.app_flows == 0
    assert summary.shown == 0
    assert summary.top_pairs == []


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


def test_fetch_summary_passes_scope_through(monkeypatch):
    lines = "\n".join([
        _flow_line(src_ns="shop", dst_ns="shop"),
        _flow_line(src_ns="kube-system", dst_ns="kube-system"),
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(lines))
    app = hf.fetch_summary(scope="app")
    allf = hf.fetch_summary(scope="all")
    assert app.shown == 1
    assert allf.shown == 2
