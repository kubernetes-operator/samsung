"""actuator 패키지 - actuator-dev 담당.

구현해야 할 함수 시그니처 (handlers.py가 이대로 호출한다):

    # executor.py
    from ..schemas import ActuationResult, Decision

    def apply(spec: dict, decision: Decision) -> ActuationResult:
        '''Decision을 실제 Kubernetes / Gateway API 리소스 변경으로 실행.

        - action == "noop"            : 아무 것도 하지 않고 applied=False 반환.
        - action == "scale"           : Deployment(spec.target.deployment) replica를
                                        decision.target_replicas로 패치. 단, 반드시
                                        spec.actions.minReplicas ~ maxReplicas로 clamp하고,
                                        변경폭 제한(급격한 스케일 방지)을 적용한다.
        - action == "reroute"/"isolate_backend" : HTTPRoute(spec.target.httpRoute)의
                                        backendRefs weight를 decision.backend_weights로 패치.
                                        allowRouteIsolation=false면 격리를 거부(applied=False).
        - dry-run 지원: 실제 변경 전 계획을 로깅하고, dry-run 모드면 applied=False,
          dry_run=True로 반환한다.
        - 실패 시 error 필드에 메시지를 담아 반환(예외를 밖으로 던지지 말 것 — timer 안정성).
        - kubernetes python client를 사용한다.
        '''
        ...

handlers.py는 `from .actuator import executor as actuator` 후 `actuator.apply(spec, decision)`을 호출한다.
"""
