---
name: gateway-api-traffic-metrics
description: "Gateway API(HTTPRoute/Gateway) 뒤에서 동작하는 서비스의 트래픽 메트릭(RPS/에러율/지연시간)을 Prometheus에서 수집하고 정규화한다. Envoy Gateway/Istio 등 구현체별 메트릭 이름 매핑, PromQL 쿼리 작성, TrafficSnapshot 생성에 사용. '트래픽 메트릭 수집', 'PromQL 쿼리 작성', 'Gateway API 메트릭 연동' 요청 시 반드시 사용."
---

# Gateway API 트래픽 메트릭 수집

Gateway API 뒤의 서비스가 실제로 처리하는 트래픽(리소스 사용률이 아니라)을 수집하는 절차.

## 왜 Gateway API 기준인가

Gateway API는 Ingress의 후속 표준으로, `Gateway`(진입점)와 `HTTPRoute`(라우팅 규칙 + backendRefs)로 트래픽 경로를 선언한다. 이 경로 위에서 흐르는 실제 요청을 관측하면, Pod 내부 리소스 사용량이 아니라 **사용자가 실제로 겪는 서비스 품질**(요청 수, 실패율, 응답 속도)을 직접 볼 수 있다. 이는 라우팅 제어(HTTPRoute weight 조정)와도 같은 리소스 축에서 다뤄지므로, 스케일링/이상탐지/라우팅 제어가 하나의 데이터 소스로 통합된다.

## 구현체별 메트릭 매핑

Gateway API는 리소스 스펙 표준이고, 실제 메트릭 노출은 구현체(데이터플레인)가 담당한다. 구현체를 몰라도 되도록 어댑터 뒤에 숨긴다.

| 개념 | Envoy Gateway (Envoy 기반) | Istio (Envoy 기반, 다른 레이블 체계) |
|------|---------------------------|--------------------------------|
| 요청 수 | `envoy_http_downstream_rq_total` | `istio_requests_total` |
| 에러(5xx) | 위 메트릭에서 `envoy_response_code_class="5"` 필터 | `istio_requests_total{response_code=~"5.."}` |
| 지연시간 히스토그램 | `envoy_http_downstream_rq_time_bucket` | `istio_request_duration_milliseconds_bucket` |
| backend 구분 레이블 | `envoy_cluster_name` (HTTPRoute backendRef별 cluster) | `destination_service`, `destination_workload` |

두 구현체 모두 Prometheus 히스토그램이므로 PromQL의 `histogram_quantile`로 p50/p95/p99를 계산하는 방식은 동일하다 — 레이블 이름만 다르다.

## PromQL 쿼리 패턴

```promql
# RPS (1분 윈도우, backend별)
sum by (envoy_cluster_name) (rate(envoy_http_downstream_rq_total{envoy_http_conn_manager_prefix="checkout-route"}[1m]))

# 에러율 (5xx / 전체)
sum(rate(envoy_http_downstream_rq_total{envoy_response_code_class="5"}[1m]))
  / sum(rate(envoy_http_downstream_rq_total[1m]))

# p99 지연시간
histogram_quantile(0.99, sum by (le) (rate(envoy_http_downstream_rq_time_bucket[1m])))
```

윈도우(`[1m]`)는 CRD의 `spec.window` 값을 그대로 사용한다 — 하드코딩하지 않는다. 윈도우가 짧을수록 반응은 빠르지만 노이즈에 민감해지고, 정책 엔진의 hysteresis/cooldown이 이를 보완한다는 전제를 신뢰한다.

## 정규화 및 결측치 처리

쿼리 결과가 비어 있으면(신규 배포, 트래픽 없음, 레이블 불일치) `0`으로 채우지 말고 `TrafficSnapshot.status = "no_data"`를 반환한다. Prometheus 연결 자체가 실패하면 `"collection_failed"`를 반환한다. 이 두 상태를 정책 엔진이 구분해서 처리할 수 있어야 하므로 (`k8s-operator-design`의 `schemas.py` 참조), 여기서 값을 임의로 대체하면 하류에서 오판단이 발생한다.

```python
def normalize(raw_result) -> TrafficSnapshot:
    if raw_result is None:
        return TrafficSnapshot(status="collection_failed", rps=None, ...)
    if not raw_result.samples:
        return TrafficSnapshot(status="no_data", rps=None, ...)
    return TrafficSnapshot(status="ok", rps=raw_result.rps, ...)
```

## per-backend 분해

라우팅 격리(특정 backend만 weight 낮추기)를 정책 엔진이 판단하려면, 전체 합산 지표뿐 아니라 backend별 지표(`per_backend` 필드)가 필요하다. HTTPRoute의 `backendRefs`와 Prometheus 레이블(`envoy_cluster_name`/`destination_workload`)을 매핑하여 backend 단위로 집계한다.

## 지원하지 않는 구현체

목록에 없는 Gateway API 구현체를 만나면 예외를 던지지 않는다. 표준 Gateway API 메트릭 컨벤션(가능하다면)으로 폴백을 시도하고, 실패하면 `collection_failed`로 명시 반환하여 상위 reconcile 루프가 안전하게 noop 처리하도록 한다.
