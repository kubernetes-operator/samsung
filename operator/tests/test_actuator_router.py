"""actuator/router.py 단위 테스트 - weight clamp, idempotency, 409 재시도, v1beta1 폴백."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from k8s_traffic_operator.actuator.router import (
    MAX_WEIGHT_DELTA_PER_RECONCILE,
    clamp_weight_change,
    set_backend_weights,
)


def _route(weights: dict) -> dict:
    return {
        "spec": {
            "rules": [
                {"backendRefs": [{"name": name, "weight": w} for name, w in weights.items()]}
            ]
        }
    }


def _custom_api_returning(route: dict, *, v1_status: int | None = 200) -> MagicMock:
    api = MagicMock()

    def get_namespaced_custom_object(**kwargs):
        if v1_status != 200 and kwargs["version"] == "v1":
            raise ApiException(status=v1_status, reason="Not Found")
        return copy.deepcopy(route)

    api.get_namespaced_custom_object.side_effect = get_namespaced_custom_object
    return api


# --------------------------------------------------------------------------- clamp_weight_change
def test_clamp_weight_change_limits_delta():
    assert clamp_weight_change(0, 100) == MAX_WEIGHT_DELTA_PER_RECONCILE
    assert clamp_weight_change(100, 0) == 100 - MAX_WEIGHT_DELTA_PER_RECONCILE


def test_clamp_weight_change_within_step_applies_fully():
    assert clamp_weight_change(90, 100) == 100
    assert clamp_weight_change(10, 0) == 0


def test_clamp_weight_change_bounds_0_to_100():
    assert clamp_weight_change(0, -50) == 0
    assert clamp_weight_change(100, 500) == 100


# --------------------------------------------------------------------------- set_backend_weights
def test_idempotent_skip_when_already_at_clamped_target():
    api = _custom_api_returning(_route({"a": 100, "b": 100}))
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 100})
    assert outcome.status == "skipped"
    api.replace_namespaced_custom_object.assert_not_called()


def test_applies_clamped_weight_change():
    api = _custom_api_returning(_route({"a": 0, "b": 100}))
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 100})
    assert outcome.status == "applied"
    _, kwargs = api.replace_namespaced_custom_object.call_args
    patched = kwargs["body"]
    a_ref = next(r for r in patched["spec"]["rules"][0]["backendRefs"] if r["name"] == "a")
    assert a_ref["weight"] == MAX_WEIGHT_DELTA_PER_RECONCILE  # 한 reconcile당 변경폭 제한


def test_dry_run_does_not_call_replace():
    api = _custom_api_returning(_route({"a": 0, "b": 100}))
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 100}, dry_run=True)
    assert outcome.status == "skipped"
    assert "dry-run" in outcome.detail
    api.replace_namespaced_custom_object.assert_not_called()


def test_rejects_out_of_range_weight_without_calling_api():
    api = _custom_api_returning(_route({"a": 0, "b": 100}))
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 150})
    assert outcome.status == "failed"
    assert "범위 밖" in outcome.error
    api.get_namespaced_custom_object.assert_not_called()


def test_unmatched_backend_reports_failed():
    api = _custom_api_returning(_route({"a": 0, "b": 100}))
    outcome = set_backend_weights(api, "shop", "checkout-route", {"does-not-exist": 100})
    assert outcome.status == "failed"
    assert "route에 없음" in outcome.error


def test_conflict_retried_once_then_succeeds():
    api = _custom_api_returning(_route({"a": 0, "b": 100}))
    api.replace_namespaced_custom_object.side_effect = [
        ApiException(status=409, reason="Conflict"),
        None,  # 재시도 성공
    ]
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 100})
    assert outcome.status == "applied"
    assert api.replace_namespaced_custom_object.call_count == 2


def test_conflict_exhausts_retry_and_fails():
    api = _custom_api_returning(_route({"a": 0, "b": 100}))
    api.replace_namespaced_custom_object.side_effect = ApiException(status=409, reason="Conflict")
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 100})
    assert outcome.status == "failed"
    assert "409" in outcome.error


def test_falls_back_to_v1beta1_when_v1_not_found():
    api = _custom_api_returning(_route({"a": 0, "b": 100}), v1_status=404)
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 100})
    assert outcome.status == "applied"
    _, kwargs = api.replace_namespaced_custom_object.call_args
    assert kwargs["version"] == "v1beta1"


def test_non_404_error_does_not_attempt_version_fallback():
    api = _custom_api_returning(_route({"a": 0, "b": 100}), v1_status=403)
    outcome = set_backend_weights(api, "shop", "checkout-route", {"a": 100})
    assert outcome.status == "failed"
    api.replace_namespaced_custom_object.assert_not_called()
