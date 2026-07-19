"""actuator/executor.py 단위 테스트 - Decision -> 실제 실행 분기(dispatch)와 안전장치.

scaler.scale_deployment / router.set_backend_weights 자체는 test_actuator_scaler.py /
test_actuator_router.py에서 이미 검증했으므로, 여기서는 executor가 이들을 "올바른 인자로,
올바른 조건에서만" 호출하는지에 집중한다 (noop이 API를 안 부르는지, 알 수 없는 action을
추측 실행하지 않는지, allowRouteIsolation 게이트가 isolate_backend에만 적용되는지 등).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from k8s_traffic_operator.actuator import executor
from k8s_traffic_operator.actuator._result import ActionOutcome
from k8s_traffic_operator.schemas import Decision


@pytest.fixture(autouse=True)
def _fake_clients(monkeypatch):
    """_load_clients()가 실제 kubeconfig를 읽지 않도록 항상 mock 클라이언트를 반환하게 한다."""
    apps_v1, custom_api = MagicMock(), MagicMock()
    monkeypatch.setattr(executor, "_load_clients", lambda: (apps_v1, custom_api))
    return apps_v1, custom_api


def _spec(**overrides):
    spec = {
        "target": {"httpRoute": "checkout-route", "namespace": "shop", "deployment": "checkout-service"},
        "actions": {"minReplicas": 2, "maxReplicas": 20, "maxScaleStep": 4, "allowRouteIsolation": True},
    }
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------- noop
def test_noop_never_touches_kubernetes_client(monkeypatch):
    scale_mock = MagicMock()
    monkeypatch.setattr(executor.scaler, "scale_deployment", scale_mock)
    result = executor.apply(_spec(), Decision(action="noop", reason="정상"))
    assert result.applied is False
    assert result.action == "noop"
    scale_mock.assert_not_called()


# --------------------------------------------------------------------------- 알 수 없는 action
def test_unknown_action_is_rejected_not_guessed(monkeypatch):
    load_clients_mock = MagicMock()
    monkeypatch.setattr(executor, "_load_clients", load_clients_mock)
    result = executor.apply(_spec(), Decision(action="reboot_cluster", reason="???"))
    assert result.applied is False
    assert result.error is not None
    assert "알 수 없는 action" in result.error
    load_clients_mock.assert_not_called()  # 클라이언트 접근조차 하지 않아야 한다.


# --------------------------------------------------------------------------- scale
def test_scale_missing_deployment_target_fails_without_calling_scaler(monkeypatch):
    scale_mock = MagicMock()
    monkeypatch.setattr(executor.scaler, "scale_deployment", scale_mock)
    spec = _spec(target={"httpRoute": "r"})  # deployment 없음
    result = executor.apply(spec, Decision(action="scale", reason="", target_replicas=6))
    assert result.applied is False
    assert "target.deployment" in result.error
    scale_mock.assert_not_called()


def test_scale_missing_target_replicas_is_rejected_as_contract_violation(monkeypatch):
    scale_mock = MagicMock()
    monkeypatch.setattr(executor.scaler, "scale_deployment", scale_mock)
    result = executor.apply(_spec(), Decision(action="scale", reason="", target_replicas=None))
    assert result.applied is False
    assert "target_replicas" in result.error
    scale_mock.assert_not_called()


def test_scale_dispatches_to_scaler_with_actions_params(monkeypatch, _fake_clients):
    apps_v1, _ = _fake_clients
    scale_mock = MagicMock(return_value=ActionOutcome("applied", detail="replicas 5 -> 8"))
    monkeypatch.setattr(executor.scaler, "scale_deployment", scale_mock)

    result = executor.apply(_spec(), Decision(action="scale", reason="", target_replicas=8))

    assert result.applied is True
    assert result.action == "scale"
    args, kwargs = scale_mock.call_args
    assert args[0] is apps_v1
    assert args[1] == "shop"
    assert args[2] == "checkout-service"
    assert args[3] == 8
    assert kwargs["min_replicas"] == 2
    assert kwargs["max_replicas"] == 20
    assert kwargs["max_scale_step"] == 4


# --------------------------------------------------------------------------- reroute / isolate_backend
def test_reroute_missing_http_route_fails_without_calling_router(monkeypatch):
    router_mock = MagicMock()
    monkeypatch.setattr(executor.router, "set_backend_weights", router_mock)
    spec = _spec(target={"deployment": "d"})  # httpRoute 없음
    result = executor.apply(spec, Decision(action="reroute", reason="", backend_weights={"a": 100}))
    assert result.applied is False
    assert "target.httpRoute" in result.error
    router_mock.assert_not_called()


def test_reroute_missing_backend_weights_is_rejected_as_contract_violation(monkeypatch):
    router_mock = MagicMock()
    monkeypatch.setattr(executor.router, "set_backend_weights", router_mock)
    result = executor.apply(_spec(), Decision(action="reroute", reason="", backend_weights=None))
    assert result.applied is False
    assert "backend_weights" in result.error
    router_mock.assert_not_called()


def test_reroute_dispatches_to_router_without_allow_isolation_gate(monkeypatch, _fake_clients):
    """reroute는 isolate_backend와 달리 allowRouteIsolation 게이트를 적용받지 않는다."""
    _, custom_api = _fake_clients
    router_mock = MagicMock(return_value=ActionOutcome("applied", detail="a 20->100"))
    monkeypatch.setattr(executor.router, "set_backend_weights", router_mock)

    spec = _spec(actions={"allowRouteIsolation": False})  # 격리는 금지되어 있어도
    result = executor.apply(spec, Decision(action="reroute", reason="", backend_weights={"a": 100}))

    assert result.applied is True
    router_mock.assert_called_once()
    args, _ = router_mock.call_args
    assert args[0] is custom_api


def test_isolate_backend_denied_when_allow_route_isolation_false(monkeypatch):
    router_mock = MagicMock()
    monkeypatch.setattr(executor.router, "set_backend_weights", router_mock)
    spec = _spec(actions={"allowRouteIsolation": False})
    result = executor.apply(spec, Decision(action="isolate_backend", reason="", backend_weights={"a": 0, "b": 100}))
    assert result.applied is False
    assert result.error is None  # 정책상 거부는 실패가 아니라 명시적 스킵
    assert "allowRouteIsolation=false" in result.detail
    router_mock.assert_not_called()


def test_isolate_backend_dispatches_when_allowed(monkeypatch, _fake_clients):
    router_mock = MagicMock(return_value=ActionOutcome("applied", detail="a 100->0"))
    monkeypatch.setattr(executor.router, "set_backend_weights", router_mock)
    result = executor.apply(_spec(), Decision(action="isolate_backend", reason="", backend_weights={"a": 0, "b": 100}))
    assert result.applied is True
    router_mock.assert_called_once()


# --------------------------------------------------------------------------- 에러 격리 (예외를 밖으로 던지지 않음)
def test_client_load_failure_is_reported_not_raised(monkeypatch):
    def boom():
        raise RuntimeError("kubeconfig 없음")

    monkeypatch.setattr(executor, "_load_clients", boom)
    result = executor.apply(_spec(), Decision(action="scale", reason="", target_replicas=8))
    assert result.applied is False
    assert "초기화 실패" in result.error


def test_unexpected_exception_inside_dispatch_is_caught(monkeypatch, _fake_clients):
    def boom(*a, **kw):
        raise RuntimeError("무언가 터짐")

    monkeypatch.setattr(executor.scaler, "scale_deployment", boom)
    result = executor.apply(_spec(), Decision(action="scale", reason="", target_replicas=8))
    assert result.applied is False
    assert result.error is not None


# --------------------------------------------------------------------------- dry-run / namespace 해석
def test_dry_run_from_spec_actions_flag_is_forwarded(monkeypatch, _fake_clients):
    scale_mock = MagicMock(return_value=ActionOutcome("skipped", detail="[dry-run] ..."))
    monkeypatch.setattr(executor.scaler, "scale_deployment", scale_mock)
    spec = _spec(actions={"dryRun": True, "minReplicas": 2, "maxReplicas": 20})
    result = executor.apply(spec, Decision(action="scale", reason="", target_replicas=8))
    assert result.dry_run is True
    _, kwargs = scale_mock.call_args
    assert kwargs["dry_run"] is True


def test_namespace_resolved_from_target_when_present(monkeypatch, _fake_clients):
    scale_mock = MagicMock(return_value=ActionOutcome("applied", detail=""))
    monkeypatch.setattr(executor.scaler, "scale_deployment", scale_mock)
    executor.apply(_spec(), Decision(action="scale", reason="", target_replicas=8))
    args, _ = scale_mock.call_args
    assert args[1] == "shop"
