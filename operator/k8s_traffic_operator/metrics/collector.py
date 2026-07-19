"""metrics 진입점 - handlers.py 가 `metrics.collect(spec)` 로 호출한다.

책임:
    1) CRD spec 파싱 (target / window) 및 관측 윈도우 정규화
    2) Gateway API 구현체 어댑터 선택 (구현체 중립을 위해 이름 매핑은 어댑터 안에서만)
    3) 어댑터가 만든 PromQL 을 Prometheus 로 실행하고 결과를 TrafficSnapshot 으로 정규화
    4) 실패/무데이터를 status 로 안전하게 구분 (예외를 handlers 로 던지지 않음)

정규화 규약(schemas.py 준수, 위반 금지):
    - latency : ms (float)          - 어댑터 latency_unit 이 "s" 면 *1000
    - error_rate : 0.0~1.0 (float)  - error_rps / total_rps 로 계산. 퍼센트 아님
    - rps : req/s (float)
    - window_seconds : 초 (int)     - CRD spec.window 파생
    - timestamp : Unix epoch 초 (float, UTC) - time.time()

status 판정:
    - Prometheus 접근/쿼리 실패        -> "collection_failed" (지표 전부 None)
    - 쿼리 성공했으나 total 트래픽 없음 -> "no_data"          (지표 전부 None, 0으로 위장 금지)
    - total 트래픽 관측됨               -> "ok"

설정(spec/스키마에 없는 값 → 가정. 이견 시 조정):
    - Prometheus 엔드포인트: 환경변수 PROMETHEUS_URL, 없으면 spec.metrics.prometheusUrl,
      둘 다 없으면 "http://prometheus-server.monitoring.svc:80".
    - Gateway 구현체: 환경변수 GATEWAY_IMPLEMENTATION, 없으면 spec.metrics.implementation,
      둘 다 없으면 어댑터 레지스트리의 DEFAULT_IMPL(envoy-gateway).
    - total_ready_pods: kube-state-metrics 가 Prometheus 로 스크랩된다고 가정하고
      kube_deployment_status_replicas_ready 로 조회. 불가/부재 시 None(로깅), status 는 유지.
    spec 은 dict 이며 아직 스키마에 없는 metrics 하위 블록은 .get() 으로 안전 접근한다
    (architect 가 후속에 추가할 수 있으므로 forward-compatible).

Hubble(CNI) 대안 경로:
    spec.metrics.implementation(또는 GATEWAY_IMPLEMENTATION 환경변수)이 "cilium-hubble"/
    "hubble"/"cilium"이면 Prometheus/어댑터 경로를 완전히 건너뛰고 hubble_collector.py로
    위임한다. Gateway API가 트래픽 메트릭을 노출하지 않는 클러스터에서도 Cilium/Hubble이
    떠 있으면 실제 트래픽을 볼 수 있다는 것이 이 경로의 존재 이유다 — 단, rps/error_rate의
    의미가 다르고(연결 단위, HTTP 아님) latency/per_backend는 제공되지 않는다.
    자세한 내용은 hubble_collector.py의 모듈 docstring 참조.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Dict, List, Optional, Tuple

from ..schemas import BackendTraffic, TrafficSnapshot
from . import hubble_collector
from .adapters import DEFAULT_IMPL, get_adapter
from .prometheus_client import (
    PrometheusClient,
    PrometheusConnectionError,
    Sample,
)

log = logging.getLogger(__name__)

_DEFAULT_PROM_URL = "http://prometheus-server.monitoring.svc:80"
_DEFAULT_WINDOW = "1m"

# "cilium-hubble" 등으로 지정되면 Gateway API/Prometheus 경로를 완전히 건너뛰고
# hubble_collector로 위임한다. Hubble은 GatewayAdapter가 아니다(PromQL을 쓰지 않으므로
# adapters/ 레지스트리에 넣지 않는다) — 별도의 수집 경로로 취급한다.
_HUBBLE_IMPL_NAMES = frozenset({"cilium-hubble", "hubble", "cilium"})

# CRD window 패턴: ^[0-9]+(ms|s|m|h)$  (PromQL range 문자열과 동일 표기)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


# =========================================================================== #
# 진입점
# =========================================================================== #
def collect(spec: dict) -> TrafficSnapshot:
    """CRD spec 을 받아 트래픽 스냅샷을 수집·정규화한다. 예외를 밖으로 던지지 않는다."""
    window_str, window_seconds = _parse_window((spec or {}).get("window"))
    now = time.time()

    try:
        impl = _resolve_impl(spec)
        if (impl or "").strip().lower() in _HUBBLE_IMPL_NAMES:
            # Hubble 경로는 HTTPRoute를 쓰지 않는다(Gateway API를 모름) - target.deployment만으로
            # 특정한다. 이 try 블록 안에 두어야 hubble_collector 내부에서 예기치 못한 예외가
            # 나도(collect() 자체의 "예외를 밖으로 던지지 않는다" 계약이 깨지지 않는다.
            return hubble_collector.collect(spec, window_seconds, now)

        target = (spec or {}).get("target", {}) or {}
        route = target.get("httpRoute")
        namespace = target.get("namespace")  # None 허용(어댑터가 처리)
        deployment = target.get("deployment")

        if not route:
            # 관측 대상을 특정할 수 없음 = 수집 불가. 트래픽 0 과 구분하기 위해 failed 로 명시.
            log.error("spec.target.httpRoute 누락 - 수집 대상 특정 불가")
            return _failed(now, window_seconds, meta={"error": "target.httpRoute missing"})

        adapter = get_adapter(impl)
        client = _build_client(spec)

        base_meta = {
            "implementation": adapter.IMPL_NAME,
            "requested_implementation": impl or DEFAULT_IMPL,
            "prometheus_url": client.base_url,
            "route": route,
            "namespace": namespace or "",
            "window": window_str,
        }

        return _collect(
            adapter, client, route, namespace, deployment,
            window_str, window_seconds, now, base_meta,
        )

    except PrometheusConnectionError as e:
        log.warning("Prometheus 수집 실패 → collection_failed: %s", e)
        return _failed(now, window_seconds, meta={"error": str(e)})
    except Exception as e:  # 어떤 경우에도 reconcile 루프를 죽이지 않는다.
        log.exception("metrics.collect 예기치 못한 오류 → collection_failed")
        return _failed(now, window_seconds, meta={"error": f"{type(e).__name__}: {e}"})


# =========================================================================== #
# 내부 구현
# =========================================================================== #
def _collect(
    adapter,
    client: PrometheusClient,
    route: str,
    namespace: Optional[str],
    deployment: Optional[str],
    window_str: str,
    window_seconds: int,
    now: float,
    base_meta: Dict[str, str],
) -> TrafficSnapshot:
    """실제 쿼리 실행 + 정규화. Prometheus 오류는 상위 collect() 가 잡는다."""
    agg_q = adapter.aggregate_queries(route, namespace, window_str)
    bk_q = adapter.backend_queries(route, namespace, window_str)

    # --- 1) total_rps 로 no_data 여부부터 판정 (분모이자 관측 유무의 기준) ---
    total_rps = _scalar(client.query(agg_q["total_rps"]))
    if total_rps is None:
        # 쿼리는 성공했으나 매칭 시계열 없음 = 트래픽 없음/신규 배포/레이블 불일치.
        # 0 으로 위장하지 않고 no_data 로 명시한다.
        log.info("total_rps 결과 없음 → no_data (route=%s)", route)
        meta = dict(base_meta)
        meta["reason"] = "no matching request series (total_rps empty)"
        return TrafficSnapshot(
            status="no_data",
            timestamp=now,
            window_seconds=window_seconds,
            meta=meta,
        )

    # --- 2) 나머지 aggregate 지표 ---
    error_rps = _scalar(client.query(agg_q["error_rps"])) or 0.0  # 5xx 없음 = 진짜 0 (traffic 존재)
    error_rate = _safe_ratio(error_rps, total_rps)

    p50 = _to_ms(_scalar(client.query(agg_q["p50"])), adapter.latency_unit)
    p95 = _to_ms(_scalar(client.query(agg_q["p95"])), adapter.latency_unit)
    p99 = _to_ms(_scalar(client.query(agg_q["p99"])), adapter.latency_unit)

    # --- 3) per-backend 분해 ---
    per_backend = _build_per_backend(adapter, client, bk_q)

    # --- 4) total_ready_pods (RPS/pod 분모) - best effort ---
    total_ready_pods = _query_ready_pods(client, deployment, namespace)

    meta = dict(base_meta)
    meta["backends"] = str(len(per_backend))
    meta["ready_pods_source"] = (
        "kube_deployment_status_replicas_ready" if total_ready_pods is not None else "unavailable"
    )

    return TrafficSnapshot(
        status="ok",
        timestamp=now,
        window_seconds=window_seconds,
        rps=total_rps,
        error_rate=error_rate,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        p99_latency_ms=p99,
        total_ready_pods=total_ready_pods,
        per_backend=per_backend,
        meta=meta,
    )


def _build_per_backend(adapter, client: PrometheusClient, bk_q: Dict[str, str]) -> List[BackendTraffic]:
    """backend 레이블별 rps/error_rate/p99 를 join 하여 BackendTraffic 목록 생성.

    ready_pods(per-backend)는 backend→Deployment 매핑 정보가 없으므로 None 으로 둔다
    (전체 total_ready_pods 만 채운다). backend 별 Deployment 매핑이 필요해지면 CRD 확장 후 보강.
    """
    rps_by = _vector(client.query(bk_q["rps"]), adapter.backend_label, adapter.map_backend_name)
    err_by = _vector(client.query(bk_q["error_rps"]), adapter.backend_label, adapter.map_backend_name)
    p99_by = _vector(client.query(bk_q["p99"]), adapter.backend_label, adapter.map_backend_name)

    backends: List[BackendTraffic] = []
    for name in sorted(rps_by.keys()):
        rps = rps_by[name]
        err = err_by.get(name, 0.0)  # 5xx 시계열 없음 = 이 backend 는 에러 0 (rps 는 존재)
        p99 = p99_by.get(name)
        backends.append(
            BackendTraffic(
                name=name,
                rps=rps,
                error_rate=_safe_ratio(err, rps),
                p99_latency_ms=_to_ms(p99, adapter.latency_unit),
                ready_pods=None,
            )
        )
    return backends


def _query_ready_pods(
    client: PrometheusClient, deployment: Optional[str], namespace: Optional[str]
) -> Optional[int]:
    """kube-state-metrics 로 대상 Deployment 의 Ready replica 수를 조회(best effort)."""
    if not deployment:
        return None
    sel = [f'deployment="{deployment}"']
    if namespace:
        sel.append(f'namespace="{namespace}"')
    q = f'sum(kube_deployment_status_replicas_ready{{{",".join(sel)}}})'
    try:
        val = _scalar(client.query(q))
    except PrometheusConnectionError as e:
        # ready_pods 미확보가 전체 수집을 실패시키지 않도록 여기서 흡수(None + 로깅).
        log.warning("ready_pods 조회 실패(무시하고 None): %s", e)
        return None
    if val is None:
        return None
    return int(round(val))


# =========================================================================== #
# 값 헬퍼
# =========================================================================== #
def _scalar(samples: List[Sample]) -> Optional[float]:
    """단일 값 쿼리 결과에서 float 하나를 뽑는다. 없음/NaN -> None (0 으로 위장 금지)."""
    if not samples:
        return None
    v = samples[0].value
    if v is None or math.isnan(v) or math.isinf(v):
        return None
    return float(v)


def _vector(samples: List[Sample], label: str, name_map) -> Dict[str, float]:
    """벡터 결과를 {backend_name: value} 로 변환. NaN/Inf 샘플은 제외."""
    out: Dict[str, float] = {}
    for s in samples:
        if s.value is None or math.isnan(s.value) or math.isinf(s.value):
            continue
        raw = s.labels.get(label, "")
        name = name_map(raw)
        if not name:
            continue
        # 동일 backend 로 매핑되는 시계열이 여러 개면 합산.
        out[name] = out.get(name, 0.0) + float(s.value)
    return out


def _safe_ratio(numer: float, denom: float) -> float:
    """error_rate 계산. denom<=0 이면 0.0 (여기 도달 시 total_rps>0 이 보장됨)."""
    if denom <= 0:
        return 0.0
    r = numer / denom
    # 부동소수/집계 지연으로 아주 살짝 1.0 초과할 수 있어 클램프.
    if r < 0:
        return 0.0
    if r > 1.0:
        return 1.0
    return r


def _to_ms(value: Optional[float], unit: str) -> Optional[float]:
    """지연시간을 ms 로 정규화. None 은 그대로(결측). unit=="s" 면 *1000."""
    if value is None:
        return None
    return value * 1000.0 if unit == "s" else value


# =========================================================================== #
# 설정 파싱
# =========================================================================== #
def _parse_window(raw: Optional[str]) -> Tuple[str, int]:
    """CRD window("1m","30s","500ms","2h") -> (PromQL range 문자열, window_seconds:int).

    PromQL range 는 CRD 표기와 동일하므로 그대로 사용(하드코딩 금지 원칙 준수).
    파싱 불가 시 기본 "1m" 로 폴백하고 경고.
    """
    if not raw or not isinstance(raw, str):
        return _DEFAULT_WINDOW, 60

    s = raw.strip()
    # 뒤에서부터 단위 추출
    for unit in ("ms", "s", "m", "h"):
        if s.endswith(unit):
            num = s[: -len(unit)]
            if num.isdigit() and int(num) > 0:
                seconds = int(num) * _UNIT_SECONDS[unit]
                # window_seconds 는 int. 1초 미만이면 최소 1로 올림.
                return s, max(1, int(round(seconds)))
            break
    log.warning("window 값 '%s' 파싱 실패 → 기본 '%s' 사용", raw, _DEFAULT_WINDOW)
    return _DEFAULT_WINDOW, 60


def _resolve_impl(spec: dict) -> str:
    """Gateway 구현체 식별자 결정: env > spec.metrics.implementation > DEFAULT_IMPL."""
    env = os.getenv("GATEWAY_IMPLEMENTATION")
    if env:
        return env
    metrics_cfg = (spec or {}).get("metrics", {}) or {}
    return metrics_cfg.get("implementation") or DEFAULT_IMPL


def _build_client(spec: dict) -> PrometheusClient:
    """Prometheus 클라이언트 구성: env > spec.metrics.prometheusUrl > 기본값. 재시도 1회."""
    metrics_cfg = (spec or {}).get("metrics", {}) or {}
    url = (
        os.getenv("PROMETHEUS_URL")
        or metrics_cfg.get("prometheusUrl")
        or _DEFAULT_PROM_URL
    )
    return PrometheusClient(base_url=url, timeout=5.0, retries=1)


def _failed(now: float, window_seconds: int, meta: Dict[str, str]) -> TrafficSnapshot:
    """collection_failed 스냅샷 생성. 지표는 전부 None."""
    return TrafficSnapshot(
        status="collection_failed",
        timestamp=now,
        window_seconds=window_seconds,
        meta=meta,
    )
