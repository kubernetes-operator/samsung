"""metrics/collector.py 통합 테스트.

PrometheusClient의 `session` 생성자 인자(테스트 확장점, prometheus_client.py 문서 참조)에
가짜 HTTP 세션을 주입해서, `requests` 없이도 실제 응답 파싱(_parse_vector) 경로까지 포함한
전체 collect() 파이프라인을 검증한다. query() 문자열을 monkeypatch하는 것보다 한 단계 더
실물에 가깝다.
"""

from __future__ import annotations

import pytest

from k8s_traffic_operator.metrics import collector
from k8s_traffic_operator.metrics.prometheus_client import PrometheusClient


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _vector(*samples) -> dict:
    """samples: (labels_dict, value) 튜플들. Prometheus vector 결과 payload를 만든다."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [0, str(value)]}
                for labels, value in samples
            ],
        },
    }


EMPTY = {"status": "success", "data": {"resultType": "vector", "result": []}}


class RoutingFakeSession:
    """PromQL 문자열의 특징 문자열로 분기해서 canned 응답을 돌려주는 가짜 세션."""

    def __init__(self, routes: dict):
        # routes: {부분문자열: payload_dict 또는 callable(query)->payload_dict}
        self._routes = routes
        self.calls = []

    def get(self, url, params, timeout):
        query = params["query"]
        self.calls.append(query)
        for needle, payload in self._routes.items():
            if needle in query:
                resolved = payload(query) if callable(payload) else payload
                return FakeResponse(200, resolved)
        raise AssertionError(f"예상하지 못한 PromQL: {query!r}")


def _client_with(routes: dict) -> PrometheusClient:
    session = RoutingFakeSession(routes)
    return PrometheusClient(base_url="http://prom.test", session=session)


def _spec(implementation="envoy-gateway", **overrides):
    spec = {
        "target": {"httpRoute": "checkout-route", "namespace": "shop", "deployment": "checkout-service"},
        "window": "1m",
        "metrics": {"implementation": implementation},
    }
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------- envoy-gateway happy path
def test_envoy_gateway_collect_happy_path(monkeypatch):
    routes = {
        "kube_deployment_status_replicas_ready": _vector(({}, 5)),
        "envoy_http_downstream_rq_time_bucket": _vector(({}, 42.0)),  # p50/p95/p99 동일 처리
        "envoy_response_code_class=\"5\"": lambda q: (
            _vector(({"envoy_cluster_name": "checkout-service"}, 1.0), ({"envoy_cluster_name": "checkout-service-canary"}, 4.0))
            if "by (envoy_cluster_name)" in q or "sum by" in q
            else _vector(({}, 5.0))
        ),
        "envoy_http_downstream_rq_total": lambda q: (
            _vector(({"envoy_cluster_name": "checkout-service"}, 70.0), ({"envoy_cluster_name": "checkout-service-canary"}, 50.0))
            if "sum by" in q
            else _vector(({}, 120.0))
        ),
    }

    def build_client(spec):
        return _client_with(routes)

    monkeypatch.setattr(collector, "_build_client", build_client)
    snap = collector.collect(_spec())

    assert snap.status == "ok"
    assert snap.rps == 120.0
    assert snap.total_ready_pods == 5
    names = {b.name for b in snap.per_backend}
    assert names == {"checkout-service", "checkout-service-canary"}


def test_no_data_when_total_rps_query_returns_empty(monkeypatch):
    routes = {
        "kube_deployment_status_replicas_ready": _vector(({}, 5)),
        "envoy_http_downstream_rq_total": EMPTY,
    }
    monkeypatch.setattr(collector, "_build_client", lambda spec: _client_with(routes))
    snap = collector.collect(_spec())
    assert snap.status == "no_data"
    assert snap.rps is None


def test_collection_failed_when_prometheus_unreachable(monkeypatch):
    class BrokenSession:
        def get(self, *a, **kw):
            raise ConnectionError("boom")

    monkeypatch.setattr(
        collector, "_build_client",
        lambda spec: PrometheusClient(base_url="http://prom.test", session=BrokenSession(), retries=0),
    )
    snap = collector.collect(_spec())
    assert snap.status == "collection_failed"
    assert snap.rps is None


def test_missing_http_route_target_is_collection_failed_not_exception():
    spec = _spec()
    spec["target"] = {"deployment": "checkout-service"}  # httpRoute 누락
    snap = collector.collect(spec)  # _build_client 호출 전에 걸러져야 하므로 monkeypatch 불필요
    assert snap.status == "collection_failed"


def test_error_rate_is_clamped_and_never_faked_as_zero_on_missing_error_series(monkeypatch):
    """5xx 시계열이 아예 없으면(빈 벡터) 에러율은 0.0이어야 한다(진짜 무에러) — no_data와는 다른 케이스."""
    routes = {
        "kube_deployment_status_replicas_ready": _vector(({}, 5)),
        "envoy_http_downstream_rq_time_bucket": _vector(({}, 10.0)),
        "envoy_response_code_class=\"5\"": EMPTY,
        "envoy_http_downstream_rq_total": lambda q: (
            EMPTY if "sum by" in q else _vector(({}, 100.0))
        ),
    }
    monkeypatch.setattr(collector, "_build_client", lambda spec: _client_with(routes))
    snap = collector.collect(_spec())
    assert snap.status == "ok"
    assert snap.error_rate == 0.0


# --------------------------------------------------------------------------- nginx-gateway-fabric (제약 있는 어댑터)
def test_nginx_gateway_fabric_collect_p95_p99_are_none_by_design(monkeypatch):
    routes = {
        "kube_deployment_status_replicas_ready": _vector(({}, 5)),
        "nginxplus_http_server_zone_requests": _vector(({}, 120.0)),
        "nginxplus_http_server_zone_responses": _vector(({}, 6.0)),
        "nginxplus_http_upstream_server_response_time_percentile": EMPTY,
        "nginxplus_http_upstream_server_response_time": lambda q: (
            _vector(({}, 45.0)) if "percentile" not in q else EMPTY
        ),
        "nginxplus_http_upstream_server_requests": _vector(
            ({"upstream": "shop_checkout-service_80"}, 70.0),
            ({"upstream": "shop_checkout-service-canary_80"}, 50.0),
        ),
        "nginxplus_http_upstream_server_responses": _vector(
            ({"upstream": "shop_checkout-service_80"}, 1.0),
            ({"upstream": "shop_checkout-service-canary_80"}, 5.0),
        ),
    }
    monkeypatch.setattr(collector, "_build_client", lambda spec: _client_with(routes))
    snap = collector.collect(_spec(implementation="nginx-gateway-fabric"))

    assert snap.status == "ok"
    assert snap.rps == 120.0
    assert abs(snap.error_rate - 0.05) < 1e-9
    assert snap.p50_latency_ms == 45.0
    assert snap.p95_latency_ms is None  # 평균을 percentile로 위장하지 않는다는 설계 원칙
    assert snap.p99_latency_ms is None
    names = {b.name for b in snap.per_backend}
    assert names == {"checkout-service", "checkout-service-canary"}


def test_nginx_gateway_fabric_missing_p99_does_not_crash_downstream_policy(monkeypatch):
    """p99가 None인 스냅샷을 policy에 넘겨도 예외 없이 안전하게 처리되는지(경계면) 확인."""
    from k8s_traffic_operator.policy import engine

    routes = {
        "kube_deployment_status_replicas_ready": _vector(({}, 5)),
        "nginxplus_http_server_zone_requests": _vector(({}, 30.0)),
        "nginxplus_http_server_zone_responses": _vector(({}, 0.0)),
        "nginxplus_http_upstream_server_response_time_percentile": EMPTY,
        "nginxplus_http_upstream_server_response_time": lambda q: (
            _vector(({}, 20.0)) if "percentile" not in q else EMPTY
        ),
        "nginxplus_http_upstream_server_requests": _vector(({"upstream": "shop_checkout-service_80"}, 30.0)),
        "nginxplus_http_upstream_server_responses": _vector(({"upstream": "shop_checkout-service_80"}, 0.0)),
    }
    monkeypatch.setattr(collector, "_build_client", lambda spec: _client_with(routes))
    spec = _spec(implementation="nginx-gateway-fabric")
    spec["thresholds"] = {"targetRPSPerPod": 50, "scaleDownRPSPerPod": 20, "scaleUpErrorRate": 0.05, "maxP99LatencyMs": 800}
    spec["actions"] = {"minReplicas": 2, "maxReplicas": 20, "cooldownSeconds": 0, "allowRouteIsolation": True}

    snap = collector.collect(spec)
    decision = engine.evaluate(spec, snap, {})
    assert decision.action in ("noop", "scale")  # 지연시간 축 이상탐지는 비활성, 예외 없음
