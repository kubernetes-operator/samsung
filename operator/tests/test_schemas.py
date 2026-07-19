"""공유 계약(schemas.py) 자체의 회귀 테스트.

세 모듈이 이 파일을 계약으로 삼으므로, 필드 존재 여부와 to_dict() 직렬화가 깨지면
모든 경계면이 동시에 깨진다 — 가장 기초적이지만 가장 중요한 회귀 테스트다.
"""

from __future__ import annotations

from k8s_traffic_operator.schemas import ActuationResult, BackendTraffic, Decision, TrafficSnapshot


def test_traffic_snapshot_defaults_are_none_not_zero():
    """결측치가 0으로 위장되지 않는다는 원칙이 기본값에서부터 지켜지는지 확인."""
    snap = TrafficSnapshot(status="no_data", timestamp=1.0, window_seconds=60)
    assert snap.rps is None
    assert snap.error_rate is None
    assert snap.p50_latency_ms is None
    assert snap.p95_latency_ms is None
    assert snap.p99_latency_ms is None
    assert snap.total_ready_pods is None
    assert snap.per_backend == []


def test_traffic_snapshot_to_dict_round_trip():
    snap = TrafficSnapshot(
        status="ok",
        timestamp=1.0,
        window_seconds=60,
        rps=10.0,
        error_rate=0.02,
        per_backend=[BackendTraffic(name="a", rps=10.0, error_rate=0.02, p99_latency_ms=100.0, ready_pods=2)],
    )
    d = snap.to_dict()
    assert d["status"] == "ok"
    assert d["per_backend"][0]["name"] == "a"


def test_decision_defaults():
    d = Decision(action="noop", reason="정상")
    assert d.target_replicas is None
    assert d.backend_weights is None
    assert d.severity == "none"
    assert d.cooldown_until is None


def test_decision_to_dict_preserves_action_and_weights():
    d = Decision(action="isolate_backend", reason="격리", backend_weights={"a": 0, "b": 100})
    payload = d.to_dict()
    assert payload["action"] == "isolate_backend"
    assert payload["backend_weights"] == {"a": 0, "b": 100}


def test_actuation_result_defaults():
    r = ActuationResult(applied=False, action="noop")
    assert r.detail == ""
    assert r.dry_run is False
    assert r.error is None
