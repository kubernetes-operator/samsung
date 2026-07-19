"""k8s_traffic_operator.

Kubernetes 트래픽 기반 자동운영 오퍼레이터 (Python + kopf).

Pod의 CPU/메모리가 아니라 Gateway API를 통해 관측되는 서비스 트래픽 지표
(RPS / 에러율 / 지연시간)를 기준으로 스케일링, 장애 감지, 라우팅 제어,
이상 탐지·자동 대응을 수행한다.

패키지 레이아웃:
    schemas.py   - 세 모듈이 공유하는 데이터 계약 (TrafficSnapshot, Decision)
    handlers.py  - kopf 핸들러 (@kopf.on.create/update, @kopf.timer)
    main.py      - kopf 실행 엔트리포인트
    metrics/     - metrics-collector-dev 담당: TrafficSnapshot 생성
    policy/      - policy-engine-dev 담당: Decision 산출
    actuator/    - actuator-dev 담당: Decision을 k8s 리소스 변경으로 실행
"""

__version__ = "0.1.0"

# CRD 좌표 - 세 모듈과 핸들러가 동일한 상수를 참조하도록 여기서 고정한다.
API_GROUP = "ops.example.com"
API_VERSION = "v1alpha1"
CRD_PLURAL = "trafficpolicies"
