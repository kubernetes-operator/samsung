# samsung — Kubernetes 트래픽 기반 자동운영 오퍼레이터

Pod의 CPU/메모리가 아니라 **서비스 트래픽(RPS, 에러율, 지연시간)**을 기준으로
Kubernetes를 자동 운영하는 Python [kopf](https://kopf.readthedocs.io/) 오퍼레이터.
Gateway API(HTTPRoute)를 통해 관측되는 실제 트래픽 지표로 스케일링, 장애 감지,
라우팅 제어, 이상 탐지·자동 복구를 수행한다.

## 왜 트래픽 기반인가

리소스 사용률(CPU/메모리)은 원인일 뿐 결과가 아니다 — 사용자가 실제로 겪는 것은
요청 실패율과 응답 속도다. 이 오퍼레이터는 RPS/에러율/지연시간을 1급 시민으로 삼아
판단하며, CRD 어디에도 CPU/메모리 임계값이 존재하지 않는다.

## 핵심 기능

- **트래픽 기반 스케일링**: 파드당 RPS가 목표치를 초과/미달하면 replica 조정 (스케일업/다운 임계값 분리로 flapping 방지)
- **이상 탐지**: 정적 임계값(CRD 명시값) + EWMA baseline 이탈 조합
- **라우팅 격리·복구**: 에러가 특정 backend에 집중되면 HTTPRoute weight를 낮춰 격리하고, 해소되면 점진적으로 복구
- **안전장치**: cooldown, hysteresis, 변경 폭 제한, idempotent patch, dry-run

## 구조

```
operator/
├── crds/trafficpolicy.yaml         # TrafficPolicy CRD
├── k8s_traffic_operator/
│   ├── schemas.py                  # 모듈 간 공유 계약(TrafficSnapshot/Decision/ActuationResult)
│   ├── handlers.py                 # kopf 핸들러(reconcile 배선)
│   ├── metrics/                    # 트래픽 메트릭 수집 (Envoy Gateway / Istio / nginx Gateway Fabric)
│   ├── policy/                     # 스케일링·이상탐지·복구 정책 엔진
│   ├── actuator/                   # Deployment 스케일 + HTTPRoute weight 실행기
│   └── dashboard/                  # 읽기 전용 웹 대시보드 (별도 프로세스)
├── deploy/dashboard.yaml           # 대시보드 배포 매니페스트 (ServiceAccount/RBAC/Deployment/Service)
├── Dockerfile.dashboard
└── tests/                          # pytest 회귀 테스트 (128개)
```

## 시작하기

```bash
cd operator
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 테스트 실행
./.venv/bin/pytest

# CRD 설치 (클러스터 대상)
kubectl apply -f crds/trafficpolicy.yaml

# 오퍼레이터 실행 (operator/ 디렉토리에서, 모듈 모드)
./.venv/bin/kopf run -m k8s_traffic_operator.main --verbose
```

### TrafficPolicy 예시

```yaml
apiVersion: ops.example.com/v1alpha1
kind: TrafficPolicy
metadata:
  name: checkout-traffic-policy
spec:
  target:
    httpRoute: checkout-route
    namespace: shop
    deployment: checkout-service
  thresholds:
    targetRPSPerPod: 50
    scaleDownRPSPerPod: 20
    scaleUpErrorRate: 0.05
    maxP99LatencyMs: 800
  actions:
    minReplicas: 2
    maxReplicas: 20
    cooldownSeconds: 120
    maxScaleStep: 4
    allowRouteIsolation: true
  window: "1m"
```

## 대시보드

읽기 전용 웹 UI가 `operator/k8s_traffic_operator/dashboard/`에 있다. 오퍼레이터 본체와
별도 프로세스로 동작하며 클러스터에 아무것도 쓰지 않는다(조회만).

- **`/` — TrafficPolicy 현황**: 각 CR의 phase, 마지막 판단(action/reason/severity), 실제
  적용 여부. Gateway API 트래픽 메트릭 기반 오퍼레이터 판단 결과다.
- **`/flows` — 실시간 Pod 트래픽 흐름**: Cilium Hubble에서 가져온 실제 Pod 간 L3/L4
  연결(어느 Pod가 어느 Pod와 통신했는지, allow/deny, 프로토콜, 연결 수). Gateway API
  메트릭이 없는 클러스터에서도 CNI 레벨에서 실제 트래픽을 볼 수 있다 — 단, HTTP
  요청수/에러율이 아니라 연결 단위 데이터이므로 위 TrafficPolicy 판단과는 성격이 다르다.

```bash
# 로컬에서 (현재 kubeconfig 컨텍스트 + hubble CLI 필요 시)
cd operator
./.venv/bin/uvicorn k8s_traffic_operator.dashboard.app:app --reload
# http://localhost:8000 (정책 현황), http://localhost:8000/flows (트래픽 흐름)

# 클러스터에 배포 (이미지 빌드/푸시 후 <registry> 값 교체 필요)
docker build -f Dockerfile.dashboard -t <registry>/traffic-policy-dashboard:latest .
kubectl apply -f deploy/dashboard.yaml
```

특정 네임스페이스만 보고 싶으면 `WATCH_NAMESPACE` 환경변수를 지정한다(비우면 클러스터 전체).
`/flows`는 `HUBBLE_RELAY_ADDR`(기본 `hubble-relay.kube-system.svc.cluster.local:80`)로
hubble-relay에 접속한다 — **이미지에 포함된 hubble CLI 버전이 클러스터의 Cilium 버전과
맞아야 한다**(Dockerfile.dashboard의 `HUBBLE_CLI_VERSION` 빌드 인자 참조), 버전이 다르면
"invalid fieldmask" 오류로 조회가 실패한다.

**배포 시 주의**: HTTPRoute로 외부에 연결하면 인증이 없는 상태다. 읽기 전용이라 해도
운영 환경에서는 앞단에 접근 제어(OAuth 프록시, IP 제한 등)를 추가하는 것을 권장한다.

## 지원 Gateway API 구현체

Envoy Gateway, Istio, nginx Gateway Fabric. 새 구현체는
`operator/k8s_traffic_operator/metrics/adapters/`에 어댑터를 추가하면 된다
(`base.GatewayAdapter` 참조).

## 개발 하네스

이 프로젝트는 5개 전문 에이전트(설계/메트릭/정책/실행/QA)와 오케스트레이터 스킬로
구성된 하네스로 만들어졌다. 하네스 구성과 변경 이력은 [CLAUDE.md](CLAUDE.md) 참조.
