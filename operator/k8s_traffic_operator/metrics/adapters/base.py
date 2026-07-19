"""Gateway API 구현체별 어댑터의 공통 인터페이스.

핵심 목적: 정책 엔진(policy)과 collector 의 정규화 로직이 "어떤 구현체인지"를 전혀
모르게 만든다. 구현체마다 다른 것은 오직 두 가지다.
    1) 메트릭 이름 / 레이블 이름 (envoy_http_downstream_rq_total vs istio_requests_total)
    2) backend 를 식별하는 레이블 (envoy_cluster_name vs destination_workload)

이 어댑터는 그 차이를 흡수해서 **동일한 키 집합의 PromQL 문자열**을 만들어 낸다.
collector 는 어댑터가 만든 쿼리를 실행하고, 반환 키만 보고 TrafficSnapshot 을 채운다 —
따라서 collector 도 구현체 중립이다.

어댑터가 만드는 쿼리 키(계약):
    aggregate_queries() ->
        "total_rps"  : 전체 초당 요청 수 (스칼라, 5xx 포함 전부)
        "error_rps"  : 5xx 초당 요청 수 (스칼라)
        "p50"/"p95"/"p99" : 전체 지연시간 분위수 (스칼라, 단위는 아래 latency_unit 참조)
    backend_queries() ->
        "rps"        : backend 레이블별 초당 요청 수 (벡터, by backend_label)
        "error_rps"  : backend 레이블별 5xx 초당 요청 수 (벡터, by backend_label)
        "p99"        : backend 레이블별 p99 지연시간 (벡터, by backend_label + le)

에러율은 어댑터가 직접 나눗셈으로 만들지 않는다. total_rps / error_rps 를 각각 돌려주고
collector 가 파이썬에서 error_rps/total_rps 로 계산한다. 이유: PromQL 에서 5xx 가 0건이면
분자 시계열이 비어 division 결과가 empty 가 되어 "에러율 0"과 "데이터 없음"을 구분할 수
없게 된다. 분모(total)를 별도로 관측하면 이 둘을 안전하게 구분할 수 있다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional

# p50/p95/p99 -> histogram_quantile 인자
QUANTILES = {"p50": 0.5, "p95": 0.95, "p99": 0.99}


class GatewayAdapter(ABC):
    """Gateway API 구현체별 메트릭 어댑터의 추상 베이스."""

    #: 구현체 식별자 (레지스트리 키). 예: "envoy-gateway", "istio".
    IMPL_NAME: str = "base"

    #: per-backend 집계에 사용할 Prometheus 레이블 이름.
    backend_label: str = ""

    #: 이 구현체의 지연시간 히스토그램 단위.
    #: TrafficSnapshot 은 ms 로 고정이므로, collector 가 이 값을 보고 s->ms 변환 여부를 정한다.
    #: "ms" 또는 "s".
    latency_unit: str = "ms"

    @abstractmethod
    def aggregate_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        """전체(aggregate) 지표용 PromQL 문자열 집합을 만든다.

        window 는 CRD spec.window 값을 그대로 쓴 PromQL range 문자열(예: "1m", "30s").
        하드코딩 금지 — 관측 윈도우가 스케일링 민감도를 결정하므로 CRD 값을 그대로 반영한다.
        """

    @abstractmethod
    def backend_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        """per-backend 분해 지표용 PromQL 문자열 집합을 만든다."""

    def map_backend_name(self, label_value: str) -> str:
        """Prometheus 레이블 값 -> HTTPRoute backendRef 이름 매핑.

        기본은 항등 함수. 구현체별 cluster 명명 규칙(예: Envoy Gateway 의 cluster path)이
        backendRef 이름과 다르면 서브클래스에서 오버라이드한다.

        중요: BackendTraffic.name 은 actuator 가 HTTPRoute backendRef 와 매칭하는 키다.
        여기서 정확히 매핑하지 못하면 actuator 의 격리(isolate_backend)가 대상 backend 를
        못 찾는다. 정확한 규칙을 모를 때는 원본 레이블을 그대로 두어(항등) 최소한 진단은
        가능하게 한다.
        """
        return label_value
