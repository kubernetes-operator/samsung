"""actuator/scaler.py 단위 테스트 - clamp 로직 + scale_deployment (kubernetes client는 mock)."""

from __future__ import annotations

from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from k8s_traffic_operator.actuator.scaler import clamp_replicas, scale_deployment


# --------------------------------------------------------------------------- clamp_replicas
def test_clamp_replicas_applies_min_max():
    assert clamp_replicas(target=1, current=5, min_replicas=2, max_replicas=20, max_scale_step=None) == 2
    assert clamp_replicas(target=100, current=5, min_replicas=2, max_replicas=20, max_scale_step=None) == 20


def test_clamp_replicas_applies_max_scale_step():
    # 목표 20, 현재 5, 최대 변화폭 4 -> 이번엔 9까지만.
    assert clamp_replicas(target=20, current=5, min_replicas=2, max_replicas=20, max_scale_step=4) == 9


def test_clamp_replicas_never_negative():
    assert clamp_replicas(target=-10, current=0, min_replicas=None, max_replicas=None, max_scale_step=None) == 0


def test_clamp_replicas_min_max_applied_before_scale_step():
    # target=1 이지만 min_replicas=5 이므로 먼저 5로 clamp된 뒤, 변화폭 제한(step=2)이 적용되어
    # 현재(1)+2=3까지만 이번 reconcile에서 이동한다(5로 한 번에 점프하지 않음).
    assert clamp_replicas(target=1, current=1, min_replicas=5, max_replicas=20, max_scale_step=2) == 3


# --------------------------------------------------------------------------- scale_deployment
def _apps_v1_with_current_replicas(n: int) -> MagicMock:
    apps_v1 = MagicMock()
    apps_v1.read_namespaced_deployment_scale.return_value.spec.replicas = n
    return apps_v1


def test_scale_deployment_idempotent_skip_when_already_at_target():
    apps_v1 = _apps_v1_with_current_replicas(5)
    outcome = scale_deployment(apps_v1, "shop", "checkout-service", target_replicas=5, min_replicas=2, max_replicas=20)
    assert outcome.status == "skipped"
    apps_v1.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_deployment_applies_patch_when_target_differs():
    apps_v1 = _apps_v1_with_current_replicas(5)
    outcome = scale_deployment(apps_v1, "shop", "checkout-service", target_replicas=8, min_replicas=2, max_replicas=20)
    assert outcome.status == "applied"
    apps_v1.patch_namespaced_deployment_scale.assert_called_once()
    _, kwargs = apps_v1.patch_namespaced_deployment_scale.call_args
    assert kwargs["body"] == {"spec": {"replicas": 8}}


def test_scale_deployment_dry_run_does_not_call_patch():
    apps_v1 = _apps_v1_with_current_replicas(5)
    outcome = scale_deployment(apps_v1, "shop", "checkout-service", target_replicas=8, dry_run=True)
    assert outcome.status == "skipped"
    assert "dry-run" in outcome.detail
    apps_v1.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_deployment_read_failure_reports_failed_without_raising():
    apps_v1 = MagicMock()
    apps_v1.read_namespaced_deployment_scale.side_effect = ApiException(status=403, reason="Forbidden")
    outcome = scale_deployment(apps_v1, "shop", "checkout-service", target_replicas=8)
    assert outcome.status == "failed"
    assert outcome.error is not None
    apps_v1.patch_namespaced_deployment_scale.assert_not_called()


def test_scale_deployment_patch_failure_reports_failed_without_raising():
    apps_v1 = _apps_v1_with_current_replicas(5)
    apps_v1.patch_namespaced_deployment_scale.side_effect = ApiException(status=500, reason="Internal Error")
    outcome = scale_deployment(apps_v1, "shop", "checkout-service", target_replicas=8)
    assert outcome.status == "failed"
    assert outcome.error is not None


def test_scale_deployment_respects_max_scale_step_across_call():
    apps_v1 = _apps_v1_with_current_replicas(5)
    outcome = scale_deployment(
        apps_v1, "shop", "checkout-service", target_replicas=20,
        min_replicas=2, max_replicas=20, max_scale_step=4,
    )
    assert outcome.status == "applied"
    _, kwargs = apps_v1.patch_namespaced_deployment_scale.call_args
    assert kwargs["body"]["spec"]["replicas"] == 9  # 5 + 4
