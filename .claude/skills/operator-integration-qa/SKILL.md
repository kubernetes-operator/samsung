---
name: operator-integration-qa
description: "Kubernetes 트래픽 운영 오퍼레이터의 metrics/policy/actuator 모듈 간 경계면 정합성을 교차 비교로 검증한다. 스키마 필드 일치, CRD-코드 일치, kopf 핸들러 배선, 안전장치(cooldown/변경폭 제한) 구현 여부 확인 요청 시 사용. 모듈 완성 직후 점진 검증, 통합 전 최종 검증에 모두 사용."
---

# 오퍼레이터 통합 정합성 검증

metrics→policy→actuator 파이프라인의 경계면을 양쪽 동시에 읽어 검증하는 절차.

## 검증 순서 (점진적)

모듈이 완성되는 순서대로 검증한다. 세 모듈이 모두 끝날 때까지 기다리지 않는다.

1. `metrics-collector-dev` 완료 → `schemas.py`의 `TrafficSnapshot`과 실제 반환값 비교
2. `policy-engine-dev` 완료 → `TrafficSnapshot` 소비 코드와 `Decision` 생성 코드 비교
3. `actuator-dev` 완료 → `Decision`의 action 종류와 actuator의 분기 처리 비교
4. 전체 완료 → `handlers.py`의 reconcile 배선 확인

## 1. 스키마 필드 교차 비교

```bash
# schemas.py에 정의된 TrafficSnapshot 필드 추출
grep -A 20 "class TrafficSnapshot" operator/k8s_traffic_operator/schemas.py

# metrics 모듈이 실제로 채우는 필드 확인
grep -n "TrafficSnapshot(" operator/k8s_traffic_operator/metrics/*.py

# policy 모듈이 실제로 접근하는 필드 확인
grep -n "snapshot\." operator/k8s_traffic_operator/policy/*.py
```

세 결과를 나란히 놓고: (a) metrics가 채우지 않는데 policy가 읽는 필드 → `AttributeError`/`None` 위험, (b) metrics가 채우는데 아무도 읽지 않는 필드 → 죽은 데이터, 둘 다 리포트한다.

## 2. Decision action 종류 완전성

```bash
# policy가 만들 수 있는 모든 action 값
grep -n 'action="' operator/k8s_traffic_operator/policy/*.py

# actuator가 처리하는 action 분기
grep -n 'if decision.action\|case "' operator/k8s_traffic_operator/actuator/*.py
```

policy가 생성 가능한 action 중 actuator에 대응 분기가 없는 것이 있으면 **치명적 결함**으로 분류한다 — 해당 Decision이 내려지는 순간 아무 일도 일어나지 않거나 예외가 발생한다.

## 3. CRD 필드 소비 여부

```bash
# CRD spec 필드 전체 목록
grep -n "^\s*[a-zA-Z]*:" operator/crds/trafficpolicy.yaml

# 코드에서 spec 필드를 읽는 지점
grep -rn "spec\[.*\]\|spec\.get(\|spec\." operator/k8s_traffic_operator/
```

특히 `cooldownSeconds`, `scaleUpErrorRate`/`scaleDownRPSPerPod`(hysteresis), `maxReplicas`/`minReplicas` — 이 필드들이 정의만 되고 실제 로직에서 참조되지 않으면 CRD에 안전장치가 선언되어 있어도 무력화된 것이다. 이 검증 항목을 생략하지 않는다 — QA 가이드에서 가장 흔히 놓치는 결함 유형이 "설계에는 있는데 구현에는 없는" 케이스다.

## 4. kopf 핸들러 배선

```bash
grep -n "@kopf\." operator/k8s_traffic_operator/handlers.py
grep -n "metrics\.\|policy\.\|actuator\." operator/k8s_traffic_operator/handlers.py
```

`@kopf.timer` 핸들러 함수 본문 안에서 metrics → policy → actuator 세 모듈이 실제로 순서대로 호출되는지 확인한다. 이 배선이 빠지면 세 모듈이 개별적으로 완벽해도 오퍼레이터는 아무것도 하지 않는다.

## 5. 안전장치 구현 확인

| 안전장치 | 확인 위치 | 확인 방법 |
|---------|----------|----------|
| Cooldown | `policy/` | `status.get("lastActionTime")` 비교 로직 존재 여부 |
| Hysteresis | `policy/` | 스케일업/다운에 서로 다른 임계값 사용 여부 |
| 변경 폭 제한 | `actuator/` | weight 변경 시 delta clamp 로직 존재 여부 |
| Idempotency | `actuator/` | patch 전 현재 상태와 목표 상태 비교 후 스킵하는 로직 존재 여부 |
| no_data 처리 | `policy/` | `status in ("no_data", "collection_failed")`일 때 `noop` 반환하는지 |

## 리포트 형식

```markdown
## 경계면 검증 리포트

### metrics → policy
- [PASS/FAIL] 필드 일치: ...
- [PASS/FAIL] no_data 처리: ...

### policy → actuator
- [PASS/FAIL] action 완전성: ...

### CRD → 코드
- [PASS/FAIL] cooldownSeconds 소비: ...

### 발견된 결함
1. [파일:라인] 설명 — 재현 시나리오 — 제안 수정
```

발견 즉시 해당 개발 에이전트에게 파일:라인과 구체적 수정 방법을 SendMessage로 전달한다. 경계면 문제는 관련된 양쪽 에이전트 모두에게 알린다.
