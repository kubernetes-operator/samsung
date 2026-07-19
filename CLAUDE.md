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
