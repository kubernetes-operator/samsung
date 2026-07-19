"""HTTPRoute(gateway.networking.k8s.io) 라우팅 제어 실행기.

정책 엔진의 reroute / isolate_backend Decision을 HTTPRoute backendRefs의
weight 변경으로 옮긴다. HTTPRoute는 CRD이므로 CustomObjectsApi로
read-modify-write(get -> 수정 -> replace) 한다.

핵심 안전장치:
  - 변경폭 제한: 한 reconcile에서 backend별 weight 변화폭을 MAX_WEIGHT_DELTA_PER_RECONCILE
    (percentage points)로 clamp. 정책 로직의 버그가 즉시 "트래픽 전량 차단/전량 전환"으로
    이어지지 않게 하는 마지막 방어선. timer 재실행으로 여러 reconcile에 걸쳐 수렴한다.
  - resourceVersion 충돌(409): 최신 상태를 재조회 후 1회 재시도.
  - idempotent: 계산된 최종 weight가 현재와 모두 같으면 replace 호출 자체를 생략.

RBAC 요구(operator-architect에게 전달됨):
    gateway.networking.k8s.io/httproutes : get, list, update

가정(assumption): Gateway API 그룹은 gateway.networking.k8s.io. 안정 버전 v1을
우선 시도하고, 클러스터가 v1을 제공하지 않으면(404) v1beta1로 폴백한다. 스키마·필드
구조(spec.rules[].backendRefs[].weight)는 v1/v1beta1 동일하다.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from kubernetes import client
from kubernetes.client.rest import ApiException

from ._result import ActionOutcome

GATEWAY_GROUP = "gateway.networking.k8s.io"
HTTPROUTE_PLURAL = "httproutes"
# 우선 v1, 실패 시 v1beta1. 대부분 클러스터가 v1(안정) 제공.
HTTPROUTE_VERSIONS: Tuple[str, ...] = ("v1", "v1beta1")

# 한 reconcile에서 허용하는 backend별 weight 변화폭 상한(percentage points).
# 정책 목표치를 신뢰하되, 최종 방어선으로 actuator에도 방어적으로 둔다.
MAX_WEIGHT_DELTA_PER_RECONCILE = 30

# Gateway API 규약: backendRef의 weight가 생략되면 기본값 1로 간주된다.
DEFAULT_BACKEND_WEIGHT = 1


def clamp_weight_change(current_weight: int, target_weight: int) -> int:
    """한 reconcile의 weight 변화폭을 MAX_WEIGHT_DELTA_PER_RECONCILE로 제한하고 0~100으로 가둔다."""
    delta = target_weight - current_weight
    if abs(delta) > MAX_WEIGHT_DELTA_PER_RECONCILE:
        delta = MAX_WEIGHT_DELTA_PER_RECONCILE if delta > 0 else -MAX_WEIGHT_DELTA_PER_RECONCILE
    result = current_weight + delta
    return max(0, min(100, result))


def _normalize_targets(weights: Dict[str, int]) -> Tuple[Dict[str, int], List[str]]:
    """정책이 준 backend_weights를 검증·정규화한다.

    - 값이 int로 해석 불가하거나 0~100 밖이면 해당 항목을 거부(무시)하고 사유를 남긴다.
    - actuator는 "추측 실행 금지" 원칙에 따라 이상한 값은 clamp가 아니라 거부한다
      (0~100 밖 값은 정책 버그 신호이므로 조용히 보정하지 않고 드러낸다).
    """
    clean: Dict[str, int] = {}
    rejected: List[str] = []
    for name, w in weights.items():
        try:
            wi = int(w)
        except (TypeError, ValueError):
            rejected.append(f"{name}={w!r}(정수 아님)")
            continue
        if wi < 0 or wi > 100:
            rejected.append(f"{name}={wi}(0~100 범위 밖)")
            continue
        clean[name] = wi
    return clean, rejected


def _apply_weights_to_route(route: dict, targets: Dict[str, int]) -> Tuple[bool, List[str], List[str]]:
    """route dict을 in-place 수정한다. (변경 발생 여부, 변경 요약, 매칭된 backend 목록) 반환.

    변경폭 제한(clamp_weight_change)을 backend별로 적용한다. 현재 weight는 생략 시
    DEFAULT_BACKEND_WEIGHT(1)로 간주.
    """
    changed = False
    summaries: List[str] = []
    matched: List[str] = []
    for rule in route.get("spec", {}).get("rules", []) or []:
        for ref in rule.get("backendRefs", []) or []:
            ref_name = ref.get("name")
            if ref_name not in targets:
                continue
            matched.append(ref_name)
            current = ref.get("weight", DEFAULT_BACKEND_WEIGHT)
            desired = clamp_weight_change(current, targets[ref_name])
            if desired != current:
                ref["weight"] = desired
                changed = True
                summaries.append(f"{ref_name} {current}->{desired}(target={targets[ref_name]})")
            else:
                summaries.append(f"{ref_name} {current}(변화없음)")
    return changed, summaries, matched


def _get_route(custom_api: client.CustomObjectsApi, namespace: str, name: str) -> Tuple[Optional[dict], str, Optional[str]]:
    """HTTPRoute를 조회한다. v1 -> v1beta1 순으로 시도. (route, 사용버전, 에러) 반환."""
    last_exc: Optional[ApiException] = None
    for version in HTTPROUTE_VERSIONS:
        try:
            route = custom_api.get_namespaced_custom_object(
                group=GATEWAY_GROUP, version=version, namespace=namespace,
                plural=HTTPROUTE_PLURAL, name=name,
            )
            return route, version, None
        except ApiException as exc:
            last_exc = exc
            # 404가 "리소스 종류 없음"(해당 버전 미제공)인지 "객체 없음"인지 구분이 어렵다.
            # 다음 버전을 시도해 보고, 모두 실패하면 마지막 에러를 보고한다.
            if exc.status == 404:
                continue
            # 권한 등 그 외 에러는 즉시 보고(버전 폴백 무의미).
            return None, version, f"get httproute {namespace}/{name} (v={version}): {exc.status} {exc.reason}"
    status = last_exc.status if last_exc else "?"
    reason = last_exc.reason if last_exc else "unknown"
    return None, HTTPROUTE_VERSIONS[-1], f"get httproute {namespace}/{name}: {status} {reason}"


def set_backend_weights(
    custom_api: client.CustomObjectsApi,
    namespace: str,
    route_name: str,
    weights: Dict[str, int],
    *,
    dry_run: bool = False,
) -> ActionOutcome:
    """HTTPRoute backendRefs weight를 목표치로 조정한다(변경폭 제한·충돌 재시도 포함)."""
    targets, rejected = _normalize_targets(weights)
    if not targets:
        return ActionOutcome(
            "failed",
            detail="적용 가능한 backend weight 없음",
            error=f"모든 weight 항목이 유효하지 않음: {', '.join(rejected) or '(빈 맵)'}",
        )

    # read-modify-write + 409 충돌 시 1회 재시도.
    for attempt in range(2):
        route, version, err = _get_route(custom_api, namespace, route_name)
        if err is not None:
            return ActionOutcome("failed", detail=f"httproute/{route_name} 조회 실패", error=err)

        changed, summaries, matched = _apply_weights_to_route(route, targets)

        # 정책이 지정한 backend 중 route에 실제로 없는 것 — 매칭 실패는 경계면 신호이므로 detail에 남긴다.
        unmatched = [b for b in targets if b not in matched]
        notes = []
        if rejected:
            notes.append(f"거부됨: {', '.join(rejected)}")
        if unmatched:
            notes.append(f"route에 없는 backend: {', '.join(unmatched)}")
        note_str = (" | " + "; ".join(notes)) if notes else ""

        detail = f"[{', '.join(summaries) or 'no backendRefs matched'}]{note_str}"

        # 매칭된 backend가 하나도 없으면 patch 대상이 없다 — 실패로 보고(오타/스키마 불일치 의심).
        if not matched:
            return ActionOutcome(
                "failed",
                detail=f"httproute/{route_name}",
                error=f"지정한 backend가 route에 없음: {', '.join(targets)}{note_str}",
            )

        # idempotent 스킵 — 계산된 최종 weight가 현재와 모두 같음.
        if not changed:
            return ActionOutcome("skipped", detail=f"이미 목표 상태 {detail}")

        # dry-run — 쓰기 없이 계획만.
        if dry_run:
            return ActionOutcome("skipped", detail=f"[dry-run] {detail}")

        # replace (read-modify-write). resourceVersion은 방금 읽은 route에 포함되어 전송된다.
        try:
            custom_api.replace_namespaced_custom_object(
                group=GATEWAY_GROUP, version=version, namespace=namespace,
                plural=HTTPROUTE_PLURAL, name=route_name, body=route,
            )
            return ActionOutcome("applied", detail=detail)
        except ApiException as exc:
            if exc.status == 409 and attempt == 0:
                # resourceVersion 충돌 — 최신 상태 재조회 후 1회 재시도.
                continue
            return ActionOutcome(
                "failed",
                detail=f"replace 실패 {detail}",
                error=f"replace httproute {namespace}/{route_name}: {exc.status} {exc.reason}",
            )

    # 재시도까지 소진(409 반복).
    return ActionOutcome(
        "failed",
        detail=f"httproute/{route_name}",
        error="resourceVersion 충돌(409) 재시도 후에도 실패",
    )
