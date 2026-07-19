"""metrics/hubble_collector.py 단위 테스트.

hubble_client.fetch_flows는 monkeypatch로 대체(실제 hubble CLI/relay 불필요).
kubernetes client(ready_pods 조회)도 MagicMock으로 대체.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from k8s_traffic_operator.hubble_client import FlowEndpoint, FlowEvent, HubbleUnavailableError
from k8s_traffic_operator.metrics import hubble_collector as hcoll

NOW = 1_000_000.0


def _spec(namespace="shop", deployment="checkout-service", window_seconds_hint=60):
    return {"target": {"httpRoute": "checkout-route", "namespace": namespace, "deployment": deployment}}


def _event(*, verdict="FORWARDED", epoch=NOW - 10, dst_workload="checkout-service", dst_pod_name="checkout-service-abc123"):
    return FlowEvent(
        time="t", epoch=epoch, verdict=verdict, direction="INGRESS", protocol="TCP", dst_port=8080,
        src=FlowEndpoint(label="shop/client-xyz", namespace="shop", pod_name="client-xyz"),
        dst=FlowEndpoint(label=f"shop/{dst_pod_name}", namespace="shop", pod_name=dst_pod_name, workload=dst_workload),
    )


# --------------------------------------------------------------------------- 대상 특정
def test_missing_deployment_is_collection_failed():
    snap = hcoll.collect({"target": {"namespace": "shop"}}, window_seconds=60, now=NOW)
    assert snap.status == "collection_failed"
    assert "deployment" in snap.meta["error"]


def test_fetch_forwards_to_namespace_filter(monkeypatch):
    captured = {}

    def fake_fetch(last, extra_args=None):
        captured["extra_args"] = extra_args
        return []

    monkeypatch.setattr(hcoll.hubble_client, "fetch_flows", fake_fetch)
    hcoll.collect(_spec(namespace="shop"), window_seconds=60, now=NOW)
    assert captured["extra_args"] == ["--to-namespace", "shop"]


# --------------------------------------------------------------------------- 상태 판정
def test_hubble_unavailable_is_collection_failed(monkeypatch):
    def boom(last, extra_args=None):
        raise HubbleUnavailableError("relay 연결 실패")
    monkeypatch.setattr(hcoll.hubble_client, "fetch_flows", boom)
    snap = hcoll.collect(_spec(), window_seconds=60, now=NOW)
    assert snap.status == "collection_failed"
    assert "relay 연결 실패" in snap.meta["error"]


def test_no_matching_flows_is_no_data(monkeypatch):
    # 다른 deployment로 가는 flow만 있음 -> 매칭 안 됨 -> no_data
    monkeypatch.setattr(hcoll.hubble_client, "fetch_flows", lambda last, extra_args=None: [
        _event(dst_workload="other-service", dst_pod_name="other-service-xyz"),
    ])
    snap = hcoll.collect(_spec(deployment="checkout-service"), window_seconds=60, now=NOW)
    assert snap.status == "no_data"
    assert snap.rps is None


def test_flows_outside_window_are_excluded(monkeypatch):
    old_event = _event(epoch=NOW - 3600)  # 윈도우(60s) 밖
    monkeypatch.setattr(hcoll.hubble_client, "fetch_flows", lambda last, extra_args=None: [old_event])
    snap = hcoll.collect(_spec(), window_seconds=60, now=NOW)
    assert snap.status == "no_data"


def test_events_with_unparseable_time_are_excluded(monkeypatch):
    """시각 파싱에 실패한(epoch=None) 이벤트는 윈도우 판단에 넣지 않는다(결측치 정직 원칙)."""
    no_epoch_event = _event()
    no_epoch_event.epoch = None
    monkeypatch.setattr(hcoll.hubble_client, "fetch_flows", lambda last, extra_args=None: [no_epoch_event])
    snap = hcoll.collect(_spec(), window_seconds=60, now=NOW)
    assert snap.status == "no_data"  # epoch 없는 이벤트만 있으므로 매칭된 것이 0건


# --------------------------------------------------------------------------- 정상 집계
def test_ok_status_computes_rps_and_error_rate(monkeypatch):
    events = (
        [_event(verdict="FORWARDED") for _ in range(9)]
        + [_event(verdict="DROPPED")]
    )
    monkeypatch.setattr(hcoll.hubble_client, "fetch_flows", lambda last, extra_args=None: events)
    monkeypatch.setattr(hcoll, "_query_ready_pods", lambda ns, dep: 3)

    snap = hcoll.collect(_spec(), window_seconds=10, now=NOW)

    assert snap.status == "ok"
    assert snap.rps == 1.0  # 10건 / 10초
    assert abs(snap.error_rate - 0.1) < 1e-9
    assert snap.total_ready_pods == 3
    assert snap.per_backend == []          # 의도적으로 빈 값
    assert snap.p50_latency_ms is None     # 의도적으로 None
    assert snap.p99_latency_ms is None


def test_matches_target_prefers_workload_over_pod_prefix():
    assert hcoll._matches_target("checkout-service", "checkout-service-abc", "checkout-service") is True
    assert hcoll._matches_target("other-service", "checkout-service-abc", "checkout-service") is False


def test_matches_target_falls_back_to_pod_name_prefix_when_no_workload():
    assert hcoll._matches_target(None, "checkout-service-7f9d8-abcde", "checkout-service") is True
    assert hcoll._matches_target(None, "checkout-service-canary-abc", "checkout-service") is True  # 접두 매칭 한계(의도된 근사)
    assert hcoll._matches_target(None, "unrelated-pod", "checkout-service") is False


def test_matches_target_false_when_nothing_to_match():
    assert hcoll._matches_target(None, None, "checkout-service") is False


# --------------------------------------------------------------------------- ready_pods
def test_query_ready_pods_none_when_deployment_missing():
    assert hcoll._query_ready_pods("shop", None) is None
    assert hcoll._query_ready_pods(None, "checkout-service") is None


def test_query_ready_pods_reads_kubernetes_api(monkeypatch):
    apps_v1 = MagicMock()
    apps_v1.read_namespaced_deployment.return_value.status.ready_replicas = 4
    monkeypatch.setattr(hcoll, "_load_apps_v1", lambda: apps_v1)
    assert hcoll._query_ready_pods("shop", "checkout-service") == 4


def test_query_ready_pods_handles_api_exception_gracefully(monkeypatch):
    apps_v1 = MagicMock()
    apps_v1.read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")
    monkeypatch.setattr(hcoll, "_load_apps_v1", lambda: apps_v1)
    assert hcoll._query_ready_pods("shop", "checkout-service") is None
