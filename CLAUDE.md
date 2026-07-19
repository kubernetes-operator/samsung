## 하네스: Kubernetes 트래픽 기반 자동운영 오퍼레이터

**목표:** Pod의 CPU/메모리가 아닌 서비스 트래픽(RPS/에러율/지연시간, Gateway API 기반)을 기준으로 스케일링·장애감지·라우팅제어·이상탐지를 자동 수행하는 Python kopf 오퍼레이터를 5인 에이전트 팀으로 구축·유지보수한다.

**트리거:** 이 오퍼레이터 관련 작업 요청 시 `k8s-traffic-ops-orchestrator` 스킬을 사용하라. 단순 질문은 직접 응답 가능.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-07-19 | 초기 구성 (5인 팀: operator-architect, metrics-collector-dev, policy-engine-dev, actuator-dev, qa-engineer + 오케스트레이터) | 전체 | 신규 프로젝트, Python+kopf / Gateway API 트래픽 지표 기반으로 결정 |
| 2026-07-19 | Ralph Loop 연동 추가 — QA 전체 PASS 시 `<promise>` 출력, Phase 0에 "QA 재작업 재개" 분기 추가 | skills/k8s-traffic-ops-orchestrator | ralph-loop 플러그인으로 QA PASS까지 무인 반복 실행을 지원하기 위함 |
| 2026-07-19 | 오퍼레이터 최초 빌드 완료 (`operator/` 전체, ~2,100줄) — QA 5개 경계면 전부 PASS | operator/ | TeamCreate 미지원 환경이라 서브 에이전트 모드로 실행(architect 순차 → metrics/policy/actuator 병렬 → QA). FINDING-1: policy가 `reroute` action을 생성하지 않아 CRD/스키마의 reroute(카나리) 기능이 미구현 상태 — 후속 결정 필요 |
| 2026-07-19 | FINDING-1 해결 — policy/engine.py에 isolate_backend 복구용 reroute 로직 추가, 구현 중 발견된 복구 조기중단/cooldown 상호작용 버그 2건 동시 수정 | operator/k8s_traffic_operator/policy/engine.py | 사용자가 "지금 policy에 구현" 선택. reroute 없이는 격리된 backend가 영구히 낮은 weight에 머무는 결함이었음 |
| 2026-07-19 | nginx Gateway Fabric 어댑터 추가 (코드 구현 + mock 검증) | operator/.../metrics/adapters/nginx_gateway_fabric.py | 실제 검증 시도 중 이 프로젝트가 연결된 클러스터의 실제 Gateway API 구현체가 nginx Gateway Fabric임을 확인(기존엔 Envoy Gateway/Istio만 지원). 단, 해당 클러스터엔 stub_status/메트릭 노출이 없어 실제 E2E는 미검증 — 사용자 결정으로 클러스터에 리소스는 생성하지 않음. NGINX Plus API는 latency percentile 미제공이라 p95/p99는 의도적으로 None |
| 2026-07-19 | pytest 정식 회귀 테스트 스위트 추가 (10개 파일, 116개 테스트) | operator/tests/, operator/pytest.ini, operator/.venv | 지금까지 인라인 스크립트로 임시 검증했던 것(reroute 복구 버그 2건 포함)을 정식 회귀 테스트로 고정. kubernetes client는 MagicMock, Prometheus는 PrometheusClient의 session 주입 지점으로 mock. 실제 클러스터/kopf 이벤트 루프 E2E는 여전히 범위 밖 |
| 2026-07-19 | 실제 클러스터 E2E 검증 수행 후 정리 — 전용 네임스페이스(traffic-ops-test)에 CRD/더미 backend/HTTPRoute/최소권한 RBAC/CR을 실배포, SA 자격증명으로 kopf 실행하여 collection_failed/no_data 안전 경로 확인, 합성 Decision으로 actuator의 실제 scale/reroute 쓰기 경로(변경폭 clamp·idempotent 포함) 검증. 검증 후 전부 삭제 | (클러스터, 저장소 변경 없음) | 사용자 요청으로 실제 배포 검증. 클러스터는 nginx Gateway Fabric이 트래픽 메트릭을 노출하지 않아 정책 로직 자체는 발동되지 않았지만, kopf reconcile 배선과 actuator 쓰기 경로는 실제 API 서버 대상으로 확인됨 |
| 2026-07-19 | 읽기 전용 웹 대시보드 추가 (`dashboard/`) | operator/k8s_traffic_operator/dashboard/, operator/deploy/dashboard.yaml, operator/Dockerfile.dashboard | 사용자가 오퍼레이터 운영 현황을 웹페이지로 보고 싶어함. FastAPI 기반, 클러스터엔 get/list/watch만(쓰기 없음), 오퍼레이터 본체와 별도 프로세스로 배포. 12개 테스트 추가(총 128개) |
| 2026-07-19 | 대시보드를 실제 클러스터에 배포 (`traffic-policy-dashboard` 네임스페이스, 내부 레지스트리 registry.local.cloud:5000 사용) | 클러스터 | 사용자 요청. `https://test2.studiobasa.com/traffic-dashboard/`로 실제 브라우저 접속 확인(기존 앱들과 같은 호스트명 관례 사용, 전용 서브도메인은 DNS에 없어 실패했었음 — HTTPRoute에 URLRewrite 필터 추가로 prefix 제거 후 정상 동작). 인증 없이 공개 노출된 상태이니 운영 시 접근 제어 추가 필요 |
| 2026-07-19 | CRD/실제 TrafficPolicy CR 재배포 후 테스트 — kopf 재실행으로 실제 reconcile 확인, 대시보드에 실제 반영됨을 확인 | 클러스터 (전과 동일 traffic-ops-test) | 사용자 요청. 대시보드에 traffic-ops-test(합성 테스트 앱)만 보이는 것에 대해 사용자가 문제 제기 → 이 클러스터는 Prometheus가 애플리케이션 네임스페이스를 아예 스크랩하지 않는다는 추가 한계도 발견됨(Gateway 메트릭 미노출과 별개) |
| 2026-07-19 | Hubble(Cilium CNI) 기반 실시간 Pod 트래픽 흐름 대시보드 추가 (`/flows`) | operator/k8s_traffic_operator/dashboard/hubble_flows.py, app.py, Dockerfile.dashboard, deploy/dashboard.yaml | 사용자가 "Gateway API와 CNI를 통해서 Pod 트래픽 상황을 모니터링"을 요청. 이 클러스터에 Cilium/Hubble이 이미 떠 있어(enable-hubble=true) 별도 인프라 변경 없이 실제 L3/L4 Pod 트래픽 흐름 관측 가능함을 실측 확인. hubble CLI를 서브프로세스로 호출(버전이 클러스터 Cilium과 안 맞으면 "invalid fieldmask"로 실패 — Dockerfile의 HUBBLE_CLI_VERSION으로 고정). 사용자 결정으로 범위는 시각화만(정책 엔진의 스케일링 판단에는 아직 연결 안 함). 22개 테스트 추가(총 150개) |
| 2026-07-19 | Hubble을 정책 엔진 스케일링 판단에도 연결 (`GATEWAY_IMPLEMENTATION=cilium-hubble`) | operator/k8s_traffic_operator/hubble_client.py(신규, dashboard와 공유), metrics/hubble_collector.py(신규), metrics/collector.py | 사용자 요청으로 시각화 전용에서 확장. rps=연결수/윈도우초(HTTP 요청 아님), error_rate=비FORWARDED 비율(HTTP 5xx 아님)로 명확히 구분 문서화. latency/per_backend는 의도적으로 미제공(isolate_backend/reroute는 이 소스로 트리거 안 됨 — backendRef 매핑 불확실성으로 인한 안전 설계). traffic-ops-test에 실제 트래픽 발생시켜 collect()→policy.evaluate() 전체 파이프라인 실제 클러스터 검증 완료. 18개 테스트 추가(총 168개) |
