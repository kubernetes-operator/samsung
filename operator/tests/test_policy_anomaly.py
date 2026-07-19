"""anomaly.py 단위 테스트 - 정적/EWMA 조합, severity 산정, culprit backend 판별."""

from __future__ import annotations

from k8s_traffic_operator.policy.anomaly import assess_anomaly
from k8s_traffic_operator.policy.baseline import Baseline, _MIN_SAMPLES
from k8s_traffic_operator.schemas import BackendTraffic, TrafficSnapshot


def _snapshot(error_rate=0.01, p99=100.0, per_backend=None):
    return TrafficSnapshot(
        status="ok", timestamp=1.0, window_seconds=60,
        rps=100.0, error_rate=error_rate, p99_latency_ms=p99,
        total_ready_pods=5, per_backend=per_backend or [],
    )


def test_no_anomaly_when_all_normal():
    b = Baseline()
    a = assess_anomaly(_snapshot(0.01, 100.0), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.severity == "none"
    assert a.any_anomaly is False


def test_static_threshold_breach_before_warmup_caps_at_warning():
    b = Baseline()  # warmup 전
    a = assess_anomaly(_snapshot(0.20, 100.0), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.error_static is True
    assert a.error_ewma is False  # warmup 전이라 EWMA 신호 없음
    assert a.severity == "warning"  # critical로 승격되지 않음


def test_critical_requires_both_static_and_ewma_after_warmup():
    b = Baseline()
    for _ in range(_MIN_SAMPLES):
        b.observe(0.01, 100.0)
    # 정적 임계값(0.05) 초과 + EWMA baseline(평소 0.01) 대비 급격 이탈 -> critical
    a = assess_anomaly(_snapshot(0.5, 100.0), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.error_static is True
    assert a.error_ewma is True
    assert a.severity == "critical"


def test_static_only_without_ewma_breach_is_warning_even_when_warm():
    b = Baseline()
    for _ in range(_MIN_SAMPLES):
        b.observe(0.049, 100.0)  # baseline이 이미 임계값 근처
    # 정적 임계값(0.05)은 살짝 넘지만 baseline 대비로는 이탈이 아님 -> warning
    a = assess_anomaly(_snapshot(0.06, 100.0), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.error_static is True
    assert a.severity in ("warning", "none")
    assert a.severity != "critical"


def test_latency_anomaly_independent_of_error_rate():
    b = Baseline()
    a = assess_anomaly(_snapshot(0.01, 2000.0), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.latency_static is True
    assert a.error_static is False
    assert a.any_anomaly is True


def test_none_metrics_are_skipped_not_treated_as_zero():
    """error_rate/p99가 None이면(스냅샷이 일부만 채워진 극단 상황) 해당 축은 평가하지 않는다."""
    b = Baseline()
    snap = TrafficSnapshot(status="ok", timestamp=1.0, window_seconds=60, rps=100.0, total_ready_pods=5)
    a = assess_anomaly(snap, b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.error_static is False
    assert a.latency_static is False
    assert a.severity == "none"


def test_culprit_backends_found_when_one_backend_breaches_and_others_healthy():
    per_backend = [
        BackendTraffic(name="a", rps=50, error_rate=0.5, p99_latency_ms=100, ready_pods=2),
        BackendTraffic(name="b", rps=50, error_rate=0.01, p99_latency_ms=100, ready_pods=3),
    ]
    b = Baseline()
    a = assess_anomaly(_snapshot(0.25, 100.0, per_backend), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.culprit_backends == ["a"]


def test_no_culprit_when_all_backends_breach_systemic_problem():
    per_backend = [
        BackendTraffic(name="a", rps=50, error_rate=0.5, p99_latency_ms=100, ready_pods=2),
        BackendTraffic(name="b", rps=50, error_rate=0.6, p99_latency_ms=100, ready_pods=3),
    ]
    b = Baseline()
    a = assess_anomaly(_snapshot(0.55, 100.0, per_backend), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.culprit_backends == []  # 전체 문제 -> 격리 후보 없음(증설로 대응해야 함)


def test_reason_is_never_empty_string():
    b = Baseline()
    a = assess_anomaly(_snapshot(0.01, 100.0), b, scale_up_error_rate=0.05, max_p99_latency_ms=800.0)
    assert a.reason  # "이상 없음" 등 항상 채워짐
