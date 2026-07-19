"""Gateway API 구현체 어댑터 단위 테스트.

각 어댑터가 base.GatewayAdapter 계약(aggregate_queries/backend_queries의 키 집합)을
지키는지, 그리고 map_backend_name 휴리스틱이 문서에 적힌 대로 동작하는지 확인한다.
"""

from __future__ import annotations

import pytest

from k8s_traffic_operator.metrics.adapters import (
    DEFAULT_IMPL,
    EnvoyGatewayAdapter,
    IstioAdapter,
    NginxGatewayFabricAdapter,
    get_adapter,
    supported_implementations,
)

ALL_ADAPTERS = [EnvoyGatewayAdapter(), IstioAdapter(), NginxGatewayFabricAdapter()]


@pytest.mark.parametrize("adapter", ALL_ADAPTERS, ids=lambda a: a.IMPL_NAME)
def test_aggregate_queries_contract_keys(adapter):
    q = adapter.aggregate_queries("checkout-route", "shop", "1m")
    assert set(q.keys()) == {"total_rps", "error_rps", "p50", "p95", "p99"}
    assert all(isinstance(v, str) and v for v in q.values())


@pytest.mark.parametrize("adapter", ALL_ADAPTERS, ids=lambda a: a.IMPL_NAME)
def test_backend_queries_contract_keys(adapter):
    q = adapter.backend_queries("checkout-route", "shop", "1m")
    assert set(q.keys()) == {"rps", "error_rps", "p99"}
    assert all(isinstance(v, str) and v for v in q.values())


@pytest.mark.parametrize("adapter", ALL_ADAPTERS, ids=lambda a: a.IMPL_NAME)
def test_window_is_reflected_verbatim_in_queries_not_hardcoded(adapter):
    q5m = adapter.aggregate_queries("checkout-route", "shop", "5m")
    assert "[5m]" in q5m["total_rps"]
    q30s = adapter.aggregate_queries("checkout-route", "shop", "30s")
    assert "[30s]" in q30s["total_rps"]


def test_registry_lists_all_three_implementations():
    impls = supported_implementations()
    assert impls == sorted(["envoy-gateway", "istio", "nginx-gateway-fabric"])


def test_get_adapter_by_exact_name():
    assert isinstance(get_adapter("envoy-gateway"), EnvoyGatewayAdapter)
    assert isinstance(get_adapter("istio"), IstioAdapter)
    assert isinstance(get_adapter("nginx-gateway-fabric"), NginxGatewayFabricAdapter)


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("envoy", EnvoyGatewayAdapter),
        ("EnvoyGateway", EnvoyGatewayAdapter),
        ("istio-gateway", IstioAdapter),
        ("nginx", NginxGatewayFabricAdapter),
        ("NGF", NginxGatewayFabricAdapter),
    ],
)
def test_get_adapter_aliases(alias, expected):
    assert isinstance(get_adapter(alias), expected)


def test_unknown_implementation_falls_back_to_default_without_raising():
    adapter = get_adapter("some-unknown-mesh-vendor")
    assert adapter.IMPL_NAME == DEFAULT_IMPL


def test_unknown_implementation_none_or_empty_falls_back_to_default():
    assert get_adapter(None).IMPL_NAME == DEFAULT_IMPL
    assert get_adapter("").IMPL_NAME == DEFAULT_IMPL


# --------------------------------------------------------------------------- map_backend_name
def test_envoy_map_backend_name_ns_svc_form_takes_last_segment():
    a = EnvoyGatewayAdapter()
    assert a.map_backend_name("shop/checkout-service") == "checkout-service"


def test_envoy_map_backend_name_path_form_kept_as_is_for_diagnostics():
    a = EnvoyGatewayAdapter()
    val = "httproute/shop/checkout-route/rule/0/backend/0"
    assert a.map_backend_name(val) == val


def test_istio_map_backend_name_is_identity():
    a = IstioAdapter()
    assert a.map_backend_name("checkout-service") == "checkout-service"


def test_nginx_map_backend_name_three_part_takes_middle_segment():
    a = NginxGatewayFabricAdapter()
    assert a.map_backend_name("shop_checkout-service_80") == "checkout-service"


def test_nginx_map_backend_name_unexpected_shape_kept_as_is():
    a = NginxGatewayFabricAdapter()
    assert a.map_backend_name("weird-name") == "weird-name"


def test_base_default_map_backend_name_is_identity():
    from k8s_traffic_operator.metrics.adapters.base import GatewayAdapter

    class _Dummy(GatewayAdapter):
        IMPL_NAME = "dummy"

        def aggregate_queries(self, route, namespace, window):
            return {}

        def backend_queries(self, route, namespace, window):
            return {}

    assert _Dummy().map_backend_name("anything") == "anything"
