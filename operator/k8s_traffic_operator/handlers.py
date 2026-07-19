"""kopf 핸들러 - 오퍼레이터의 배선(wiring) 지점.

핵심 배선은 reconcile timer 안의 세 줄이다:

    snapshot = metrics.collect(spec)                     # metrics-collector-dev
    decision = policy.evaluate(spec, snapshot, status)   # policy-engine-dev
    result   = actuator.apply(spec, decision)            # actuator-dev

세 모듈 중 하나라도 완성되지 않아도 오퍼레이터가 뜨도록, 미완성 모듈은
안전한 폴백(stub)으로 대체한다. 실제 모듈이 생기면 아래 import가 그것을 우선 사용한다.
"""

from __future__ import annotations

import time

import kopf

from . import API_GROUP, API_VERSION, CRD_PLURAL
from .schemas import ActuationResult, Decision, TrafficSnapshot

# ---------------------------------------------------------------------------
# 모듈 import - 병렬 개발 중 미완성 모듈은 폴백 stub으로 대체한다.
# 각 팀의 모듈이 아래 함수 시그니처를 그대로 구현해야 배선이 맞는다:
#   metrics.collect(spec: dict) -> TrafficSnapshot
#   policy.evaluate(spec: dict, snapshot: TrafficSnapshot, status: dict) -> Decision
#   actuator.apply(spec: dict, decision: Decision) -> ActuationResult
# ---------------------------------------------------------------------------
try:
    from .metrics import collector as metrics          # metrics.collect(spec)
except Exception:  # noqa: BLE001 - 미완성/미존재 모듈 허용
    metrics = None

try:
    from .policy import engine as policy               # policy.evaluate(spec, snapshot, status)
except Exception:  # noqa: BLE001
    policy = None

try:
    from .actuator import executor as actuator         # actuator.apply(spec, decision)
except Exception:  # noqa: BLE001
    actuator = None


# ---------------------------------------------------------------------------
# 폴백 구현 - 실제 모듈이 없을 때 오퍼레이터가 크래시하지 않고 no-op으로 동작.
# ---------------------------------------------------------------------------
def _fallback_collect(spec: dict) -> TrafficSnapshot:
    return TrafficSnapshot(
        status="collection_failed",
        timestamp=time.time(),
        window_seconds=_parse_window_seconds(spec.get("window", "1m")),
        meta={"note": "metrics module not available (fallback)"},
    )


def _fallback_evaluate(spec: dict, snapshot: TrafficSnapshot, status: dict) -> Decision:
    return Decision(action="noop", reason="policy module not available (fallback)")


def _fallback_apply(spec: dict, decision: Decision) -> ActuationResult:
    return ActuationResult(
        applied=False,
        action=decision.action,
        detail="actuator module not available (fallback)",
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def _parse_window_seconds(window: str) -> int:
    """CRD spec.window("30s"/"1m"/"5m") -> 초. 파싱 실패 시 60초."""
    try:
        window = window.strip()
        if window.endswith("ms"):
            return max(1, int(float(window[:-2]) / 1000))
        if window.endswith("s"):
            return int(float(window[:-1]))
        if window.endswith("m"):
            return int(float(window[:-1]) * 60)
        if window.endswith("h"):
            return int(float(window[:-1]) * 3600)
        return int(float(window))
    except (ValueError, AttributeError):
        return 60


# ---------------------------------------------------------------------------
# CR 등록/변경 - 검증만 수행. 실제 reconcile은 timer가 담당.
# ---------------------------------------------------------------------------
@kopf.on.create(API_GROUP, API_VERSION, CRD_PLURAL)
@kopf.on.update(API_GROUP, API_VERSION, CRD_PLURAL)
def register_policy(spec, name, namespace, logger, **kwargs):
    """TrafficPolicy 등록/변경 시 스펙을 검증하고 초기 status를 남긴다."""
    target = spec.get("target", {})
    thresholds = spec.get("thresholds", {})
    actions = spec.get("actions", {})

    # 최소 검증 - 필수 대상/임계값이 없으면 kopf 이벤트로 경고를 남긴다.
    missing = []
    if not target.get("deployment"):
        missing.append("target.deployment")
    if not target.get("httpRoute"):
        missing.append("target.httpRoute")
    if thresholds.get("targetRPSPerPod") is None:
        missing.append("thresholds.targetRPSPerPod")
    if missing:
        raise kopf.PermanentError(f"TrafficPolicy spec 필수 필드 누락: {', '.join(missing)}")

    min_r = actions.get("minReplicas")
    max_r = actions.get("maxReplicas")
    if min_r is not None and max_r is not None and min_r > max_r:
        raise kopf.PermanentError(f"minReplicas({min_r}) > maxReplicas({max_r})")

    logger.info("TrafficPolicy '%s/%s' 등록됨. reconcile timer가 관리 시작.", namespace, name)
    return {
        "phase": "Registered",
        "registeredAt": time.time(),
        "windowSeconds": _parse_window_seconds(spec.get("window", "1m")),
    }


# ---------------------------------------------------------------------------
# 주기적 reconcile - 오퍼레이터의 심장. metrics -> policy -> actuator 순서 호출.
#
# interval(초)은 CRD spec.window 보다 짧아야 반응성이 확보된다.
# 여기서는 기본 30초. window 기반 동적 조정이 필요하면 후속 과제로 남긴다.
# ---------------------------------------------------------------------------
@kopf.timer(API_GROUP, API_VERSION, CRD_PLURAL, interval=30.0, sharp=True)
def reconcile(spec, status, name, namespace, logger, **kwargs):
    """트래픽을 관측하고 정책을 평가한 뒤 대응을 실행한다.

    반환값은 kopf가 CR status.reconcile 하위에 병합 기록한다.
    """
    spec = dict(spec)
    status = dict(status or {})

    collect = metrics.collect if metrics else _fallback_collect
    evaluate = policy.evaluate if policy else _fallback_evaluate
    apply = actuator.apply if actuator else _fallback_apply

    # --- 1) 트래픽 수집 (metrics-collector-dev) ---
    snapshot: TrafficSnapshot = collect(spec)

    # 데이터가 없거나 수집 실패면 정책 평가를 건너뛴다 (None 지표에 산술 금지).
    if snapshot.status != "ok":
        logger.info("[%s/%s] snapshot status=%s → 평가 생략", namespace, name, snapshot.status)
        return {
            "phase": snapshot.status,
            "lastSnapshotStatus": snapshot.status,
            "lastReconcileAt": time.time(),
        }

    # --- 2) 정책 평가 (policy-engine-dev). cooldown/hysteresis는 이 모듈이 책임. ---
    decision: Decision = evaluate(spec, snapshot, status)

    # --- 3) 대응 실행 (actuator-dev). 자체 안전장치를 한 번 더 적용. ---
    result: ActuationResult = apply(spec, decision)

    logger.info(
        "[%s/%s] action=%s applied=%s reason=%s detail=%s",
        namespace, name, decision.action, result.applied, decision.reason, result.detail,
    )

    return {
        "phase": "Reconciled",
        "lastReconcileAt": time.time(),
        "lastSnapshotStatus": snapshot.status,
        "lastDecision": decision.to_dict(),
        "lastActuation": result.to_dict(),
    }


# ---------------------------------------------------------------------------
# CR 삭제 - 관리 중이던 리소스 정리 훅(현재는 로깅만).
# ---------------------------------------------------------------------------
@kopf.on.delete(API_GROUP, API_VERSION, CRD_PLURAL)
def cleanup_policy(name, namespace, logger, **kwargs):
    logger.info("TrafficPolicy '%s/%s' 삭제됨. 관리 중단.", namespace, name)
