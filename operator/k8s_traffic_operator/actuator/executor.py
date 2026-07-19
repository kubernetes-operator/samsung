"""actuator 진입점 — Decision을 실제 클러스터 변경으로 실행한다.

handlers.py 배선:
    from .actuator import executor as actuator
    result = actuator.apply(spec, decision)   # reconcile timer 안에서 호출

apply()는 정책 엔진의 Decision을 소비해 Deployment 스케일(scaler.py) 또는
HTTPRoute weight(router.py)를 변경하고, 그 결과를 공유 계약 ActuationResult로
반환한다. 이 모듈은 프로덕션 트래픽에 직접 영향을 주므로 가장 방어적으로 동작한다:

  - action == "noop"            : 어떤 API 호출도 하지 않는다.
  - action == "scale"           : target_replicas 필수. min/max clamp + maxScaleStep 변경폭 제한.
  - action == "reroute"         : backend_weights 필수. weight 변경폭 제한.
  - action == "isolate_backend" : backend_weights 필수. allowRouteIsolation=false면 거부.
  - 알 수 없는 action           : 추측 실행 금지. failed로 기록.
  - 모든 실패는 예외를 던지지 않고 ActuationResult.error에 담아 반환(timer 안정성).

가정(assumptions) — schemas.py / 01_architect_design.md에 명시되지 않아 여기서 결정:

  A1. apply(spec, decision) 시그니처에는 CR namespace가 전달되지 않는다(handlers가
      namespace를 넘기지 않음). 따라서 대상 namespace는 spec.target.namespace를 쓰고,
      없으면 in-cluster ServiceAccount namespace 파일 -> "default" 순으로 폴백한다.
  A2. dry-run 플래그는 공유 계약에 없다. spec.actions.dryRun(bool)로 받고, 없으면
      환경변수 ACTUATOR_DRY_RUN(1/true/yes)로 받는다. 둘 다 없으면 실제 적용.
  A3. kubernetes client config는 in-cluster를 우선 로드하고, 실패 시 로컬 kubeconfig로
      폴백한다(개발/테스트 편의). config 로드 실패는 예외가 아니라 failed 결과로 보고.
  A4. isolate_backend는 reroute와 동일한 weight patch 경로를 쓰되 allowRouteIsolation
      게이트를 통과해야 한다(격리는 별도 허용 스위치를 요구하는 파괴적 동작으로 간주).
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from ..schemas import ActuationResult, Decision
from . import router, scaler
from ._result import ActionOutcome

log = logging.getLogger(__name__)

_SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

# k8s client는 프로세스 수명 동안 재사용(캐시). 실패 시 매 호출 재시도할 수 있도록 None 유지.
_apps_v1: Optional[client.AppsV1Api] = None
_custom_api: Optional[client.CustomObjectsApi] = None


def _load_clients() -> Tuple[client.AppsV1Api, client.CustomObjectsApi]:
    """kubernetes API 클라이언트를 (지연) 초기화하여 반환. 실패 시 ConfigException 전파."""
    global _apps_v1, _custom_api
    if _apps_v1 is not None and _custom_api is not None:
        return _apps_v1, _custom_api
    try:
        config.load_incluster_config()
    except ConfigException:
        # 개발/테스트 편의: 클러스터 밖이면 로컬 kubeconfig 사용.
        config.load_kube_config()
    _apps_v1 = client.AppsV1Api()
    _custom_api = client.CustomObjectsApi()
    return _apps_v1, _custom_api


def _resolve_namespace(target: dict) -> str:
    """대상 namespace 결정: spec.target.namespace -> SA namespace 파일 -> "default"."""
    ns = target.get("namespace")
    if ns:
        return ns
    try:
        with open(_SA_NAMESPACE_FILE, "r", encoding="utf-8") as fh:
            v = fh.read().strip()
            if v:
                return v
    except OSError:
        pass
    return "default"


def _is_dry_run(actions: dict) -> bool:
    """가정 A2: spec.actions.dryRun -> 환경변수 ACTUATOR_DRY_RUN 순으로 판정."""
    if "dryRun" in actions:
        return bool(actions.get("dryRun"))
    env = os.getenv("ACTUATOR_DRY_RUN", "").strip().lower()
    return env in ("1", "true", "yes", "on")


def _result(outcome: ActionOutcome, action: str, dry_run: bool) -> ActuationResult:
    """내부 ActionOutcome -> 대외 계약 ActuationResult 변환."""
    return ActuationResult(
        applied=outcome.applied,
        action=action,  # type: ignore[arg-type]  # ActionType Literal; noop/scale/reroute/isolate_backend만 도달.
        detail=outcome.detail,
        dry_run=dry_run,
        error=outcome.error,
    )


def apply(spec: dict, decision: Decision) -> ActuationResult:
    """Decision을 실행한다. 예외를 밖으로 던지지 않는다(항상 ActuationResult 반환)."""
    action = decision.action
    target = spec.get("target", {}) or {}
    actions = spec.get("actions", {}) or {}
    dry_run = _is_dry_run(actions)

    # --- noop: 어떤 API 호출도 하지 않는다 (관성적 재적용 금지). ---
    if action == "noop":
        return ActuationResult(
            applied=False, action="noop",
            detail=f"noop: {decision.reason}", dry_run=dry_run,
        )

    # --- 알 수 없는 action: 추측 실행 금지, 즉시 failed. (스키마 밖 값 방어) ---
    if action not in ("scale", "reroute", "isolate_backend"):
        msg = f"알 수 없는 action='{action}' — 처리하지 않음(추측 실행 금지)"
        log.error("[actuator] %s", msg)
        return ActuationResult(
            applied=False, action=action, detail="", dry_run=dry_run, error=msg,
        )

    # --- 여기서부터 실제 클러스터 접근이 필요하므로 client를 확보한다. ---
    try:
        apps_v1, custom_api = _load_clients()
    except Exception as exc:  # noqa: BLE001 - config 로드 실패도 결과로 보고(timer 안정성).
        log.error("[actuator] kube client 초기화 실패: %s", exc)
        return ActuationResult(
            applied=False, action=action, detail="",
            dry_run=dry_run, error=f"kube client 초기화 실패: {exc}",
        )

    namespace = _resolve_namespace(target)

    try:
        if action == "scale":
            return _do_scale(spec, decision, apps_v1, namespace, actions, dry_run)
        # reroute / isolate_backend
        return _do_reroute(decision, custom_api, namespace, target, actions, dry_run)
    except Exception as exc:  # noqa: BLE001 - 예상 못한 오류도 절대 밖으로 던지지 않는다.
        log.exception("[actuator] action=%s 실행 중 예외", action)
        return ActuationResult(
            applied=False, action=action, detail="",
            dry_run=dry_run, error=f"예상치 못한 실행 오류: {exc}",
        )


def _do_scale(
    spec: dict, decision: Decision, apps_v1: client.AppsV1Api,
    namespace: str, actions: dict, dry_run: bool,
) -> ActuationResult:
    deployment = (spec.get("target", {}) or {}).get("deployment")
    if not deployment:
        return ActuationResult(
            applied=False, action="scale", dry_run=dry_run,
            error="spec.target.deployment 누락 — scale 대상 없음",
        )
    if decision.target_replicas is None:
        # 스키마 규약: scale이면 target_replicas 필수. 위반은 정책 버그 신호이므로 거부.
        return ActuationResult(
            applied=False, action="scale", dry_run=dry_run,
            error="scale 액션인데 target_replicas가 None (Decision 규약 위반)",
        )

    outcome = scaler.scale_deployment(
        apps_v1, namespace, deployment, int(decision.target_replicas),
        min_replicas=actions.get("minReplicas"),
        max_replicas=actions.get("maxReplicas"),
        max_scale_step=actions.get("maxScaleStep"),
        dry_run=dry_run,
    )
    return _result(outcome, "scale", dry_run)


def _do_reroute(
    decision: Decision, custom_api: client.CustomObjectsApi,
    namespace: str, target: dict, actions: dict, dry_run: bool,
) -> ActuationResult:
    action = decision.action
    route_name = target.get("httpRoute")
    if not route_name:
        return ActuationResult(
            applied=False, action=action, dry_run=dry_run,
            error="spec.target.httpRoute 누락 — 라우팅 대상 없음",
        )
    if not decision.backend_weights:
        # 스키마 규약: reroute/isolate_backend면 backend_weights 필수.
        return ActuationResult(
            applied=False, action=action, dry_run=dry_run,
            error=f"{action} 액션인데 backend_weights가 비어있음 (Decision 규약 위반)",
        )

    # 가정 A4: 격리는 파괴적 동작이므로 allowRouteIsolation 게이트를 통과해야 한다.
    if action == "isolate_backend":
        allow = actions.get("allowRouteIsolation", False)
        if not allow:
            return ActuationResult(
                applied=False, action="isolate_backend", dry_run=dry_run,
                detail="allowRouteIsolation=false — 격리 거부",
                error=None,  # 정책상 거부는 실패가 아니다(applied=False, error 없음).
            )

    outcome = router.set_backend_weights(
        custom_api, namespace, route_name, dict(decision.backend_weights), dry_run=dry_run,
    )
    return _result(outcome, action, dry_run)
