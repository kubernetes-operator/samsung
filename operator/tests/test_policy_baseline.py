"""baseline.py 단위 테스트 - EWMA _Stat, warmup, target_key 격리."""

from __future__ import annotations

from k8s_traffic_operator.policy.baseline import Baseline, _MIN_SAMPLES, get_baseline, reset, target_key


def test_baseline_not_warm_before_min_samples():
    b = Baseline()
    for _ in range(_MIN_SAMPLES - 1):
        b.observe(0.01, 100.0)
    assert b.warm is False


def test_baseline_warm_after_min_samples():
    b = Baseline()
    for _ in range(_MIN_SAMPLES):
        b.observe(0.01, 100.0)
    assert b.warm is True


def test_baseline_ignores_none_observations():
    b = Baseline()
    b.observe(None, None)
    assert b.error_rate.count == 0
    assert b.p99_latency_ms.count == 0


def test_upper_breach_detects_spike_above_flat_history():
    b = Baseline()
    for _ in range(_MIN_SAMPLES):
        b.observe(0.01, 100.0)
    # 평탄한 이력(분산 0에 가까움) 이후 급격한 스파이크 -> 상대 마진 경로로 이탈 판정.
    assert b.error_rate.is_upper_breach(0.5) is True
    assert b.error_rate.is_upper_breach(0.011) is False


def test_upper_zscore_is_zero_or_positive_never_negative_for_below_mean():
    b = Baseline()
    for _ in range(_MIN_SAMPLES):
        b.observe(0.05, 100.0)
    assert b.error_rate.upper_zscore(0.01) == 0.0  # 평소 이하 -> 0으로 클램프


def test_target_key_isolates_different_targets():
    spec_a = {"target": {"namespace": "shop", "deployment": "checkout", "httpRoute": "checkout-route"}}
    spec_b = {"target": {"namespace": "shop", "deployment": "cart", "httpRoute": "cart-route"}}
    assert target_key(spec_a) != target_key(spec_b)


def test_get_baseline_returns_same_instance_for_same_target():
    reset()
    spec = {"target": {"namespace": "shop", "deployment": "checkout", "httpRoute": "checkout-route"}}
    b1 = get_baseline(spec)
    b1.observe(0.5, 500.0)
    b2 = get_baseline(spec)
    assert b2 is b1
    assert b2.error_rate.count == 1
    reset()


def test_get_baseline_returns_different_instance_for_different_target():
    reset()
    spec_a = {"target": {"namespace": "shop", "deployment": "checkout", "httpRoute": "checkout-route"}}
    spec_b = {"target": {"namespace": "shop", "deployment": "cart", "httpRoute": "cart-route"}}
    ba = get_baseline(spec_a)
    ba.observe(0.9, 900.0)
    bb = get_baseline(spec_b)
    assert bb.error_rate.count == 0
    reset()
