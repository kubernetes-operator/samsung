"""Cilium Hubble(CNI) 기반 트래픽 스냅샷 수집 - Gateway API 메트릭이 없는 클러스터용 대안 경로.

`metrics/collector.py`의 기본 경로는 Gateway API 구현체(Envoy Gateway/Istio/nginx Gateway
Fabric)가 Prometheus에 노출하는 HTTP 레벨 메트릭을 쓴다. 이 클러스터처럼 Gateway가 트래픽
메트릭을 아예 노출하지 않아도, Cilium/Hubble이 이미 떠 있다면(enable-hubble=true) CNI
레벨에서 실제 Pod 트래픽을 관측할 수 있다 — 이 모듈이 그 경로다.

## 중요: 이 소스로 채워지는 값의 의미가 Gateway API 경로와 다르다

- **rps**: 관측 윈도우 내 대상(namespace/deployment)으로 향한 **L3/L4 연결(flow) 수 / 윈도우
  초**다. HTTP 요청 수가 아니다 — 하나의 TCP 연결 위에서 여러 HTTP 요청이 오갈 수 있고
  (keep-alive), 반대로 하나의 flow 이벤트가 요청 하나에 대응하지 않을 수도 있다. "연결 활동량"
  근사치로 이해해야 하며, 진짜 HTTP RPS와 동일시하면 오해다.
- **error_rate**: verdict가 FORWARDED가 아닌(DROPPED/ERROR/AUDIT 등) 연결의 비율이다. 이는
  **네트워크 정책(CiliumNetworkPolicy)에 의한 거부율**이지 HTTP 5xx 비율이 아니다. 제한적인
  NetworkPolicy가 없는 클러스터(이 프로젝트가 실측한 클러스터 포함)에서는 사실상 항상 0에
  가깝다 — "에러가 없다"가 아니라 "이 신호로는 애플리케이션 에러를 볼 수 없다"는 뜻이다.
- **지연시간(p50/p95/p99)**: Hubble이 L7 가시성(HTTP 파싱) 없이 기본 동작할 때는 응답 시간을
  전혀 제공하지 않는다. 항상 None — 값을 지어내지 않는다. anomaly.py는 p99가 None이면
  지연시간 기반 이상탐지를 건너뛰므로, 이 소스에서는 그 축이 비활성 상태가 된다.
- **per_backend(격리 판단용)**: 비운다(빈 리스트). Hubble은 Gateway API의 HTTPRoute
  backendRef 개념을 모르므로, Pod/워크로드 단위 데이터를 backendRef 이름에 억지로 끼워
  맞추면 actuator가 엉뚱한(또는 존재하지 않는) backend를 격리하려 시도할 위험이 있다.
  따라서 이 소스는 **스케일링/전체 이상탐지 판단만 지원**하고, isolate_backend/reroute는
  이 소스에서 트리거되지 않는다(policy/anomaly.py의 culprit_backends가 항상 빈 값이 되므로
  자연히 비활성).

## 대상 특정 방법

CRD에는 HTTPRoute가 필수 필드이지만 Hubble 조회에는 쓰지 않는다(Hubble은 Gateway API를
모른다). 대신 `target.namespace` + `target.deployment`로 대상을 특정한다:
  1. hubble-relay에는 `--to-namespace`로만 필터링해 효율적으로 좁힌다(relay 측 필터).
  2. 정확한 워크로드 매칭은 Python에서 한다 — flow의 destination.workloads[].name이
     deployment 이름과 일치하는 것을 우선 사용하고, workloads 정보가 없는 flow는
     destination pod 이름이 "<deployment>-"로 시작하는지로 보수적으로 폴백 매칭한다
     (`--to-workload` CLI 플래그의 정확한 매칭 규칙이 버전별로 다를 수 있어, 이미
     검증된 방식인 namespace 필터 + 자체 파싱 필드로 매칭하는 쪽을 택했다).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from .. import hubble_client
from ..hubble_client import HubbleUnavailableError
from ..schemas import TrafficSnapshot

log = logging.getLogger(__name__)

# 한 번에 relay에서 가져올 flow 개수(네임스페이스로 이미 좁혀진 뒤의 개수).
HUBBLE_METRICS_LAST = int(os.getenv("HUBBLE_METRICS_LAST", "2000"))

_apps_v1_cache: Optional[client.AppsV1Api] = None


def _load_apps_v1() -> client.AppsV1Api:
    """kubernetes client를 지연 초기화한다. in-cluster 우선, 실패 시 로컬 kubeconfig."""
    global _apps_v1_cache
    if _apps_v1_cache is not None:
        return _apps_v1_cache
    try:
        config.load_incluster_config()
    except Exception:  # noqa: BLE001 - 클러스터 밖 실행(개발/테스트) 대비 폴백
        config.load_kube_config()
    _apps_v1_cache = client.AppsV1Api()
    return _apps_v1_cache


def _query_ready_pods(namespace: Optional[str], deployment: Optional[str]) -> Optional[int]:
    """대상 Deployment의 Ready replica 수를 Kubernetes API로 직접 조회한다(best effort).

    Gateway API 경로(metrics/collector.py)는 이 값을 Prometheus의 kube-state-metrics로
    얻지만, Hubble 경로는 애초에 "Prometheus 없이도 동작"하는 것이 목적이므로 여기서는
    K8s API를 직접 쓴다 — Prometheus 의존성을 새로 만들지 않는다.
    """
    if not namespace or not deployment:
        return None
    try:
        apps_v1 = _load_apps_v1()
        dep = apps_v1.read_namespaced_deployment(deployment, namespace)
        ready = dep.status.ready_replicas
        return int(ready) if ready is not None else 0
    except ApiException as exc:
        log.warning("ready_pods 조회 실패(무시하고 None): %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - kubeconfig 로드 실패 등도 흡수
        log.warning("ready_pods 조회 중 예기치 못한 오류(무시하고 None): %s", exc)
        return None


def _matches_target(dst_workload: Optional[str], dst_pod_name: Optional[str], deployment: str) -> bool:
    if dst_workload:
        return dst_workload == deployment
    if dst_pod_name:
        return dst_pod_name.startswith(f"{deployment}-")
    return False


def collect(spec: dict, window_seconds: int, now: float) -> TrafficSnapshot:
    """Hubble 기반으로 TrafficSnapshot을 만든다. 예외를 밖으로 던지지 않는다."""
    target = (spec or {}).get("target", {}) or {}
    namespace = target.get("namespace")
    deployment = target.get("deployment")

    if not deployment:
        log.error("spec.target.deployment 누락 - Hubble 수집 대상 특정 불가")
        return TrafficSnapshot(
            status="collection_failed", timestamp=now, window_seconds=window_seconds,
            meta={"source": "cilium-hubble", "error": "target.deployment missing"},
        )

    extra_args = ["--to-namespace", namespace] if namespace else []

    try:
        events = hubble_client.fetch_flows(HUBBLE_METRICS_LAST, extra_args=extra_args)
    except HubbleUnavailableError as exc:
        log.warning("Hubble 수집 실패 → collection_failed: %s", exc)
        return TrafficSnapshot(
            status="collection_failed", timestamp=now, window_seconds=window_seconds,
            meta={"source": "cilium-hubble", "error": str(exc)},
        )

    cutoff = now - window_seconds
    windowed = [
        e for e in events
        if e.epoch is not None and e.epoch >= cutoff and _matches_target(e.dst.workload, e.dst.pod_name, deployment)
    ]

    base_meta = {
        "source": "cilium-hubble",
        "namespace": namespace or "",
        "deployment": deployment,
        "window_seconds": str(window_seconds),
    }

    if not windowed:
        log.info("Hubble: 윈도우 내 매칭 flow 없음 → no_data (namespace=%s, deployment=%s)", namespace, deployment)
        meta = dict(base_meta)
        meta["reason"] = "no matching flows in window"
        return TrafficSnapshot(status="no_data", timestamp=now, window_seconds=window_seconds, meta=meta)

    total = len(windowed)
    non_forwarded = sum(1 for e in windowed if e.verdict != "FORWARDED")
    rps = total / window_seconds
    error_rate = (non_forwarded / total) if total else 0.0

    total_ready_pods = _query_ready_pods(namespace, deployment)

    meta = dict(base_meta)
    meta["sampled_flows"] = str(total)
    meta["ready_pods_source"] = "kubernetes_api" if total_ready_pods is not None else "unavailable"
    meta["note"] = (
        "rps/error_rate는 L3/L4 연결 단위 근사치(HTTP 요청/에러코드 아님). "
        "latency 및 per_backend는 이 소스에서 제공되지 않음(의도적으로 None/빈 값)."
    )

    return TrafficSnapshot(
        status="ok",
        timestamp=now,
        window_seconds=window_seconds,
        rps=rps,
        error_rate=error_rate,
        total_ready_pods=total_ready_pods,
        per_backend=[],
        meta=meta,
    )
