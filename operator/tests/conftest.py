"""공유 픽스처.

policy.baseline이 프로세스 전역 딕셔너리(_BASELINES)에 상태를 보관하므로, 각 테스트
시작 전에 반드시 초기화한다 — 하지 않으면 이전 테스트의 EWMA 학습 상태가 다음 테스트로
새어 들어가 순서에 따라 결과가 달라지는 flaky 테스트가 된다.
"""

from __future__ import annotations

import pytest

from k8s_traffic_operator.policy import baseline as baseline_mod
from k8s_traffic_operator.schemas import BackendTraffic, TrafficSnapshot


@pytest.fixture(autouse=True)
def _reset_policy_baseline():
    baseline_mod.reset()
    yield
    baseline_mod.reset()


def make_spec(
    *,
    http_route: str = "checkout-route",
    namespace: str = "shop",
    deployment: str = "checkout-service",
    target_rps_per_pod: float = 50.0,
    scale_down_rps_per_pod: float = 20.0,
    scale_up_error_rate: float = 0.05,
    max_p99_latency_ms: float = 800.0,
    min_replicas: int = 2,
    max_replicas: int = 20,
    cooldown_seconds: int = 0,
    allow_route_isolation: bool = True,
    max_scale_step: int = 4,
    window: str = "1m",
    implementation: str = "envoy-gateway",
) -> dict:
    """테스트용 TrafficPolicy CRD spec(dict)을 만든다. 기본값은 CRD 예시값과 동일하게 맞춘다."""
    return {
        "target": {"httpRoute": http_route, "namespace": namespace, "deployment": deployment},
        "thresholds": {
            "targetRPSPerPod": target_rps_per_pod,
            "scaleDownRPSPerPod": scale_down_rps_per_pod,
            "scaleUpErrorRate": scale_up_error_rate,
            "maxP99LatencyMs": max_p99_latency_ms,
        },
        "actions": {
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "cooldownSeconds": cooldown_seconds,
            "allowRouteIsolation": allow_route_isolation,
            "maxScaleStep": max_scale_step,
        },
        "window": window,
        "metrics": {"implementation": implementation},
    }


def make_snapshot(
    *,
    status: str = "ok",
    timestamp: float = 1_000.0,
    window_seconds: int = 60,
    rps: float = 100.0,
    error_rate: float = 0.01,
    p50: float = 50.0,
    p95: float = 100.0,
    p99: float = 200.0,
    total_ready_pods: int = 5,
    per_backend=None,
) -> TrafficSnapshot:
    """테스트용 TrafficSnapshot을 만든다. per_backend 기본값은 backend 'a'/'b' 두 개, 둘 다 정상."""
    if per_backend is None:
        per_backend = [
            BackendTraffic(name="a", rps=rps / 2, error_rate=error_rate, p99_latency_ms=p99, ready_pods=total_ready_pods // 2),
            BackendTraffic(name="b", rps=rps / 2, error_rate=error_rate, p99_latency_ms=p99, ready_pods=total_ready_pods - total_ready_pods // 2),
        ]
    return TrafficSnapshot(
        status=status,
        timestamp=timestamp,
        window_seconds=window_seconds,
        rps=rps if status == "ok" else None,
        error_rate=error_rate if status == "ok" else None,
        p50_latency_ms=p50 if status == "ok" else None,
        p95_latency_ms=p95 if status == "ok" else None,
        p99_latency_ms=p99 if status == "ok" else None,
        total_ready_pods=total_ready_pods if status == "ok" else None,
        per_backend=per_backend if status == "ok" else [],
    )
