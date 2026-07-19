---
name: k8s-traffic-ops-orchestrator
description: "Kubernetes 트래픽 기반 자동운영 오퍼레이터(Python+kopf, Gateway API 트래픽 지표 기반 스케일링/장애감지/라우팅제어/이상탐지)를 만들거나 확장하는 5인 에이전트 팀을 조율한다. '트래픽 기반 오퍼레이터', 'k8s 자동 운영', 'Gateway API 스케일링', '트래픽 이상탐지', '라우팅 자동 제어' 관련 요청 시 반드시 사용. 후속 작업: 이 오퍼레이터의 CRD 수정, 스케일링/이상탐지 정책 변경, 특정 모듈만 다시 구현, 이전 결과 개선, 재실행 요청 시에도 반드시 이 스킬을 사용. QA 전체 PASS까지 무인 반복이 필요하면 ralph-loop 플러그인으로 이 스킬을 감싸 사용(완료 시 <promise> 태그 출력)."
---

# Kubernetes 트래픽 운영 오퍼레이터 — 팀 오케스트레이터

Pod의 CPU/메모리가 아닌 **서비스 트래픽(RPS/에러율/지연시간)**을 기준으로 Kubernetes를 자동 운영하는 Python kopf 오퍼레이터를 5인 에이전트 팀으로 설계·구현·검증한다.

## 실행 모드: 에이전트 팀

## 에이전트 구성

| 팀원 | 역할 | 스킬 | 출력 |
|------|------|------|------|
| operator-architect | CRD/kopf 구조/모듈 인터페이스 설계 | k8s-operator-design | `schemas.py`, `crds/trafficpolicy.yaml`, `handlers.py` |
| metrics-collector-dev | Gateway API/Prometheus 트래픽 메트릭 수집 | gateway-api-traffic-metrics | `metrics/` 모듈 |
| policy-engine-dev | 스케일링/이상탐지/자동대응 정책 | traffic-policy-engine | `policy/` 모듈 |
| actuator-dev | Deployment 스케일링/HTTPRoute 라우팅 실행 | k8s-gateway-actuator | `actuator/` 모듈 |
| qa-engineer | 모듈 간 경계면 정합성 검증 | operator-integration-qa | 검증 리포트 |

## 워크플로우

### Phase 0: 컨텍스트 확인 (후속 작업 지원)

1. `operator/` 디렉토리와 `_workspace/` 존재 여부 확인
2. 실행 모드 결정:
   - **`operator/` 미존재** → 초기 구축. Phase 1로 진행
   - **`operator/` 존재 + 사용자가 특정 모듈만 수정 요청** (예: "이상탐지 알고리즘만 바꿔줘", "CRD에 필드 추가해줘") → **부분 재실행**. 해당 에이전트만 단독 재호출(팀 구성 불필요, 서브 에이전트로 충분). 기존 코드를 Read하여 이해한 뒤 수정
   - **`operator/` 존재 + 아키텍처 변경 요청**(대상 리소스 종류 변경, 새 대응 액션 추가 등) → **팀 재구성**. 기존 `operator/`를 그대로 두고 Phase 2부터 재진행, 각 에이전트가 기존 코드를 Read 후 확장
   - **`_workspace/qa_report.md` 존재 + FAIL 항목이 남아 있음, 사용자의 새 지시 없음** → **QA 재작업 재개** (Ralph Loop 등 외부 루프가 동일 프롬프트로 재호출한 경우 포함). FAIL이 기록된 경계면의 담당 에이전트만 서브 에이전트로 재호출하여 리포트에 적힌 구체적 수정 요청을 그대로 전달하고, `qa-engineer`로 해당 경계면만 재검증 → Phase 6으로
3. 부분 재실행 시 해당 에이전트 정의(`.claude/agents/{name}.md`)의 "이전 산출물이 있을 때" 원칙에 따라 기존 코드를 존중하며 수정하도록 프롬프트에 명시

### Phase 1: 준비

1. 사용자 요구사항 분석 — 대상 서비스, Gateway API 구현체(Envoy Gateway/Istio 등), 스케일 범위, 우선 구현 범위(스케일링만 vs 전체 4대 기능) 파악. 모호하면 사용자에게 확인
2. `_workspace/` 생성 (부분 재실행이 아닌 경우)
3. `operator/` 표준 스캐폴딩 경로 확정 (기존 프로젝트 구조와 충돌 시 사용자에게 확인)

### Phase 2: 팀 구성

```
TeamCreate(
  team_name: "k8s-traffic-ops-team",
  members: [
    { name: "operator-architect", agent_type: "operator-architect", model: "opus",
      prompt: "TrafficPolicy CRD, kopf 핸들러 구조, schemas.py(TrafficSnapshot/Decision)를 설계하고 스캐폴딩을 생성하라. 완성 즉시 다른 세 팀원에게 인터페이스를 브로드캐스트하라." },
    { name: "metrics-collector-dev", agent_type: "metrics-collector-dev", model: "opus",
      prompt: "operator-architect의 schemas.py를 기다렸다가, Gateway API 구현체 기반 트래픽 메트릭 수집기를 구현하라." },
    { name: "policy-engine-dev", agent_type: "policy-engine-dev", model: "opus",
      prompt: "operator-architect의 schemas.py를 기다렸다가, 트래픽 기반 스케일링/이상탐지 정책 엔진을 구현하라." },
    { name: "actuator-dev", agent_type: "actuator-dev", model: "opus",
      prompt: "operator-architect의 schemas.py를 기다렸다가, Decision을 실행하는 actuator를 구현하라." },
    { name: "qa-engineer", agent_type: "qa-engineer", model: "opus",
      prompt: "각 팀원의 모듈이 완성되는 대로 즉시 경계면 정합성을 검증하라. 전원 완료 후 handlers.py 배선까지 최종 검증하라." }
  ]
)
```

작업 등록:

```
TaskCreate(tasks: [
  { title: "CRD/스키마/스캐폴딩 설계", assignee: "operator-architect" },
  { title: "트래픽 메트릭 수집기 구현", assignee: "metrics-collector-dev", depends_on: ["CRD/스키마/스캐폴딩 설계"] },
  { title: "정책 엔진 구현", assignee: "policy-engine-dev", depends_on: ["CRD/스키마/스캐폴딩 설계"] },
  { title: "actuator 구현", assignee: "actuator-dev", depends_on: ["CRD/스키마/스캐폴딩 설계"] },
  { title: "경계면 검증 (metrics↔policy)", assignee: "qa-engineer", depends_on: ["트래픽 메트릭 수집기 구현", "정책 엔진 구현"] },
  { title: "경계면 검증 (policy↔actuator)", assignee: "qa-engineer", depends_on: ["정책 엔진 구현", "actuator 구현"] },
  { title: "최종 통합 검증 (handlers.py 배선)", assignee: "qa-engineer", depends_on: ["경계면 검증 (metrics↔policy)", "경계면 검증 (policy↔actuator)"] }
])
```

### Phase 3: 설계 (operator-architect 우선 착수)

**실행 방식:** 팀 내 우선순위 작업. `operator-architect`가 `schemas.py`/CRD/스캐폴딩을 먼저 완성하고 나머지 3명에게 SendMessage로 브로드캐스트해야 병렬 구현이 시작될 수 있다. 리더는 architect의 완료 알림을 기다린다.

### Phase 4: 병렬 구현 + Incremental QA

**실행 방식:** 팀원들이 자체 조율

`metrics-collector-dev`, `policy-engine-dev`, `actuator-dev`가 `schemas.py`를 기반으로 각자 모듈을 병렬 구현한다. 인터페이스 해석에 이견이 있으면 SendMessage로 `operator-architect`에게 직접 질의한다. `qa-engineer`는 대기하지 않고, 완성되는 모듈이 생길 때마다 즉시 해당 경계면을 검증한다(예: metrics 완료 즉시 metrics↔policy 검증 착수, policy가 아직 미완성이면 스키마 정의만으로 우선 확인).

**산출물 저장:**

| 팀원 | 출력 경로 |
|------|----------|
| operator-architect | `operator/k8s_traffic_operator/schemas.py`, `operator/crds/trafficpolicy.yaml`, `operator/k8s_traffic_operator/handlers.py` |
| metrics-collector-dev | `operator/k8s_traffic_operator/metrics/` |
| policy-engine-dev | `operator/k8s_traffic_operator/policy/` |
| actuator-dev | `operator/k8s_traffic_operator/actuator/` |
| qa-engineer | `_workspace/qa_report.md` (누적 갱신) |

**리더 모니터링:** TaskGet으로 진행률 확인, 팀원이 유휴 상태가 되면 자동 알림 수신 → 막힌 경우 SendMessage로 개입.

### Phase 5: 최종 통합 검증

1. 전원 완료 대기 (TaskGet)
2. `qa-engineer`가 `handlers.py`의 reconcile 배선(metrics→policy→actuator 순서 호출)까지 검증하여 `_workspace/qa_report.md` 완성
3. 리포트에 FAIL 항목이 있으면 해당 에이전트에게 재작업 요청 (최대 2회 루프)
4. 2회 재작업 후에도 남은 FAIL은 사용자에게 명시적으로 보고 — 이 경우 Phase 6에서 완료 promise를 출력하지 않는다 (아래 "Ralph Loop 연동" 참조)

### Phase 6: 정리

1. 팀원들에게 종료 요청 (SendMessage)
2. `TeamDelete`
3. `_workspace/` 보존
4. 사용자에게 결과 요약: 구현된 기능 범위, 남은 이슈, `operator/` 디렉토리 구조
5. `_workspace/qa_report.md`에 FAIL 항목이 하나도 없으면(초기 실행이든 재작업 이후든) 응답 말미에 `<promise>OPERATOR BUILD COMPLETE</promise>`를 출력한다. FAIL이 하나라도 남아 있으면 이 태그를 출력하지 않는다 — Ralph Loop로 구동 중일 때 이 태그의 유무가 다음 iteration을 반복할지 멈출지를 결정한다.

## 데이터 흐름

```
[리더] → TeamCreate
              │
   operator-architect (schemas.py/CRD 우선 완성)
              │ SendMessage 브로드캐스트
   ┌──────────┼──────────┐
metrics-dev  policy-dev  actuator-dev   (병렬, 서로 SendMessage로 인터페이스 질의)
   │             │             │
   └─────────────┴─────────────┘
              │ (완성되는 대로)
        qa-engineer (incremental 검증)
              │
      _workspace/qa_report.md
              │
        [리더: 최종 보고]
```

## 에러 핸들링

| 상황 | 전략 |
|------|------|
| operator-architect가 지연되어 나머지 팀원이 대기 | 리더가 SendMessage로 진행 상황 확인, 필요 시 스키마 초안만 먼저 공유하도록 지시 |
| 특정 팀원 실패/중지 | 유휴 알림 감지 → 재시작 시도 → 실패 시 다른 팀원(또는 리더)이 대신 처리 |
| qa-engineer가 동일 경계면에서 반복 FAIL | 스키마 자체의 모호성일 가능성 → operator-architect에게 에스컬레이션 |
| 팀원 과반 실패 | 사용자에게 알리고 계속 진행 여부 확인 |
| Gateway API 구현체 미상 | metrics-collector-dev가 표준 컨벤션으로 폴백, 사용자에게 실제 구현체 확인 요청 |

## Ralph Loop 연동 (선택)

QA가 전부 PASS할 때까지 무인으로 반복 실행하고 싶다면, `ralph-loop` 플러그인으로 이 오케스트레이터를 감쌀 수 있다. Ralph Loop는 세션이 끝나려 할 때마다 같은 프롬프트를 다시 주입하는 방식으로 동작하므로, 이 스킬의 Phase 0(컨텍스트 확인)이 매 iteration마다 `operator/`·`_workspace/qa_report.md`를 다시 읽어 이전 작업을 이어가는 것이 전제다 — 처음부터 다시 만들지 않는다.

**실행 예시:**
```
/ralph-loop "k8s-traffic-ops-orchestrator 스킬로 트래픽 기반 오퍼레이터를 빌드하라. _workspace/qa_report.md에 FAIL이 하나라도 남아 있으면 해당 경계면만 재작업 후 다시 검증하라. FAIL이 모두 사라지면 <promise>OPERATOR BUILD COMPLETE</promise>를 출력하라." --completion-promise "OPERATOR BUILD COMPLETE" --max-iterations 8
```

**주의:**
- `TeamCreate`로 만든 팀은 iteration 간에 유지되지 않는다 — Ralph가 프롬프트를 다시 주입할 때마다 이 스킬은 Phase 2에서 팀을 새로 구성한다. 이미 완료된 모듈은 Phase 0에서 재작업 대상으로 잡히지 않으므로 중복 구현은 피하지만, 팀 구성 자체의 오버헤드는 iteration마다 발생한다.
- `--max-iterations`를 반드시 지정한다. completion promise 없이는 QA가 영원히 FAIL을 내는 경우 무한 반복될 수 있다.
- 아키텍처 설계 판단(대상 리소스 변경 등 Phase 0의 "팀 재구성" 분기)처럼 사람의 판단이 필요한 상황에서는 Ralph Loop를 쓰지 않는다 — QA PASS/FAIL처럼 성공 기준이 명확한 반복에만 적합하다.

## 테스트 시나리오

### 정상 흐름
1. 사용자가 "트래픽 기반 k8s 자동 운영 오퍼레이터 만들어줘" 요청
2. Phase 0에서 `operator/` 미존재 확인 → 초기 구축 모드
3. Phase 2에서 5인 팀 구성 + 7개 작업 등록
4. Phase 3에서 operator-architect가 스키마/CRD 완성 후 브로드캐스트
5. Phase 4에서 3명이 병렬 구현, qa-engineer가 점진 검증
6. Phase 5에서 최종 검증 통과
7. Phase 6에서 팀 정리, `operator/` 디렉토리 결과 보고

### 에러 흐름
1. Phase 4에서 metrics-collector-dev가 Gateway API 구현체를 특정하지 못해 중단
2. qa-engineer가 metrics↔policy 검증에서 스키마 미충족 발견
3. 리더가 SendMessage로 metrics-collector-dev 상태 확인 → 구현체 확인 필요 사실 파악
4. 사용자에게 실제 클러스터의 Gateway API 구현체(Envoy Gateway/Istio) 확인 요청
5. 답변 반영 후 metrics-collector-dev 재개, 나머지 파이프라인은 이미 진행된 부분 유지

### 후속 작업 흐름 (부분 재실행)
1. 사용자가 "이상탐지 임계값 로직만 z-score로 바꿔줘" 요청
2. Phase 0에서 `operator/` 존재 + 특정 모듈 요청 감지 → 부분 재실행
3. `policy-engine-dev`를 서브 에이전트로 단독 호출, 기존 `policy/` 코드를 Read 후 수정
4. `qa-engineer`를 단독 호출하여 policy↔actuator 경계면만 재검증
5. 결과 보고

### Ralph Loop 흐름 (무인 반복)
1. 사용자가 `/ralph-loop`로 이 스킬을 감싸 실행 (완료 promise + max-iterations 지정)
2. iteration 1: Phase 0에서 `operator/` 미존재 → 초기 구축, Phase 5에서 qa_report.md에 FAIL 2건 발견 → Phase 6에서 promise 미출력
3. Ralph의 stop hook이 같은 프롬프트를 재주입 → iteration 2 시작
4. Phase 0에서 `_workspace/qa_report.md`의 FAIL 2건 확인 → "QA 재작업 재개" 모드로 해당 에이전트만 재호출
5. 재검증 후 FAIL이 모두 사라지면 Phase 6에서 `<promise>OPERATOR BUILD COMPLETE</promise>` 출력 → Ralph 종료
