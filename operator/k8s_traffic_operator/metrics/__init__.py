"""metrics 패키지 - metrics-collector-dev 담당.

구현해야 할 함수 시그니처 (handlers.py가 이대로 호출한다):

    # collector.py
    from ..schemas import TrafficSnapshot

    def collect(spec: dict) -> TrafficSnapshot:
        '''CRD spec을 받아 Prometheus에서 트래픽 지표를 수집·정규화하여 반환.

        - spec.target(httpRoute/namespace/deployment), spec.window를 참고한다.
        - Gateway API 구현체(Envoy Gateway/Istio 등) 특정 메트릭 이름은 이 모듈 내부에서만
          매핑하고, 반환하는 TrafficSnapshot에는 구현체 특정 필드를 넣지 않는다(중립 유지).
        - 지연시간은 ms, error_rate는 0.0~1.0 비율, rps는 req/s 단위로 정규화한다.
        - 데이터 없음은 status="no_data", 수집 실패는 status="collection_failed"로 반환.
        '''
        ...

handlers.py는 `from .metrics import collector as metrics` 후 `metrics.collect(spec)`을 호출한다.
"""
