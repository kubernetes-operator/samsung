"""트래픽량(RPS/pod) 기반 목표 replica 산출 - hysteresis 포함.

이 모듈의 유일한 판단 입력은 **트래픽 지표**(RPS와 Ready 파드 수)다. CPU/메모리는
어떤 경로로도 입력되지 않는다 — 이 오퍼레이터의 존재 이유는 "사용자가 실제로 겪는
트래픽 경험"을 기준으로 판단하는 것이기 때문이다.

핵심 공식 (skill: traffic-policy-engine):

    target_replicas = ceil(current_total_rps / targetRPSPerPod)
    target_replicas = clamp(target_replicas, minReplicas, maxReplicas)

hysteresis(flapping 방지):
    - 스케일업은 실측 RPS/pod가 targetRPSPerPod를 **초과**할 때만
    - 스케일다운은 실측 RPS/pod가 scaleDownRPSPerPod **미만**일 때만
    - 두 임계값 사이 밴드에서는 replica 유지(변화 없음)

`current_total_rps`는 순간값이 아니라 CRD 관측 윈도우로 집계된 값(TrafficSnapshot.rps)이다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScaleAssessment:
    """스케일링 판단 결과. engine이 Decision으로 조립한다."""

    direction: str                          # "up" | "down" | "none"
    target_replicas: Optional[int]          # 절대값. direction != "none"일 때만 의미 있음.
    current_replicas: Optional[int]         # 판단에 사용한 현재 파드 수(=분모).
    rps_per_pod: Optional[float]            # 실측 RPS/pod. None이면 계산 불가(분모 결측).
    reason: str                             # 사람이 읽는 근거.


def _clamp(value: int, low: Optional[int], high: Optional[int]) -> int:
    """min/max replicas로 clamp. policy 단에서도 방어적으로 적용한다.

    최종 방어선은 actuator(minReplicas~maxReplicas clamp + maxScaleStep)지만,
    policy가 애초에 범위를 벗어난 "의도"를 내지 않도록 여기서도 한 번 clamp한다.
    """
    if low is not None:
        value = max(value, low)
    if high is not None:
        value = min(value, high)
    return value


def assess_scaling(
    rps: Optional[float],
    current_pods: Optional[int],
    target_rps_per_pod: Optional[float],
    scale_down_rps_per_pod: Optional[float],
    min_replicas: Optional[int],
    max_replicas: Optional[int],
) -> ScaleAssessment:
    """RPS/pod 기반으로 스케일 방향과 목표 replica를 산출한다(hysteresis 적용).

    보수적 원칙:
      - targetRPSPerPod가 없거나 <= 0 이면 스케일 판단 불가 -> direction="none".
      - current_pods(total_ready_pods)가 None/<=0 이면 RPS/pod(분모)를 계산할 수 없다.
        결측 데이터로 추측 스케일링을 하지 않는다는 원칙에 따라 direction="none".
        (신규 배포 직후 파드 수 미확정 구간 등)
    """
    # --- 판단 불가 케이스: 보수적으로 유지 ---
    if rps is None or target_rps_per_pod is None or target_rps_per_pod <= 0:
        return ScaleAssessment(
            direction="none",
            target_replicas=None,
            current_replicas=current_pods,
            rps_per_pod=None,
            reason="스케일 판단 입력 부족(rps 또는 targetRPSPerPod 결측/0) -> 유지",
        )
    if current_pods is None or current_pods <= 0:
        return ScaleAssessment(
            direction="none",
            target_replicas=None,
            current_replicas=current_pods,
            rps_per_pod=None,
            reason="total_ready_pods 결측/0 -> RPS/pod 계산 불가, 추측 스케일링 억제(유지)",
        )

    rps_per_pod = rps / current_pods
    # 정상 부하에서 파드당 targetRPSPerPod를 맞추도록, 양방향 모두 targetRPSPerPod를 분모로
    # 목표를 계산한다(정상상태 수렴점을 target으로 통일). scaleDownRPSPerPod는 "축소를
    # 허용할지"의 게이트로만 쓴다 -> 이것이 hysteresis 밴드를 만든다.
    desired = math.ceil(rps / target_rps_per_pod)
    desired = _clamp(desired, min_replicas, max_replicas)

    # --- 스케일업: 실측 RPS/pod > targetRPSPerPod ---
    if rps_per_pod > target_rps_per_pod and desired > current_pods:
        return ScaleAssessment(
            direction="up",
            target_replicas=desired,
            current_replicas=current_pods,
            rps_per_pod=rps_per_pod,
            reason=(
                f"RPS/pod={rps_per_pod:.1f} > targetRPSPerPod={target_rps_per_pod:.1f} "
                f"(rps={rps:.1f}, pods={current_pods}) -> {current_pods}->{desired} 스케일업"
            ),
        )

    # --- 스케일다운: 실측 RPS/pod < scaleDownRPSPerPod (설정된 경우에만) ---
    if (
        scale_down_rps_per_pod is not None
        and rps_per_pod < scale_down_rps_per_pod
        and desired < current_pods
    ):
        return ScaleAssessment(
            direction="down",
            target_replicas=desired,
            current_replicas=current_pods,
            rps_per_pod=rps_per_pod,
            reason=(
                f"RPS/pod={rps_per_pod:.1f} < scaleDownRPSPerPod={scale_down_rps_per_pod:.1f} "
                f"(rps={rps:.1f}, pods={current_pods}) -> {current_pods}->{desired} 스케일다운"
            ),
        )

    # --- hysteresis 밴드 내부 또는 목표=현재: 유지 ---
    band_hi = f"{target_rps_per_pod:.1f}"
    band_lo = "미설정" if scale_down_rps_per_pod is None else f"{scale_down_rps_per_pod:.1f}"
    return ScaleAssessment(
        direction="none",
        target_replicas=None,
        current_replicas=current_pods,
        rps_per_pod=rps_per_pod,
        reason=(
            f"RPS/pod={rps_per_pod:.1f} 가 hysteresis 밴드[{band_lo}, {band_hi}] 내 "
            f"또는 목표==현재({current_pods}) -> 유지"
        ),
    )


def capacity_target_for_anomaly(
    rps: Optional[float],
    current_pods: Optional[int],
    target_rps_per_pod: Optional[float],
    min_replicas: Optional[int],
    max_replicas: Optional[int],
) -> Optional[int]:
    """이상 탐지(지연/에러 급증)로 인한 증설 목표 replica를 산출한다.

    RPS 자체는 임계 미만이어도 지연/에러가 치솟으면 용량을 늘려 부하를 분산한다.
    목표는 "RPS 기반 목표"와 "현재+1" 중 큰 값 -> 최소 1파드는 증설되도록 보장.
    current_pods가 결측이면 안전하게 None(증설 목표 산출 불가)을 반환한다.
    actuator가 maxReplicas/maxScaleStep으로 최종 제한한다.
    """
    if current_pods is None or current_pods <= 0:
        return None
    candidate = current_pods + 1
    if rps is not None and target_rps_per_pod and target_rps_per_pod > 0:
        candidate = max(candidate, math.ceil(rps / target_rps_per_pod))
    return _clamp(candidate, min_replicas, max_replicas)
