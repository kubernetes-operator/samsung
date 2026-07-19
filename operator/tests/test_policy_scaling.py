"""scaling.py 단위 테스트 - hysteresis, 결측치 방어, capacity_target_for_anomaly."""

from __future__ import annotations

from k8s_traffic_operator.policy.scaling import assess_scaling, capacity_target_for_anomaly


def test_scale_up_when_rps_per_pod_exceeds_target():
    # rps=300, pods=5 -> 60/pod > target(50) -> 업. desired = ceil(300/50)=6
    result = assess_scaling(300.0, 5, 50.0, 20.0, 2, 20)
    assert result.direction == "up"
    assert result.target_replicas == 6


def test_scale_down_when_rps_per_pod_below_scale_down_threshold():
    # rps=50, pods=5 -> 10/pod < scaleDown(20) -> 다운. desired = ceil(50/50)=1 -> clamp to min(2)
    result = assess_scaling(50.0, 5, 50.0, 20.0, 2, 20)
    assert result.direction == "down"
    assert result.target_replicas == 2  # minReplicas clamp


def test_hysteresis_band_holds_steady():
    # rps=150, pods=5 -> 30/pod: target(50)보다 낮고 scaleDown(20)보다 높음 -> 밴드 내부 유지.
    result = assess_scaling(150.0, 5, 50.0, 20.0, 2, 20)
    assert result.direction == "none"
    assert result.target_replicas is None


def test_missing_rps_or_target_is_conservative_none():
    assert assess_scaling(None, 5, 50.0, 20.0, 2, 20).direction == "none"
    assert assess_scaling(100.0, 5, None, 20.0, 2, 20).direction == "none"
    assert assess_scaling(100.0, 5, 0.0, 20.0, 2, 20).direction == "none"


def test_missing_current_pods_is_conservative_none():
    result = assess_scaling(300.0, None, 50.0, 20.0, 2, 20)
    assert result.direction == "none"
    assert result.rps_per_pod is None
    assert "결측" in result.reason or "결측/0" in result.reason


def test_scale_down_without_scale_down_threshold_configured_never_fires():
    # scaleDownRPSPerPod가 없으면(None) 스케일다운 게이트가 없으므로 절대 down이 나오면 안 된다.
    result = assess_scaling(10.0, 5, 50.0, None, 2, 20)
    assert result.direction != "down"


def test_max_replicas_clamp_applied_to_scale_up_target():
    result = assess_scaling(10_000.0, 5, 50.0, 20.0, 2, 10)
    assert result.direction == "up"
    assert result.target_replicas == 10  # maxReplicas clamp


def test_capacity_target_for_anomaly_guarantees_at_least_plus_one():
    # rps 자체는 낮아도(임계 미만) 최소 current+1은 보장.
    target = capacity_target_for_anomaly(rps=10.0, current_pods=5, target_rps_per_pod=50.0, min_replicas=2, max_replicas=20)
    assert target == 6


def test_capacity_target_for_anomaly_none_when_current_pods_missing():
    assert capacity_target_for_anomaly(100.0, None, 50.0, 2, 20) is None
    assert capacity_target_for_anomaly(100.0, 0, 50.0, 2, 20) is None


def test_capacity_target_for_anomaly_respects_max_replicas():
    target = capacity_target_for_anomaly(rps=10_000.0, current_pods=5, target_rps_per_pod=50.0, min_replicas=2, max_replicas=8)
    assert target == 8
