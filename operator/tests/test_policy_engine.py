"""engine.py 통합 테스트 - Decision 우선순위, cooldown, isolate_backend<->reroute 복구.

이 파일의 recovery(reroute) 관련 테스트는 실제로 구현 중 발견했던 두 가지 버그의
회귀 방지용이다:
  1. reroute의 backend_weights가 항상 "목표값"(100)이라서 weight<100 여부만으로
     다음 cycle의 복구 지속 여부를 판단하면 1 cycle 만에 멈추는 버그
     (-> test_recovery_reroute_continues_across_multiple_cycles).
  2. cooldownSeconds>0일 때 cooldown-noop 사이클이 backend_weights를 보존하지 않아
     cooldown이 풀린 뒤 복구 이력 자체가 유실되는 버그
     (-> test_recovery_survives_cooldown_gap_after_isolation).
"""

from __future__ import annotations

from k8s_traffic_operator.policy import engine
from tests.conftest import make_snapshot, make_spec


def _run(spec, snapshot, status):
    """evaluate() 호출 + 다음 cycle에 넘길 status를 함께 반환하는 헬퍼."""
    decision = engine.evaluate(spec, snapshot, status)
    next_status = {"reconcile": {"lastDecision": decision.to_dict(), "lastReconcileAt": snapshot.timestamp}}
    return decision, next_status


# --------------------------------------------------------------------------- (A) 결측치 방어
def test_non_ok_snapshot_always_noop():
    spec = make_spec()
    snap = make_snapshot(status="no_data")
    decision = engine.evaluate(spec, snap, {})
    assert decision.action == "noop"
    assert decision.target_replicas is None
    assert decision.backend_weights is None


def test_collection_failed_snapshot_always_noop():
    spec = make_spec()
    snap = make_snapshot(status="collection_failed")
    decision = engine.evaluate(spec, snap, {})
    assert decision.action == "noop"


# --------------------------------------------------------------------------- (C) cooldown
def test_cooldown_blocks_action_via_cooldown_until_field():
    spec = make_spec(cooldown_seconds=120)
    snap = make_snapshot(rps=100.0, error_rate=0.01)
    status = {"reconcile": {"lastDecision": {
        "action": "scale", "reason": "prev", "target_replicas": 6, "cooldown_until": snap.timestamp + 60,
    }}}
    decision = engine.evaluate(spec, snap, status)
    assert decision.action == "noop"
    assert "cooldown" in decision.reason


def test_cooldown_expired_allows_new_action():
    spec = make_spec(cooldown_seconds=120, target_rps_per_pod=10.0, scale_down_rps_per_pod=5.0)
    # rps/pod 를 크게 올려 스케일업 조건을 확실히 만족시킨다.
    snap = make_snapshot(rps=1000.0, error_rate=0.01, total_ready_pods=5)
    status = {"reconcile": {"lastDecision": {
        "action": "scale", "reason": "prev", "target_replicas": 6, "cooldown_until": snap.timestamp - 1,
    }}}
    decision = engine.evaluate(spec, snap, status)
    assert decision.action == "scale"


def test_cooldown_fallback_path_blocks_when_no_cooldown_until_recorded():
    """cooldown_until이 없어도(구버전 상태 등) lastReconcileAt+cooldownSeconds 폴백으로 억제."""
    spec = make_spec(cooldown_seconds=120)
    snap = make_snapshot(rps=100.0, error_rate=0.01, timestamp=1030.0)
    status = {"reconcile": {
        "lastDecision": {"action": "scale", "reason": "prev", "target_replicas": 6},
        "lastReconcileAt": 1000.0,  # 30초 전 -> 120s cooldown 아직 안 끝남
    }}
    decision = engine.evaluate(spec, snap, status)
    assert decision.action == "noop"
    assert "cooldown" in decision.reason


def test_cooldown_fallback_path_does_not_gate_reroute():
    """reroute는 cooldown 폴백 대상에서 제외된다 — 복구 시퀀스가 cooldown에 막히지 않아야 한다."""
    spec = make_spec(cooldown_seconds=120)
    snap = make_snapshot(rps=100.0, error_rate=0.01, timestamp=1030.0)
    status = {"reconcile": {
        "lastDecision": {
            "action": "reroute", "reason": "이전 복구 [recovery done=1 total=4 backends=a]",
            "backend_weights": {"a": 100},
        },
        "lastReconcileAt": 1000.0,
    }}
    decision = engine.evaluate(spec, snap, status)
    assert decision.action != "noop" or "cooldown" not in decision.reason


# --------------------------------------------------------------------------- isolate_backend
def test_isolate_backend_on_error_anomaly_with_culprit_and_allowed():
    spec = make_spec(allow_route_isolation=True)
    from k8s_traffic_operator.schemas import BackendTraffic
    per_backend = [
        BackendTraffic(name="a", rps=50, error_rate=0.5, p99_latency_ms=100, ready_pods=2),
        BackendTraffic(name="b", rps=50, error_rate=0.01, p99_latency_ms=100, ready_pods=3),
    ]
    snap = make_snapshot(rps=100.0, error_rate=0.25, per_backend=per_backend)
    decision = engine.evaluate(spec, snap, {})
    assert decision.action == "isolate_backend"
    assert decision.backend_weights["a"] < decision.backend_weights["b"]


def test_isolate_backend_denied_falls_back_to_scale_when_allow_isolation_false():
    from k8s_traffic_operator.schemas import BackendTraffic
    spec = make_spec(allow_route_isolation=False, target_rps_per_pod=10.0)
    per_backend = [
        BackendTraffic(name="a", rps=50, error_rate=0.5, p99_latency_ms=100, ready_pods=2),
        BackendTraffic(name="b", rps=50, error_rate=0.01, p99_latency_ms=100, ready_pods=3),
    ]
    snap = make_snapshot(rps=100.0, error_rate=0.25, per_backend=per_backend, total_ready_pods=5)
    decision = engine.evaluate(spec, snap, {})
    assert decision.action != "isolate_backend"
    assert "격리 불가" in decision.reason or decision.action == "scale"


def test_systemic_anomaly_scales_up_when_all_backends_breach():
    from k8s_traffic_operator.schemas import BackendTraffic
    spec = make_spec(target_rps_per_pod=10.0)
    per_backend = [
        BackendTraffic(name="a", rps=50, error_rate=0.5, p99_latency_ms=100, ready_pods=2),
        BackendTraffic(name="b", rps=50, error_rate=0.6, p99_latency_ms=100, ready_pods=3),
    ]
    snap = make_snapshot(rps=100.0, error_rate=0.55, per_backend=per_backend, total_ready_pods=5)
    decision = engine.evaluate(spec, snap, {})
    assert decision.action == "scale"
    assert decision.target_replicas is not None and decision.target_replicas > 5


# --------------------------------------------------------------------------- hysteresis scale
def test_hysteresis_scale_up_when_healthy_but_over_capacity():
    spec = make_spec(target_rps_per_pod=10.0, scale_down_rps_per_pod=5.0)
    snap = make_snapshot(rps=1000.0, error_rate=0.01, total_ready_pods=5)
    decision = engine.evaluate(spec, snap, {})
    assert decision.action == "scale"
    assert decision.severity == "none"


def test_hysteresis_band_holds_steady_when_normal():
    spec = make_spec(target_rps_per_pod=50.0, scale_down_rps_per_pod=20.0)
    snap = make_snapshot(rps=150.0, error_rate=0.01, total_ready_pods=5)  # 30/pod: 밴드 내부
    decision = engine.evaluate(spec, snap, {})
    assert decision.action == "noop"


# --------------------------------------------------------------------------- reroute 복구 (핵심 회귀 테스트)
def test_recovery_reroute_continues_across_multiple_cycles():
    """isolate_backend(weight 0) 이후 anomaly가 사라지면 reroute가 여러 cycle에 걸쳐
    점진적으로 weight를 100까지 복구하고, 수렴 후에는 정확히 noop으로 정착해야 한다."""
    spec = make_spec(cooldown_seconds=0)
    healthy = make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5)
    status = {"reconcile": {"lastDecision": {
        "action": "isolate_backend", "reason": "prev isolate",
        "backend_weights": {"a": 0, "b": 100},
    }}}

    actions = []
    for i in range(6):
        snap = make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5, timestamp=float(i))
        decision, status = _run(spec, snap, status)
        actions.append(decision.action)

    # MAX_WEIGHT_DELTA_PER_RECONCILE=30 가정 하 ceil(100/30)=4번의 reroute 후 noop으로 정착.
    assert actions == ["reroute", "reroute", "reroute", "reroute", "noop", "noop"]


def test_recovery_reroute_restores_weight_to_healthy_target():
    spec = make_spec(cooldown_seconds=0)
    healthy = make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5)
    status = {"reconcile": {"lastDecision": {
        "action": "isolate_backend", "reason": "prev isolate",
        "backend_weights": {"a": 0, "b": 100},
    }}}
    decision = engine.evaluate(spec, healthy, status)
    assert decision.action == "reroute"
    assert decision.backend_weights == {"a": 100}
    assert "recovery done=1 total=4" in decision.reason


def test_recovery_does_not_repeat_forever_after_convergence():
    """수렴(cycles_needed 도달) 후에는 다시 isolate/reroute가 재발동하지 않아야 한다."""
    spec = make_spec(cooldown_seconds=0)
    status = {"reconcile": {"lastDecision": {
        "action": "isolate_backend", "reason": "prev isolate",
        "backend_weights": {"a": 0, "b": 100},
    }}}
    for i in range(4):
        snap = make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5, timestamp=float(i))
        decision, status = _run(spec, snap, status)
        assert decision.action == "reroute"

    # 5번째, 6번째 cycle: 더 이상 reroute가 나오면 안 된다(수렴 완료).
    for i in range(4, 6):
        snap = make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5, timestamp=float(i))
        decision, status = _run(spec, snap, status)
        assert decision.action != "reroute"
        assert decision.action != "isolate_backend"


def test_recovery_survives_cooldown_gap_after_isolation():
    """cooldownSeconds>0 인 실제 설정에서, isolate_backend 직후 cooldown이 여러 cycle 지속돼도
    cooldown이 풀린 뒤 복구(reroute)가 정상적으로 시작되어야 한다(버그#2 회귀 방지)."""
    spec = make_spec(cooldown_seconds=120)
    status = {"reconcile": {"lastDecision": {
        "action": "isolate_backend", "reason": "prev isolate",
        "backend_weights": {"a": 0, "b": 100},
        "cooldown_until": 120.0,
    }}}

    t = 0
    actions = []
    for _ in range(7):
        t += 30
        snap = make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5, timestamp=float(t))
        decision, status = _run(spec, snap, status)
        actions.append(decision.action)

    # t=30,60,90: cooldown 활성(noop). t=120부터 복구 4 cycle(reroute). 이후 noop.
    assert actions[:3] == ["noop", "noop", "noop"]
    assert actions[3:7] == ["reroute", "reroute", "reroute", "reroute"]


def test_recovery_interrupted_by_new_anomaly_reissues_isolation():
    """복구 도중 이상이 재발하면 즉시 isolate_backend로 되돌아가야 하고(안전 우선),
    이상이 다시 해소되면 새 시작 weight 기준으로 복구가 재계산되어야 한다."""
    from k8s_traffic_operator.schemas import BackendTraffic
    spec = make_spec(cooldown_seconds=0)
    status = {"reconcile": {"lastDecision": {
        "action": "isolate_backend", "reason": "prev isolate",
        "backend_weights": {"a": 0, "b": 100},
    }}}

    def healthy_snap(ts):
        return make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5, timestamp=ts)

    def bad_snap(ts):
        per_backend = [
            BackendTraffic(name="a", rps=50, error_rate=0.40, p99_latency_ms=200, ready_pods=2),
            BackendTraffic(name="b", rps=50, error_rate=0.01, p99_latency_ms=200, ready_pods=3),
        ]
        return make_snapshot(rps=100.0, error_rate=0.20, per_backend=per_backend, total_ready_pods=5, timestamp=ts)

    d0, status = _run(spec, healthy_snap(0.0), status)
    assert d0.action == "reroute"

    d1, status = _run(spec, bad_snap(1.0), status)
    assert d1.action == "isolate_backend"  # 이상 재발 -> 즉시 재격리

    d2, status = _run(spec, healthy_snap(2.0), status)
    assert d2.action == "reroute"  # 새 격리 기준으로 복구 재시작
    assert "1/" in d2.reason


def test_recovery_reason_carries_reroute_across_status_shapes():
    """status에 lastDecision이 최상위(reconcile 래핑 없이)로 온 구버전 배선도 처리되어야 한다."""
    spec = make_spec(cooldown_seconds=0)
    status = {"lastDecision": {
        "action": "isolate_backend", "reason": "prev isolate",
        "backend_weights": {"a": 20, "b": 100},
    }}
    snap = make_snapshot(rps=100.0, error_rate=0.01, total_ready_pods=5)
    decision = engine.evaluate(spec, snap, status)
    assert decision.action == "reroute"
