"""nginx Gateway Fabric 어댑터 (Gateway API 구현체 #3).

nginx Gateway Fabric(NGF)의 데이터플레인은 NGINX(OSS 또는 Plus)다. per-route/per-backend
트래픽 통계는 NGINX Plus API(`/api/{ver}/http/server_zones`, `/api/{ver}/http/upstreams`)를
통해서만 얻을 수 있고, 이를 Prometheus 형식으로 재노출하는 표준 도구가
nginx-prometheus-exporter(NGINX 공식)다. 이 어댑터는 그 익스포터가 노출하는
`nginxplus_http_*` 메트릭 계약을 기준으로 작성했다.

    개념        | 메트릭 / 필터
    ------------|--------------------------------------------------------------
    요청 수     | nginxplus_http_server_zone_requests (server_zone 레이블)
    5xx         | nginxplus_http_server_zone_responses{code="5xx"}
    backend     | nginxplus_http_upstream_server_requests/_responses (upstream 레이블)
    지연시간    | (아래 "지연시간 제약" 참조 — 부분 지원)

이 클러스터 실측 확인(2026-07-19): 실제로 배포된 nginx-gateway 파드의 포트 9113은
컨트롤 플레인(Go controller-runtime) 자체 지표만 노출하고 있었고, 데이터플레인
nginx 컨테이너에는 stub_status/메트릭 location이 구성되어 있지 않았다. 즉 이 코드는
"nginxplus_http_* 익스포터가 켜져 있는 배포"를 대상으로 작성된 것이며, 이 저장소의
샘플 클러스터에서 실제 엔드-투-엔드로 검증되지는 않았다(모듈 단위 mock 테스트로만 검증).
운영 클러스터에 적용하기 전에 실제 노출 여부/버전별 레이블을 다시 확인할 것.

## 지연시간 제약 (중요)

NGINX Plus API는 Envoy/Istio처럼 지연시간 히스토그램을 제공하지 않는다 — 최근 구간의
"평균" 응답시간(`response_time`)만 게이지로 제공한다. 평균을 p99인 것처럼 속여서 채우면
tail latency 이상을 오히려 놓치게 되므로(평균은 분포의 꼬리를 감춘다), 이 어댑터는:
  - p50: 평균 응답시간을 근사치로 채운다(주석대로 근사치이며 참고용).
  - p95/p99: 실제로 존재하지 않는 메트릭을 조회하여 의도적으로 빈 결과(None)를 반환한다.
    policy/anomaly.py는 p99가 None이면 지연시간 기반 이상탐지를 건너뛰므로(결측치 정직 원칙),
    이 어댑터를 쓰는 배포에서는 에러율/RPS 기반 판단만 유효하고 지연시간 기반 판단은
    비활성 상태가 된다 — 침묵의 오탐(fabricated precision)보다 명시적 결측이 안전하다.
"""

from __future__ import annotations

from typing import Dict, Optional

from .base import GatewayAdapter


class NginxGatewayFabricAdapter(GatewayAdapter):
    IMPL_NAME = "nginx-gateway-fabric"
    backend_label = "upstream"
    latency_unit = "ms"

    # ------------------------------------------------------------------ selectors
    def zone_selector(self, route: str, namespace: Optional[str]) -> str:
        """server_zone 레이블로 HTTPRoute를 특정한다.

        가정: NGF는 Gateway listener(hostname) 단위로 server_zone을 구성하므로, 정확히는
        HTTPRoute가 아니라 그 라우트가 붙은 listener/hostname 기준이다. 이 저장소는 단순화를
        위해 HTTPRoute 이름과 server_zone 이름이 같다고 가정한다(Envoy Gateway 어댑터의
        conn_manager_prefix 가정과 동일한 성격) — 실제 배포에서 다르면 이 메서드만
        오버라이드하면 된다.
        """
        return f'server_zone="{route}"'

    def upstream_selector(self, route: str, namespace: Optional[str]) -> str:
        """upstream 레이블로 이 라우트의 backend 그룹을 특정한다.

        가정: NGF가 만드는 upstream 이름에 HTTPRoute 이름이 포함된다(예: "<ns>_<route>_..").
        정확한 명명 규칙은 NGF 버전에 따라 다를 수 있어 부분 일치(정규식)로 느슨하게 잡는다.
        """
        return f'upstream=~".*{route}.*"'

    # ------------------------------------------------------------------ aggregate
    def aggregate_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        zone_sel = self.zone_selector(route, namespace)
        up_sel = self.upstream_selector(route, namespace)

        return {
            "total_rps": f"sum(rate(nginxplus_http_server_zone_requests{{{zone_sel}}}[{window}]))",
            "error_rps": (
                f'sum(rate(nginxplus_http_server_zone_responses{{{zone_sel},code="5xx"}}[{window}]))'
            ),
            # p50: 평균 응답시간(ms)을 근사치로 사용. NGINX Plus API는 percentile을 제공하지
            # 않으므로 진짜 p50이 아니라 "대략적인 중심 경향" 참고값이다.
            "p50": f"avg(nginxplus_http_upstream_server_response_time{{{up_sel}}})",
            # p95/p99: NGINX Plus API에 존재하지 않는 메트릭을 의도적으로 조회하여 항상
            # 빈 결과(None)를 반환한다. 평균을 percentile인 것처럼 위장하지 않기 위함.
            "p95": f"avg(nginxplus_http_upstream_server_response_time_percentile{{{up_sel},quantile=\"0.95\"}})",
            "p99": f"avg(nginxplus_http_upstream_server_response_time_percentile{{{up_sel},quantile=\"0.99\"}})",
        }

    # ------------------------------------------------------------------ per-backend
    def backend_queries(
        self, route: str, namespace: Optional[str], window: str
    ) -> Dict[str, str]:
        up_sel = self.upstream_selector(route, namespace)
        lbl = self.backend_label

        return {
            "rps": f"sum by ({lbl}) (rate(nginxplus_http_upstream_server_requests{{{up_sel}}}[{window}]))",
            "error_rps": (
                f'sum by ({lbl}) (rate(nginxplus_http_upstream_server_responses'
                f'{{{up_sel},code="5xx"}}[{window}]))'
            ),
            # 위와 동일한 이유로 p99는 실제로 존재하지 않는 메트릭 조회 -> None.
            "p99": (
                f'avg by ({lbl}) (nginxplus_http_upstream_server_response_time_percentile'
                f'{{{up_sel},quantile="0.99"}})'
            ),
        }

    # ------------------------------------------------------------------ name mapping
    def map_backend_name(self, label_value: str) -> str:
        """upstream 레이블 -> HTTPRoute backendRef 이름 추정.

        NGF의 upstream 이름은 보통 "<namespace>_<service>_<port>" 형태다. backendRef 이름은
        서비스 이름이므로 언더스코어로 분해했을 때 가운데 세그먼트를 취한다. 세그먼트가
        3개가 아니면(명명 규칙이 다르면) 원본을 그대로 반환한다(진단 우선, 억지 추측 금지).
        """
        if not label_value:
            return label_value
        parts = label_value.split("_")
        if len(parts) == 3:
            return parts[1]
        return label_value
