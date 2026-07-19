"""policy 패키지 - policy-engine-dev 담당.

구현해야 할 함수 시그니처 (handlers.py가 이대로 호출한다):

    # engine.py
    from ..schemas import Decision, TrafficSnapshot

    def evaluate(spec: dict, snapshot: TrafficSnapshot, status: dict) -> Decision:
        '''TrafficSnapshot을 입력받아 스케일/라우팅/격리 Decision을 산출.

        - snapshot.status == "ok"인 경우에만 호출된다(handlers가 보장). 단, 방어적으로
          한 번 더 확인하는 것을 권장한다.
        - spec.thresholds(targetRPSPerPod, scaleUpErrorRate, scaleDownRPSPerPod,
          maxP99LatencyMs)를 소비하여 판단한다.
        - hysteresis: 스케일업 임계값과 스케일다운 임계값(scaleDownRPSPerPod)을 분리해
          flapping을 방지한다.
        - cooldown: spec.actions.cooldownSeconds 동안 재행동을 억제한다. 직전 결정 시각은
          status.lastReconcileAt / status.lastDecision 에서 참조할 수 있다. cooldown 중이면
          action="noop", reason에 cooldown 사유를 남긴다.
        - 이상 탐지(EWMA/z-score)를 수행하면 severity/anomaly_score 필드를 채운다.
        - target_replicas는 절대값으로 산출한다(actuator가 min/max clamp를 최종 적용).
        '''
        ...

handlers.py는 `from .policy import engine as policy` 후 `policy.evaluate(spec, snapshot, status)`을 호출한다.
"""
