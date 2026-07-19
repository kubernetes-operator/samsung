"""정책 엔진 진입점 - handlers.reconcile이 호출한다.

    from .policy import engine as policy
    decision = policy.evaluate(spec, snapshot, status)

책임:
  1. non-ok 스냅샷 방어(noop).
  2. cooldown 강제(CRD actions.cooldownSeconds). 억제 중이면 noop.
  3. 이상 탐지(정적 임계값 + EWMA baseline) -> severity/anomaly_score.
  4. 트래픽량(RPS/pod) 기반 스케일링 + hysteresis.
  5. 여러 신호 동시 발생 시 안전한 방향(트래픽 감소/격리) 우선 -> Decision 우선순위.
  6. 모든 판단 근거를 Decision.reason에 명시.

Decision 우선순위 (skill: traffic-policy-engine):
  1. 에러 이상 + 특정 backend 집중 (+ 격리 허용) -> isolate_backend
  2. 에러/지연 이상이나 backend 특정 불가(전체 문제)   -> scale (증설로 부하 분산)
  3. 이상 해소 확인, 직전에 격리된 backend가 남아있음   -> reroute (점진적 weight 복구/재분배)
  4. RPS만 증가(에러/지연 정상)                        -> scale (hysteresis)
  5. RPS 저하                                          -> scale down (hysteresis)
  6. 모두 정상 / no_data / collection_failed / cooldown -> noop

reroute(3번)는 isolate_backend의 대칭 짝이다. isolate_backend가 이상 시 backend weight를
낮추지만, 이상이 해소된 뒤 그 weight를 정상으로 되돌리는 조치가 없으면 backend가 영원히
낮은 weight에 머무는 결함이 생긴다(FINDING-1, QA 리포트). reroute는 이 복구를 담당하며,
동시에 CRD/스키마가 1급 action으로 정의한 "카나리/트래픽 시프트" 계약을 실제로 채운다.
"""

from __future__ import annotations

import math
import re
import time
from typing import Dict, Optional

from ..schemas import Decision, TrafficSnapshot
from . import anomaly as anomaly_mod
from . import baseline as baseline_mod
from . import scaling as scaling_mod

# 격리 시 문제 backend에 부여할 weight (0~100 정수, HTTPRoute backendRef 규약).
_ISOLATE_WEIGHT_CRITICAL = 0     # critical: 완전 격리(서킷브레이커성).
_ISOLATE_WEIGHT_WARNING = 20     # warning: 부분 감량(트래픽 일부만 흘려 관찰).
_HEALTHY_WEIGHT = 100            # 건강한 backend 기준 weight(상대 가중치).

# reroute 복구가 수렴하는 데 필요한 reconcile 횟수를 추정하기 위한 가정치.
# actuator(k8s-gateway-actuator 스킬)의 MAX_WEIGHT_DELTA_PER_RECONCILE=30과 일치시킨다.
# policy는 실제 클러스터에 적용된 weight를 직접 조회하지 않으므로(actuator/router의 책임
# 영역), 정확한 수렴 여부 대신 이 가정치로 필요한 reissue 횟수를 근사한다. 실제 스텝이
# 이보다 작아도 안전하다 — router.set_backend_weights는 idempotent(변화 없으면 skip)라서
# 조기 종료돼도 다음 anomaly/cooldown 판단에는 영향이 없다.
_ASSUMED_ACTUATOR_MAX_WEIGHT_STEP = 30

_RECOVERY_MARKER_RE = re.compile(r"\[recovery done=(\d+) total=(\d+) backends=([^\]]*)\]")


# ---------------------------------------------------------------------------
# 설정 추출 - spec(dict)에서 thresholds/actions를 안전하게 읽는다.
# ---------------------------------------------------------------------------
def _thresholds(spec: dict) -> dict:
    return spec.get("thresholds", {}) or {}


def _actions(spec: dict) -> dict:
    return spec.get("actions", {}) or {}


# ---------------------------------------------------------------------------
# cooldown 판단
# ---------------------------------------------------------------------------
def _prev_decision(status: dict) -> dict:
    """직전 reconcile이 status에 남긴 Decision dict를 찾는다.

    handlers.reconcile의 반환은 kopf가 status.reconcile 하위에 병합한다
    (CRD printer column도 .status.reconcile.lastDecision.action을 참조).
    구버전/다른 배선 대비로 최상위 lastDecision도 함께 탐색한다(robust).
    """
    if not isinstance(status, dict):
        return {}
    reconcile = status.get("reconcile") or {}
    if isinstance(reconcile, dict) and reconcile.get("lastDecision"):
        return reconcile["lastDecision"] or {}
    if status.get("lastDecision"):
        return status["lastDecision"] or {}
    return {}


def _prev_reconcile_at(status: dict) -> Optional[float]:
    reconcile = status.get("reconcile") or {}
    if isinstance(reconcile, dict) and reconcile.get("lastReconcileAt") is not None:
        return reconcile["lastReconcileAt"]
    return status.get("lastReconcileAt")


def _cooldown_remaining(status: dict, cooldown_seconds: int, now: float) -> Optional[float]:
    """cooldown이 활성 상태면 남은 초를 반환, 아니면 None.

    두 경로로 판단(어느 쪽이든 활성이면 억제):
      (1) 직전 Decision에 심어둔 cooldown_until(권장 경로, self-contained).
      (2) 직전 Decision.action이 실제 액션(noop/reroute 아님)이고, lastReconcileAt + cooldown 이내.

    reroute는 (2)의 대상에서 제외한다. reroute는 격리 복구(_recovery_state)를 여러
    reconcile에 걸쳐 이어가야 하는데, 중간에 cooldown-noop이 한 번이라도 끼어들면
    noop이 직전 Decision을 덮어써 복구 마커([recovery done=.. total=..])가 유실되고
    복구가 영구히 멈춘다(발견된 버그). reroute 자체가 cooldown_until을 세팅하지 않는
    것도 같은 이유다 — 이상탐지발 flapping 방지가 목적인 cooldown을, 이미 안전하다고
    판단된 복구 시퀀스에까지 적용할 이유가 없다.
    """
    if cooldown_seconds <= 0:
        return None
    prev = _prev_decision(status)

    # (1) cooldown_until 기반
    cu = prev.get("cooldown_until")
    if cu is not None and now < cu:
        return cu - now

    # (2) lastReconcileAt + action 기반 폴백 (reroute 제외 — 위 설명 참조)
    prev_action = prev.get("action")
    if prev_action and prev_action not in ("noop", "reroute"):
        prev_at = _prev_reconcile_at(status)
        if prev_at is not None:
            remaining = cooldown_seconds - (now - prev_at)
            if remaining > 0:
                return remaining
    return None


def _carry_cooldown_until(status: dict) -> Optional[float]:
    """cooldown 억제 noop을 낼 때, 원래 cooldown 종료 시각을 그대로 보존해 전달한다.

    이렇게 해야 cooldown 억제로 생성된 noop이 status.lastDecision을 덮어써도
    cooldown_until이 계속 살아있어(다음 reconcile들에서) cooldown이 유지된다.
    """
    return _prev_decision(status).get("cooldown_until")


def _carry_backend_weights(status: dict) -> Optional[Dict[str, int]]:
    """cooldown 억제 noop을 낼 때, 직전 backend_weights를 그대로 보존해 전달한다.

    isolate_backend 직후 여러 cycle 동안 cooldown이 활성化되면 그 사이의 noop들이
    status.lastDecision을 덮어쓴다. 이때 backend_weights까지 함께 사라지면
    cooldown이 풀린 뒤 _recovery_state가 "격리된 적이 있다"는 사실 자체를 잃어버려
    복구(reroute)가 영원히 시작되지 않는다(발견된 버그). noop은 schemas.py 규약상
    backend_weights를 무시하므로(actuator도 참조하지 않음) 여기 실어 보내도 안전하다 —
    순수하게 policy 내부에서 recovery 상태를 이어가기 위한 용도다.
    """
    return _prev_decision(status).get("backend_weights")


# ---------------------------------------------------------------------------
# 현재 파드 수(분모) 추출
# ---------------------------------------------------------------------------
def _current_pods(snapshot: TrafficSnapshot) -> Optional[int]:
    if snapshot.total_ready_pods is not None:
        return snapshot.total_ready_pods
    # 폴백: per_backend의 ready_pods 합(모두 존재할 때만).
    if snapshot.per_backend:
        vals = [b.ready_pods for b in snapshot.per_backend if b.ready_pods is not None]
        if vals and len(vals) == len(snapshot.per_backend):
            return sum(vals)
    return None


# ---------------------------------------------------------------------------
# backend weight 맵 구성(격리)
# ---------------------------------------------------------------------------
def _isolation_weights(
    snapshot: TrafficSnapshot,
    culprits: list,
    culprit_weight: int,
) -> Dict[str, int]:
    """모든 알려진 backend에 대한 weight 맵을 만든다.

    culprit는 낮은 weight, 나머지는 정상 weight. actuator가 전체 그림을 받도록
    문제 backend뿐 아니라 건강한 backend도 명시한다(합=100 강제 안 함; 상대 가중치).
    """
    weights: Dict[str, int] = {}
    culprit_set = set(culprits)
    for b in snapshot.per_backend:
        weights[b.name] = culprit_weight if b.name in culprit_set else _HEALTHY_WEIGHT
    # per_backend에 없지만 culprit로 지목된 경우도 방어적으로 포함.
    for name in culprits:
        weights.setdefault(name, culprit_weight)
    return weights


# ---------------------------------------------------------------------------
# 격리 복구(reroute) 상태 추적
# ---------------------------------------------------------------------------
def _parse_recovery_marker(reason: str) -> Optional[Dict[str, object]]:
    """직전 reroute Decision.reason에 남긴 '[recovery done=D total=T backends=a,b]' 마커를 읽는다.

    reroute의 backend_weights는 항상 "목표"(=100)를 담기 때문에, 두 번째 cycle부터는
    weight 값만으로 "아직 복구 중인지"를 판단할 수 없다(이미 100을 목표로 선언했으므로).
    그래서 진행 횟수(done)와 필요 횟수(total), 대상 backend 목록을 reason에 직접 실어
    다음 reconcile로 전달한다 — schemas.py/handlers.py를 건드리지 않고 policy 모듈
    내부에서만 상태를 이어가기 위한 방법이다. 마커가 없으면 None.
    """
    if not reason:
        return None
    m = _RECOVERY_MARKER_RE.search(reason)
    if not m:
        return None
    done, total, backends_str = m.groups()
    backends = [b for b in backends_str.split(",") if b]
    if not backends:
        return None
    return {"done": int(done), "total": int(total), "backends": backends}


def _recovery_state(status: dict) -> Optional[Dict[str, object]]:
    """복구가 아직 진행 중이면 {"backends": [...], "done": int, "total": int}를 반환, 아니면 None.

    두 경로로 판단한다:
      (1) 직전 Decision이 reroute였고 자체 마커(done<total)가 아직 남아있음 -> 복구 진행 중.
          weight 값만으로는 이 경우를 판단할 수 없다 — reroute는 항상 목표(100)를 선언하므로
          prev.backend_weights가 이미 100이어도 실제 클러스터가 그만큼 수렴했다는 보장이 없다.
      (2) 그 외(isolate_backend, 또는 cooldown 중 backend_weights를 보존만 하는 noop)에
          weight<100인 backend가 남아있음 -> 격리 상태가 아직 해소되지 않음. 시작 weight로부터
          필요한 reconcile 횟수(total)를 새로 계산.
    (2)가 isolate_backend뿐 아니라 noop도 포함하는 이유: cooldown 활성 중에는 (C) 단계가
    noop을 반환하며 backend_weights를 그대로 보존(_carry_backend_weights)하는데, 그 노드의
    action은 "isolate_backend"가 아니라 "noop"이다. action만으로 판단하면 cooldown이 풀린
    직후 격리 이력 자체를 잃어버려 복구가 영원히 시작되지 않는다(발견된 버그).
    """
    prev = _prev_decision(status)
    action = prev.get("action")

    if action == "reroute":
        marker = _parse_recovery_marker(prev.get("reason", ""))
        if marker is None or marker["done"] >= marker["total"]:
            return None  # 마커 없음(recovery 아닌 reroute) 또는 이미 수렴 완료.
        return marker

    raw_weights = prev.get("backend_weights") or {}
    degraded = {
        name: w
        for name, w in raw_weights.items()
        if isinstance(w, (int, float)) and w < _HEALTHY_WEIGHT
    }
    if not degraded:
        return None
    min_start_weight = min(degraded.values())
    total = math.ceil((_HEALTHY_WEIGHT - min_start_weight) / _ASSUMED_ACTUATOR_MAX_WEIGHT_STEP)
    return {"backends": sorted(degraded), "done": 0, "total": max(total, 1)}


# ---------------------------------------------------------------------------
# 진입 함수
# ---------------------------------------------------------------------------
def evaluate(spec: dict, snapshot: TrafficSnapshot, status: dict) -> Decision:
    """TrafficSnapshot -> Decision. handlers.reconcile이 이 시그니처로 호출한다."""
    spec = spec or {}
    status = status or {}
    th = _thresholds(spec)
    act = _actions(spec)

    # 기준 시각: 관측 시각(snapshot.timestamp)을 우선 사용해 판단 일관성 확보.
    now = snapshot.timestamp if getattr(snapshot, "timestamp", 0) else time.time()
    if not now or now <= 0:
        now = time.time()

    # --- (A) non-ok 스냅샷: 절대 액션하지 않음 (결측 데이터 원칙) ---
    if snapshot.status != "ok":
        return Decision(
            action="noop",
            reason=f"snapshot.status={snapshot.status} -> 관측 불가, 액션 억제(noop)",
            severity="none",
        )

    cooldown_seconds = int(act.get("cooldownSeconds", 0) or 0)
    min_replicas = act.get("minReplicas")
    max_replicas = act.get("maxReplicas")
    allow_isolation = bool(act.get("allowRouteIsolation", False))

    target_rps_per_pod = th.get("targetRPSPerPod")
    scale_down_rps_per_pod = th.get("scaleDownRPSPerPod")
    scale_up_error_rate = th.get("scaleUpErrorRate")
    max_p99 = th.get("maxP99LatencyMs")

    # --- (B) 이상 탐지 (정적 + EWMA). baseline 갱신은 심각도 판단 후. ---
    baseline = baseline_mod.get_baseline(spec)
    anom = anomaly_mod.assess_anomaly(
        snapshot, baseline, scale_up_error_rate, max_p99
    )

    # baseline 갱신: critical 구간은 오염 방지를 위해 건너뛴다. 그 외에는 관측치로 학습.
    if anom.severity != "critical":
        baseline.observe(snapshot.error_rate, snapshot.p99_latency_ms)

    # --- (C) cooldown 강제 ---
    # 설계 §0: "flapping 방지(cooldown, hysteresis)는 CRD 레벨에서 강제된다 —
    # policy 엔진이 무시할 수 없다." 따라서 cooldown 활성 시 어떤 액션도 내지 않는다.
    remaining = _cooldown_remaining(status, cooldown_seconds, now)
    if remaining is not None:
        return Decision(
            action="noop",
            reason=(
                f"cooldown 활성(잔여 {remaining:.0f}s / cooldownSeconds={cooldown_seconds}) "
                f"-> 액션 억제. 관측: {anom.reason}"
            ),
            severity=anom.severity,
            anomaly_score=anom.score,
            cooldown_until=_carry_cooldown_until(status),  # cooldown 유지(보존)
            backend_weights=_carry_backend_weights(status),  # 격리 상태 보존(복구 유실 방지)
        )

    cooldown_until = now + cooldown_seconds if cooldown_seconds > 0 else None
    current_pods = _current_pods(snapshot)

    # --- (D) Decision 우선순위 ---

    # 1) 에러 이상 + 특정 backend 집중 + 격리 허용 -> isolate_backend
    if anom.error_anomaly and anom.culprit_backends:
        if allow_isolation:
            weight = (
                _ISOLATE_WEIGHT_CRITICAL
                if anom.severity == "critical"
                else _ISOLATE_WEIGHT_WARNING
            )
            weights = _isolation_weights(snapshot, anom.culprit_backends, weight)
            return Decision(
                action="isolate_backend",
                reason=(
                    f"에러 집중 backend={anom.culprit_backends} weight->{weight}로 격리 "
                    f"(severity={anom.severity}). 근거: {anom.reason}"
                ),
                backend_weights=weights,
                severity=anom.severity,
                anomaly_score=anom.score,
                cooldown_until=cooldown_until,
            )
        # 격리가 금지된 경우: 증설로라도 완화 시도(아래 systemic 경로로 폴백).
        anom.reasons.append(
            f"allowRouteIsolation=false -> backend {anom.culprit_backends} 격리 불가, 증설로 대응 전환"
        )

    # 2) 에러/지연 이상이나 backend 특정 불가(전체 문제) -> 증설(scale up)
    if anom.any_anomaly:
        target = scaling_mod.capacity_target_for_anomaly(
            snapshot.rps, current_pods, target_rps_per_pod, min_replicas, max_replicas
        )
        if target is not None and current_pods is not None and target > current_pods:
            return Decision(
                action="scale",
                reason=(
                    f"전체 트래픽 이상(severity={anom.severity}) -> 증설 {current_pods}->{target}. "
                    f"근거: {anom.reason}"
                ),
                target_replicas=target,
                severity=anom.severity,
                anomaly_score=anom.score,
                cooldown_until=cooldown_until,
            )
        # 증설 목표 산출 불가(파드 수 결측 등) 또는 이미 목표 이상 -> noop(근거 남김).
        return Decision(
            action="noop",
            reason=(
                f"트래픽 이상 감지(severity={anom.severity})했으나 증설 목표 산출 불가/불필요"
                f"(current_pods={current_pods}, target={target}). 근거: {anom.reason}"
            ),
            severity=anom.severity,
            anomaly_score=anom.score,
        )

    # 3) 이상 해소 확인 + 직전 격리(isolate_backend/reroute)가 아직 healthy로 복구되지 않음
    #    -> reroute로 점진 복구. 격리만 하고 복구가 없으면 backend가 영원히 낮은 weight에
    #    갇히는 결함이 생기므로(FINDING-1), 이 지점을 반드시 hysteresis 스케일링보다 먼저 확인한다.
    recovery = _recovery_state(status)
    if recovery is not None:
        backends = recovery["backends"]
        total = recovery["total"]
        next_done = recovery["done"] + 1
        weights_target = {name: _HEALTHY_WEIGHT for name in backends}
        return Decision(
            action="reroute",
            reason=(
                f"이상 해소 확인 -> 이전에 격리된 backend={backends} "
                f"정상 weight({_HEALTHY_WEIGHT})로 점진 복구 중 (진행 {next_done}/{total}). "
                f"관측: {anom.reason} "
                f"[recovery done={next_done} total={total} backends={','.join(backends)}]"
            ),
            backend_weights=weights_target,
            severity="none",
            anomaly_score=anom.score,
        )

    # 4) & 5) 에러/지연 정상 -> RPS/pod 기반 스케일링 (hysteresis)
    scale = scaling_mod.assess_scaling(
        snapshot.rps,
        current_pods,
        target_rps_per_pod,
        scale_down_rps_per_pod,
        min_replicas,
        max_replicas,
    )
    if scale.direction in ("up", "down") and scale.target_replicas is not None:
        return Decision(
            action="scale",
            reason=scale.reason,
            target_replicas=scale.target_replicas,
            severity="none",
            anomaly_score=anom.score,
            cooldown_until=cooldown_until,
        )

    # 6) 모두 정상 -> noop (근거 명시). cooldown_until은 세팅하지 않는다(정상 상태).
    return Decision(
        action="noop",
        reason=f"모든 지표 정상 -> 유지. {scale.reason}",
        severity="none",
        anomaly_score=anom.score,
    )
