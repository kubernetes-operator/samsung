"""Deployment 스케일링 실행기.

정책 엔진의 scale Decision을 실제 Deployment replica 변경으로 옮긴다.
`patch_namespaced_deployment_scale`(scale subresource)만 사용한다 — 전체
Deployment를 patch하면 다른 컨트롤러가 관리하는 필드와 충돌할 수 있으므로,
replicas만 건드리는 scale subresource를 쓰는 것이 최소 영향·최소 권한 원칙에 맞다.

RBAC 요구(operator-architect에게 전달됨):
    apps/deployments/scale : get, patch
"""

from __future__ import annotations

from typing import Optional

from kubernetes import client
from kubernetes.client.rest import ApiException

from ._result import ActionOutcome


def clamp_replicas(
    target: int,
    current: int,
    min_replicas: Optional[int],
    max_replicas: Optional[int],
    max_scale_step: Optional[int],
) -> int:
    """정책이 낸 목표 replica(절대값)에 actuator의 최종 방어선을 적용한다.

    적용 순서:
      1) min/max clamp   : spec.actions.minReplicas ~ maxReplicas 범위로 가둔다.
      2) 변경폭 제한      : |target - current| <= maxScaleStep 이 되도록 한 reconcile의
                           변화폭을 제한한다(급격한 스케일 방지). timer가 주기적으로
                           재실행되므로 여러 reconcile에 걸쳐 목표치에 점진 수렴한다.
      3) 음수 방지        : 최종값이 0 미만이 되지 않게 한다.

    min/max clamp를 변경폭 제한보다 먼저 적용하는 이유: 정책이 준 목표가 애초에
    허용 범위를 벗어났다면, "허용 가능한 목표"를 기준으로 변화폭을 계산해야
    한 스텝이 낭비되지 않는다.
    """
    goal = target
    if min_replicas is not None:
        goal = max(goal, int(min_replicas))
    if max_replicas is not None:
        goal = min(goal, int(max_replicas))

    if max_scale_step is not None and max_scale_step > 0:
        delta = goal - current
        if abs(delta) > max_scale_step:
            delta = max_scale_step if delta > 0 else -max_scale_step
        goal = current + delta

    return max(0, goal)


def scale_deployment(
    apps_v1: client.AppsV1Api,
    namespace: str,
    name: str,
    target_replicas: int,
    *,
    min_replicas: Optional[int] = None,
    max_replicas: Optional[int] = None,
    max_scale_step: Optional[int] = None,
    dry_run: bool = False,
) -> ActionOutcome:
    """Deployment replica를 target_replicas로 조정한다(안전장치 포함).

    - idempotent: 현재 replica가 최종 목표와 같으면 API 호출 없이 "skipped".
    - dry_run: 현재 상태만 읽어 계획을 만들고 실제 patch는 하지 않음("skipped", dry_run 표시).
    - API 에러(권한/not found): 재시도 없이(scale read는 read-modify-write가 아니라
      단순 patch이므로 resourceVersion 충돌 위험이 없다) "failed"로 보고. 예외는 던지지 않음.
    """
    # 1) 현재 상태 조회 (scale subresource) — 실패 시 클러스터 상태를 추측하지 않고 즉시 보고.
    try:
        scale = apps_v1.read_namespaced_deployment_scale(name, namespace)
        current = scale.spec.replicas or 0
    except ApiException as exc:
        return ActionOutcome(
            "failed",
            detail=f"deployment/{name} scale 조회 실패",
            error=f"read scale {namespace}/{name}: {exc.status} {exc.reason}",
        )

    final = clamp_replicas(target_replicas, current, min_replicas, max_replicas, max_scale_step)

    plan = f"replicas {current} -> {final} (policy target={target_replicas})"

    # 2) idempotent 스킵 — 이미 목표 상태.
    if final == current:
        return ActionOutcome("skipped", detail=f"이미 목표 상태: {plan}")

    # 3) dry-run — 쓰기 없이 계획만.
    if dry_run:
        return ActionOutcome("skipped", detail=f"[dry-run] {plan}")

    # 4) 실제 patch (scale subresource, replicas만 변경).
    try:
        apps_v1.patch_namespaced_deployment_scale(
            name, namespace, body={"spec": {"replicas": final}}
        )
    except ApiException as exc:
        return ActionOutcome(
            "failed",
            detail=f"patch 실패: {plan}",
            error=f"patch scale {namespace}/{name}: {exc.status} {exc.reason}",
        )

    return ActionOutcome("applied", detail=plan)
