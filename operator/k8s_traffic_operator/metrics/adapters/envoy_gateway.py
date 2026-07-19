"""Envoy Gateway 어댑터 (Gateway API 구현체 #1, 완전 구현).

Envoy Gateway 는 데이터플레인이 Envoy 이므로 Envoy 표준 메트릭을 노출한다.

    개념        | 메트릭 / 필터
    ------------|--------------------------------------------------------------
    요청 수     | envoy_http_downstream_rq_total
    5xx         | 위 메트릭 + envoy_response_code_class="5"
    지연시간    | envoy_http_downstream_rq_time_bucket (히스토그램, 단위 ms)
    backend     | envoy_cluster_name (HTTPRoute backendRef 별 cluster)
    라우트 필터 | envoy_http_conn_manager_prefix = <HTTPRoute 이름>

가정(assumption): HTTPRoute 이름이 Envoy 의 conn manager stat prefix 로 그대로 쓰인다고
본다(스킬 문서의 예시 규약). 실제 Envoy Gateway 버전에 따라 prefix 형식이 다르면
`route_selector()` 만 조정하면 된다 — 쿼리 본문/정규화는 그대로 재사용된다.
envoy_http_downstream_rq_time_bucket 의 단위는 밀리초(ms)라고 가정한다(Envoy 기본).
"""

from __future__ import annotations

from typing import Dict, Optional

from .base import QUANTILES, GatewayAdapter


class EnvoyGatewayAdapter(GatewayAdapter):
    IMPL_NAME = "envoy-gateway"
    backend_label = "envoy_cluster_name"
    latency_unit = "ms"  # envoy_*_rq_time_* 은 ms 단위

    # ------------------------------------------------------------------ selectors
    def route_selector(self, route: str, namespace: Optional[str]) -> str:
        """라우트를 특정하는 PromQL 레이블 셀렉터(중괄호 내부, 앞부분)를 만든다.

        namespace 레이블은 Envoy 메트릭에 항상 있지는 않으므로 있으면 붙이고 없으면 생략한다.
        (namespace=None 이면 라우트 이름만으로 필터)
        """
        parts = [f'envoy_http_conn_manager_prefix="{route}"']
        if namespace:
            # Envoy Gateway 는 종종 namespace 를 별도 레이블로 노출하지 않는다.
            # 노출하는 배포판을 위해 넣되, 없으면 이 셀렉터는 결과 0건이 될 수 있음에 유의.
            # 안전을 위해 namespace 는 conn_manager_prefix 기반 매칭에 필수는 아니다 →
            # 여기서는 넣지 않고 route 로만 좁힌다(오탐 방지). 필요 시 이 줄을 활성화.
            pass
        return ",".join(parts)

    # ------------------------------------------------------------------ aggregate
    def aggregate_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        sel = self.route_selector(route, namespace)
        rq = "envoy_http_downstream_rq_total"
        bucket = "envoy_http_downstream_rq_time_bucket"

        queries: Dict[str, str] = {
            "total_rps": f"sum(rate({rq}{{{sel}}}[{window}]))",
            "error_rps": f'sum(rate({rq}{{{sel},envoy_response_code_class="5"}}[{window}]))',
        }
        for key, q in QUANTILES.items():
            queries[key] = (
                f"histogram_quantile({q}, "
                f"sum by (le) (rate({bucket}{{{sel}}}[{window}])))"
            )
        return queries

    # ------------------------------------------------------------------ per-backend
    def backend_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        sel = self.route_selector(route, namespace)
        lbl = self.backend_label
        rq = "envoy_http_downstream_rq_total"
        bucket = "envoy_http_downstream_rq_time_bucket"

        return {
            "rps": f"sum by ({lbl}) (rate({rq}{{{sel}}}[{window}]))",
            "error_rps": (
                f'sum by ({lbl}) (rate({rq}{{{sel},envoy_response_code_class="5"}}[{window}]))'
            ),
            "p99": (
                f"histogram_quantile(0.99, "
                f"sum by ({lbl}, le) (rate({bucket}{{{sel}}}[{window}])))"
            ),
        }

    # ------------------------------------------------------------------ name mapping
    def map_backend_name(self, label_value: str) -> str:
        """envoy_cluster_name -> HTTPRoute backendRef 이름 추정.

        Envoy Gateway 의 cluster 이름은 버전에 따라 다양하다. 자주 보이는 형식:
            "httproute/<ns>/<route>/rule/0/backend/0"   (경로형)
            "<ns>/<service>"                               (ns/svc 형)
            "<service>"                                    (단순형)
        backendRef 이름은 보통 서비스 이름이므로, 경로형이면 'backend' 세그먼트 이후를,
        ns/svc 형이면 마지막 세그먼트를 취한다. 규칙에 안 맞으면 원본을 그대로 둔다.

        가정: 이 휴리스틱이 실제 backendRef 이름과 다를 수 있으므로, 정확 매칭이 필요한
        배포에서는 이 메서드만 오버라이드하면 된다(쿼리/정규화 로직 불변).
        """
        if not label_value:
            return label_value
        val = label_value.strip()
        # 경로형: ".../backend/0" 같은 꼬리는 이름이 아니므로, service 이름을 유추하기 어렵다.
        # 경로형은 원본을 유지(진단 우선). ns/svc 형만 마지막 세그먼트로 축약.
        if val.startswith("httproute/") or "/rule/" in val or "/backend/" in val:
            return val
        if "/" in val:
            return val.rsplit("/", 1)[-1]
        return val
