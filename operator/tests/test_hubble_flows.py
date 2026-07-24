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


def test_summarize_sets_dominant_verdict_per_pair():
    """각 연결 쌍의 '대표 verdict'는 그 쌍에서 가장 많이 관측된 판정이어야 한다(다이어그램 색용)."""
    events = [
        _ev(dst_pod="cart-def", verdict="FORWARDED"),
        _ev(dst_pod="cart-def", verdict="FORWARDED"),
        _ev(dst_pod="cart-def", verdict="DROPPED"),   # cart는 2:1로 FORWARDED 우세
        _ev(dst_pod="pay-xyz", verdict="DROPPED"),
        _ev(dst_pod="pay-xyz", verdict="DROPPED"),    # pay는 DROPPED 우세
    ]
    summary = hf.summarize(events)
    by_dst = {p["dst"]: p["verdict"] for p in summary.top_pairs}
    assert by_dst["shop/cart-def"] == "FORWARDED"
    assert by_dst["shop/pay-xyz"] == "DROPPED"


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


# --------------------------------------------------------------------------- namespace 필터
def test_summarize_lists_namespaces_present_in_scope():
    """scope 적용 후 등장하는 네임스페이스 목록이 흐름 수 내림차순으로 채워져야 한다(필터 UI용)."""
    events = [
        _ev(src_ns="shop", dst_ns="shop"),      # {shop} → shop +1
        _ev(src_ns="shop", dst_ns="pay"),       # {shop,pay} → shop +1, pay +1
        _ev(src_ns="pay", dst_ns="pay"),        # {pay} → pay +1
    ]                                           # 결과: shop 2, pay 2 (동수)
    summary = hf.summarize(events)
    names = [n["name"] for n in summary.namespaces]
    counts = {n["name"]: n["count"] for n in summary.namespaces}
    assert names == ["pay", "shop"]  # 흐름 수 내림차순, 동수면 이름 오름차순
    assert counts == {"shop": 2, "pay": 2}


def test_summarize_namespace_filter_keeps_flows_touching_that_namespace():
    events = [
        _ev(src_ns="shop", dst_ns="shop"),
        _ev(src_ns="shop", dst_ns="pay"),   # shop이 한쪽 → shop 필터에 포함
        _ev(src_ns="pay", dst_ns="pay"),    # shop 없음 → 제외
    ]
    summary = hf.summarize(events, namespace="shop")
    assert summary.namespace == "shop"
    assert summary.shown == 2
    assert all("pay/" not in p["src"] or "shop" in (p["src"] + p["dst"]) for p in summary.top_pairs)
    # namespaces 목록은 필터와 무관하게 scope 기준 전체를 계속 제공한다.
    assert {n["name"] for n in summary.namespaces} == {"shop", "pay"}


# --------------------------------------------------------------------------- focus(연결된 리소스) 필터
def test_summarize_focus_keeps_only_flows_touching_that_resource():
    events = [
        _ev(src_pod="checkout-abc", src_ns="shop", dst_pod="cart-def", dst_ns="shop"),
        _ev(src_pod="checkout-abc", src_ns="shop", dst_pod="pay-xyz", dst_ns="shop"),
        _ev(src_pod="web-1", src_ns="shop", dst_pod="cart-def", dst_ns="shop"),  # checkout 무관
    ]
    summary = hf.summarize(events, focus="shop/checkout-abc")
    assert summary.focus == "shop/checkout-abc"
    assert summary.shown == 2
    for p in summary.top_pairs:
        assert "shop/checkout-abc" in (p["src"], p["dst"])


def test_summarize_namespace_and_focus_combine():
    events = [
        _ev(src_pod="checkout-abc", src_ns="shop", dst_pod="cart-def", dst_ns="shop"),
        _ev(src_pod="checkout-abc", src_ns="shop", dst_pod="db-1", dst_ns="data"),  # ns=shop∪data
        _ev(src_pod="worker", src_ns="data", dst_pod="db-1", dst_ns="data"),        # checkout 무관
    ]
    # data 네임스페이스에 걸치면서 checkout이 낀 흐름만
    summary = hf.summarize(events, namespace="data", focus="shop/checkout-abc")
    assert summary.shown == 1
    assert summary.top_pairs[0]["dst"] == "data/db-1"


def test_fetch_summary_passes_namespace_and_focus_through(monkeypatch):
    lines = "\n".join([
        _flow_line(src_pod="checkout-abc", src_ns="shop", dst_pod="cart-def", dst_ns="shop"),
        _flow_line(src_pod="web-1", src_ns="shop", dst_pod="cart-def", dst_ns="shop"),
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(lines))
    summary = hf.fetch_summary(namespace="shop", focus="shop/checkout-abc")
    assert summary.namespace == "shop"
    assert summary.focus == "shop/checkout-abc"
    assert summary.shown == 1


def test_fetch_summary_fetch_error_preserves_filters(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    summary = hf.fetch_summary(scope="all", namespace="shop", focus="shop/x")
    assert summary.fetch_error is not None
    assert (summary.scope, summary.namespace, summary.focus) == ("all", "shop", "shop/x")
