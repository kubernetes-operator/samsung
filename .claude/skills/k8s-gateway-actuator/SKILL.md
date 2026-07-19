---
name: k8s-gateway-actuator
description: "정책 엔진의 Decision을 실제 Kubernetes/Gateway API 리소스 변경으로 실행한다. kubernetes python client로 Deployment replica 패치, HTTPRoute backendRefs weight 패치, 변경 폭 제한/dry-run/롤백 안전장치 구현 요청 시 사용."
---

# Kubernetes/Gateway API 액션 실행 (Actuator)

정책 엔진의 결정을 실제 클러스터 변경으로 옮기는 절차. 이 스킬이 다루는 코드는 프로덕션 트래픽에 직접 영향을 주므로, 다른 모듈보다 방어적으로 작성한다.

## Deployment 스케일링

```python
from kubernetes import client

def scale_deployment(apps_v1: client.AppsV1Api, namespace: str, name: str, target_replicas: int):
    current = apps_v1.read_namespaced_deployment(name, namespace)
    if current.spec.replicas == target_replicas:
        return "skipped"  # idempotent — 이미 목표 상태
    apps_v1.patch_namespaced_deployment_scale(
        name, namespace,
        body={"spec": {"replicas": target_replicas}},
    )
    return "applied"
```

`patch_namespaced_deployment_scale`(scale subresource)을 사용한다 — 전체 Deployment를 patch하면 다른 컨트롤러(예: 사용자가 별도로 관리하는 필드)와 충돌할 위험이 있다. scale subresource는 replicas만 건드린다.

## HTTPRoute 가중치 변경

```python
def set_backend_weights(custom_api: client.CustomObjectsApi, namespace: str, route_name: str, weights: dict):
    route = custom_api.get_namespaced_custom_object(
        group="gateway.networking.k8s.io", version="v1", namespace=namespace,
        plural="httproutes", name=route_name,
    )
    for rule in route["spec"]["rules"]:
        for ref in rule["backendRefs"]:
            if ref["name"] in weights:
                ref["weight"] = weights[ref["name"]]
    custom_api.replace_namespaced_custom_object(
        group="gateway.networking.k8s.io", version="v1", namespace=namespace,
        plural="httproutes", name=route_name, body=route,
    )
```

HTTPRoute는 CRD이므로 `CustomObjectsApi`를 사용한다. 전체 객체를 읽어와 수정 후 `replace`하는 read-modify-write 패턴을 쓸 때는 `resourceVersion`이 요청 사이에 바뀔 수 있음을 감안해 충돌(409) 시 재조회 후 1회 재시도한다.

## 변경 폭 제한 (방어적 안전판)

정책 엔진이 계산을 잘못해도 actuator가 마지막 방어선이 되도록, 한 번의 reconcile에서 weight 변화폭에 상한을 둔다:

```python
MAX_WEIGHT_DELTA_PER_RECONCILE = 30  # percentage points

def clamp_weight_change(current_weight: int, target_weight: int) -> int:
    delta = target_weight - current_weight
    if abs(delta) > MAX_WEIGHT_DELTA_PER_RECONCILE:
        delta = MAX_WEIGHT_DELTA_PER_RECONCILE if delta > 0 else -MAX_WEIGHT_DELTA_PER_RECONCILE
    return current_weight + delta
```

정책 엔진의 목표치가 이미 안전 범위 내라고 신뢰하더라도, 이 클램프를 actuator에 두는 이유는 정책 로직의 버그(cooldown 미적용, 계산 오류 등)가 즉시 "트래픽 전량 차단" 같은 최악의 결과로 이어지지 않게 하기 위함이다. kopf timer가 주기적으로 재실행되므로, 여러 reconcile에 걸쳐 점진적으로 목표치에 수렴한다.

## Idempotency

같은 Decision이 반복 적용되어도 상태가 발산하지 않아야 한다. patch 전 항상 현재 상태를 조회하여 목표와 같으면 API 호출 자체를 생략한다(`"skipped"` 반환) — 불필요한 API 서버 부하와 리소스 버전 충돌을 모두 줄인다.

## 실행 결과 반환

모든 액션 함수는 `"applied"` / `"skipped"` / `"failed"` 중 하나를 명시적으로 반환한다. 호출부(`handlers.py`)가 이 결과를 `status`에 기록해야 다음 reconcile의 cooldown 판단(마지막 액션 시각)이 정확해진다.

## RBAC 요구사항

이 모듈이 필요로 하는 최소 권한을 `operator-architect`에게 명시적으로 전달한다:
- `apps/deployments/scale`: get, patch
- `gateway.networking.k8s.io/httproutes`: get, list, update
- CR 자체(`trafficpolicies`): get, list, watch, patch (status subresource)

과도한 권한(예: 클러스터 전체 리소스에 대한 `*`)을 요청하지 않는다 — 최소 권한 원칙이 이 오퍼레이터의 실패 시 영향 범위를 제한한다.
