"""Istio 어댑터 (Gateway API 구현체 #2).

Envoy Gateway 어댑터와 동일한 키 집합을 만들어 낸다 — 다른 것은 메트릭/레이블 이름뿐이다.
이 어댑터의 존재 자체가 "collector 정규화 로직이 구현체 중립"임을 증명한다: collector 는
아래 쿼리가 istio_* 인지 envoy_* 인지 전혀 모른다.

    개념        | 메트릭 / 필터
    ------------|--------------------------------------------------------------
    요청 수     | istio_requests_total
    5xx         | istio_requests_total{response_code=~"5.."}
    지연시간    | istio_request_duration_milliseconds_bucket (히스토그램, 단위 ms)
    backend     | destination_workload
    라우트 필터 | 가정: HTTPRoute -> VirtualService 이름이 동일하고 있으면 그 레이블로 필터.
                  Istio 표준 요청 메트릭에는 route 이름 레이블이 항상 있지는 않으므로,
                  destination_service 로 대략 필터한다(아래 route_selector 참조).
"""

from __future__ import annotations

from typing import Dict, Optional

from .base import QUANTILES, GatewayAdapter


class IstioAdapter(GatewayAdapter):
    IMPL_NAME = "istio"
    backend_label = "destination_workload"
    latency_unit = "ms"  # istio_request_duration_milliseconds_* 는 ms 단위

    def route_selector(self, route: str, namespace: Optional[str]) -> str:
        # 가정: HTTPRoute 이름과 대상 서비스 이름이 매칭된다고 보고 destination_service_name 으로
        # 좁힌다. namespace 가 있으면 destination_service_namespace 로 추가 제한.
        parts = [f'destination_service_name=~"{route}.*"']
        if namespace:
            parts.append(f'destination_service_namespace="{namespace}"')
        # reporter="destination" 로 서버측 관측만 사용(중복 집계 방지, Istio 관례).
        parts.append('reporter="destination"')
        return ",".join(parts)

    def aggregate_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        sel = self.route_selector(route, namespace)
        rq = "istio_requests_total"
        bucket = "istio_request_duration_milliseconds_bucket"

        queries: Dict[str, str] = {
            "total_rps": f"sum(rate({rq}{{{sel}}}[{window}]))",
            "error_rps": f'sum(rate({rq}{{{sel},response_code=~"5.."}}[{window}]))',
        }
        for key, q in QUANTILES.items():
            queries[key] = (
                f"histogram_quantile({q}, "
                f"sum by (le) (rate({bucket}{{{sel}}}[{window}])))"
            )
        return queries

    def backend_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        sel = self.route_selector(route, namespace)
        lbl = self.backend_label
        rq = "istio_requests_total"
        bucket = "istio_request_duration_milliseconds_bucket"

        return {
            "rps": f"sum by ({lbl}) (rate({rq}{{{sel}}}[{window}]))",
            "error_rps": (
                f'sum by ({lbl}) (rate({rq}{{{sel},response_code=~"5.."}}[{window}]))'
            ),
            "p99": (
                f"histogram_quantile(0.99, "
                f"sum by ({lbl}, le) (rate({bucket}{{{sel}}}[{window}])))"
            ),
        }

    # destination_workload 는 보통 Deployment/backend 이름과 일치하므로 항등 매핑으로 충분.
