---
name: traffic-policy-engine
description: "TrafficSnapshot(RPS/에러율/지연시간)을 입력받아 스케일링·이상탐지·자동대응 Decision을 산출하는 정책 로직을 설계·구현한다. EWMA/z-score 이상탐지, hysteresis, cooldown, flapping 방지 로직 작성 요청 시 사용."
---

# 트래픽 기반 정책 엔진 설계

트래픽 지표를 "무엇을 할지"로 변환하는 의사결정 로직 작성 절차.

## 왜 임계값만으로는 부족한가

단순 임계값(예: "RPS > 100이면 스케일업")은 순간 스파이크에도 반응해 flapping을 일으킨다. 이 오퍼레이터는 트래픽이라는, 본질적으로 노이즈가 많은 신호를 다루므로 **추세 기반 판단 + 이력 상태(hysteresis, cooldown)**가 구조적으로 필요하다.

## 스케일링 로직

트래픽량 기반 목표 replica 계산의 핵심 공식:

```python
target_replicas = ceil(current_total_rps / target_rps_per_pod)
target_replicas = clamp(target_replicas, min_replicas, max_replicas)
```

`current_total_rps`는 순간값이 아니라 CRD의 관측 윈도우로 집계된 값(`TrafficSnapshot.rps`)을 사용한다. 스케일업/다운 임계값을 CRD에서 분리(`scaleUpErrorRate`/`scaleDownRPSPerPod`처럼 서로 다른 필드)해 두었다면, 목표 replica가 현재보다 클 때만 스케일업 임계값을, 작을 때만 스케일다운 임계값을 적용한다 — 이것이 hysteresis다. 같은 임계값을 양방향에 쓰면 목표 replica가 경계값 근처에서 진동한다.

## 이상 탐지 알고리즘 선택

| 알고리즘 | 적합한 경우 | 특징 |
|---------|-----------|------|
| **정적 임계값** | CRD에 명시적 상한(예: `maxErrorRate: 0.05`)이 있을 때 | 단순, 예측 가능. 이 오퍼레이터의 1차 방어선 |
| **EWMA + 표준편차 밴드** | 트래픽 패턴에 일/주 단위 계절성이 있고 "평소 대비 이상"을 잡고 싶을 때 | baseline이 서서히 적응, 급격한 변화에 민감 |
| **z-score (슬라이딩 윈도우)** | 짧은 시간 내 급변(스파이크)을 빠르게 잡고 싶을 때 | 윈도우가 짧으면 민감, 데이터 부족 시 불안정 |

**권장 조합:** 정적 임계값(CRD 명시값)을 1차 게이트로, EWMA baseline 이탈을 2차 신호로 사용한다. 두 신호가 모두 이상을 가리킬 때만 라우팅 격리 같은 강한 조치를 취하고, 하나만 이상이면 스케일업 정도로 약하게 대응한다 — 오탐으로 인한 과잉 대응을 줄인다.

```python
def is_anomalous(snapshot, baseline) -> bool:
    static_breach = snapshot.error_rate > spec.thresholds.scale_up_error_rate
    ewma_breach = abs(snapshot.error_rate - baseline.ewma) > 3 * baseline.stddev
    return static_breach and ewma_breach
```

baseline이 아직 충분히 쌓이지 않은 초기 구간(신규 배포 직후 등)에서는 EWMA 신호를 무시하고 정적 임계값만으로 판단한다 — 데이터 부족 상태에서 통계적 이상탐지는 신뢰할 수 없다.

## Cooldown 강제

```python
def evaluate(spec, snapshot, status) -> Decision:
    last_action_time = status.get("lastActionTime")
    if last_action_time and (now() - last_action_time) < spec.actions.cooldown_seconds:
        return Decision(action="noop", reason="cooldown 기간 중")
    ...
```

cooldown은 CRD 필드이므로 임의로 생략하거나 정책 엔진 내부에 다른 값으로 하드코딩하지 않는다. `status`(kopf가 CR에 저장하는 상태)에 마지막 액션 시각을 기록해 재조회한다.

## Decision 우선순위

여러 이상 신호가 동시에 감지되면 더 안전한 방향(트래픽을 줄이는 쪽)을 우선한다:

1. 에러율 이상 + 특정 backend에 집중 → `isolate_backend` (해당 backend weight 하향)
2. 에러율 이상이나 backend 특정 불가(전체적 문제) → `scale` (replica 증설로 부하 분산)
3. RPS만 증가, 에러/지연 정상 → `scale`
4. 모든 지표 정상 또는 `no_data`/`collection_failed` → `noop`

## Decision에 근거를 남긴다

```python
Decision(
    action="scale",
    reason=f"RPS {snapshot.rps:.1f} > target {target_rps_per_pod * current_replicas:.1f} "
           f"(target_rps_per_pod={target_rps_per_pod})",
    target_replicas=target_replicas,
)
```

`reason`은 운영자가 나중에 "왜 스케일업이 일어났는가"를 로그만으로 이해할 수 있게 하는 필수 필드다 — 생략하지 않는다.
