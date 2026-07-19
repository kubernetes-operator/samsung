"""공유 데이터 계약 (interface contract).

이 파일은 operator-architect가 소유한다. metrics-collector-dev / policy-engine-dev /
actuator-dev 세 팀은 각자 스키마를 따로 정의하지 말고 **반드시 이 파일에서 import**하여
사용한다. 경계면(boundary) 버그의 대부분은 필드명·타입·단위 불일치에서 발생하므로,
아래 주석에 고정된 단위와 값의 범위를 준수한다.

데이터 흐름:
    metrics.collect(spec)          -> TrafficSnapshot
    policy.evaluate(spec, snap, s) -> Decision
    actuator.apply(spec, decision) -> ActuationResult

단위 고정 (전 모듈 공통, 위반 금지):
    - 지연시간(latency)  : 밀리초(ms), float
    - 에러율(error_rate) : 0.0 ~ 1.0 비율(fraction), float.  퍼센트(%) 아님.
    - RPS               : requests per second, float
    - 시간 간격/윈도우   : 초(seconds), int 또는 float
    - 가중치(weight)     : 0 ~ 100 정수. Gateway API HTTPRoute backendRef weight 규약.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Literal, Optional

# ---------------------------------------------------------------------------
# Literal 타입 별칭 - 세 모듈이 문자열을 오타 없이 공유하도록 한 곳에 고정한다.
# ---------------------------------------------------------------------------

# TrafficSnapshot 수집 결과 상태.
#   "ok"                : 유효한 트래픽 데이터가 채워짐
#   "no_data"           : 쿼리는 성공했으나 대상 트래픽이 없음(신규 배포/유휴). 지표 필드는 None.
#   "collection_failed" : Prometheus 접근 실패 등 수집 자체가 실패. policy는 noop 처리 권장.
SnapshotStatus = Literal["ok", "no_data", "collection_failed"]

# 이상 탐지 심각도. policy-engine이 산출, actuator가 대응 강도 판단에 참고.
Severity = Literal["none", "warning", "critical"]

# Decision이 지시하는 액션 종류.
#   "noop"            : 아무 것도 하지 않음 (임계값 내 정상 / 데이터 없음 / cooldown 중)
#   "scale"           : Deployment replica 조정. target_replicas 필수.
#   "reroute"         : HTTPRoute backendRef 가중치 조정(카나리/트래픽 시프트). backend_weights 필수.
#   "isolate_backend" : 이상 backend의 weight를 낮춰 격리(서킷브레이커성). backend_weights 필수.
ActionType = Literal["noop", "scale", "reroute", "isolate_backend"]


# ---------------------------------------------------------------------------
# metrics-collector-dev  ->  produces
# policy-engine-dev      ->  consumes
# ---------------------------------------------------------------------------

@dataclass
class BackendTraffic:
    """단일 backend(파드 그룹/서비스)의 트래픽 지표.

    per-backend 라우팅 격리(isolate_backend) 판단에 사용된다. 전체 합산 지표만으로는
    "어느 backend가 문제인지"를 알 수 없으므로 backend별로 분해한 값을 함께 제공한다.
    """

    name: str                              # backend 식별자. HTTPRoute backendRef 이름과 동일해야 actuator가 매칭 가능.
    rps: Optional[float] = None            # 이 backend의 초당 요청 수 (req/s)
    error_rate: Optional[float] = None     # 이 backend의 에러율 (0.0~1.0)
    p99_latency_ms: Optional[float] = None # 이 backend의 p99 지연시간 (ms)
    ready_pods: Optional[int] = None       # 현재 Ready 상태 파드 수. RPS/pod 계산의 분모. 없으면 None.


@dataclass
class TrafficSnapshot:
    """한 시점의 트래픽 관측 스냅샷. 구현체 중립(implementation-neutral).

    Envoy Gateway / Istio 등 어떤 Gateway API 구현체를 쓰더라도 metrics 모듈이
    이 공통 형태로 정규화(normalize)한다. 구현체 특정 필드(예: envoy_cluster,
    istio_revision)는 여기에 넣지 않는다 — 그런 값은 metrics 모듈 내부에만 존재한다.

    지표 필드(rps, error_rate, *_latency_ms)는 status가 "ok"일 때만 채워지며,
    "no_data"/"collection_failed"일 때는 None이다. policy 모듈은 반드시 status를
    먼저 확인하고, None 지표에 산술을 적용하지 않는다.
    """

    status: SnapshotStatus                 # "ok" | "no_data" | "collection_failed" 먼저 확인할 것.
    timestamp: float                       # 수집 시각. Unix epoch 초(seconds), UTC. time.time() 값.
    window_seconds: int                    # 이 스냅샷이 집계한 관측 윈도우 길이(초). CRD spec.window에서 파생.

    # --- 전체 backend 합산(aggregate) 지표 ---
    rps: Optional[float] = None            # 전체 초당 요청 수 합산 (req/s)
    error_rate: Optional[float] = None     # 전체 에러율 (0.0~1.0). 5xx / 전체요청. 퍼센트 아님.
    p50_latency_ms: Optional[float] = None # 전체 p50 지연시간 (ms)
    p95_latency_ms: Optional[float] = None # 전체 p95 지연시간 (ms)
    p99_latency_ms: Optional[float] = None # 전체 p99 지연시간 (ms)

    total_ready_pods: Optional[int] = None # 대상 Deployment의 현재 Ready 파드 수. RPS/pod 계산 분모.

    # --- per-backend 분해 지표 (격리 판단용) ---
    per_backend: List[BackendTraffic] = field(default_factory=list)

    # 진단용 자유 메타데이터. 어떤 구현체/쿼리에서 왔는지 등. policy/actuator는 로직에 쓰지 말 것(로깅만).
    meta: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# policy-engine-dev  ->  produces
# actuator-dev       ->  consumes  (그리고 handlers가 status에 기록)
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """policy 엔진의 판단 결과. actuator가 소비하여 실제 리소스를 변경한다.

    핵심 규약:
      - action == "noop"            : target_replicas / backend_weights 무시됨.
      - action == "scale"           : target_replicas 필수(None 금지). backend_weights는 None.
      - action == "reroute"         : backend_weights 필수. target_replicas는 None.
      - action == "isolate_backend" : backend_weights 필수(격리 대상 backend의 weight를 낮춘 맵).

    actuator는 이 Decision을 신뢰하되, 자체 안전장치(min/max replicas clamp,
    변경폭 제한, dry-run)를 반드시 한 번 더 적용한다. Decision은 "의도"이고
    actuator의 안전장치는 "최종 방어선"이다.
    """

    action: ActionType                     # "noop" | "scale" | "reroute" | "isolate_backend"
    reason: str                            # 사람이 읽는 판단 근거. 예: "p99=920ms > maxP99LatencyMs=800". status에 기록됨.

    # --- scale 액션 ---
    target_replicas: Optional[int] = None  # 목표 replica 수(절대값). actuator가 min/max로 clamp한다.

    # --- reroute / isolate_backend 액션 ---
    # {backend_name: weight(0~100 정수)}. HTTPRoute backendRefs weight 규약을 따른다.
    # 모든 backend의 합이 반드시 100일 필요는 없다(Gateway API는 상대 가중치). actuator가 검증.
    backend_weights: Optional[Dict[str, int]] = None

    # --- 이상 탐지 부가 정보 (policy가 채우고, actuator/status가 참고) ---
    severity: Severity = "none"            # "none" | "warning" | "critical"
    anomaly_score: Optional[float] = None  # 이상 점수(예: z-score). 해석은 policy 소유. actuator는 참고만.

    # flapping 방지용: 이 결정이 적용되면 다음 cooldown 종료 시각(Unix epoch 초).
    # policy가 cooldownSeconds를 반영해 계산해 넣을 수 있고, actuator/handlers가 존중한다. 선택적.
    cooldown_until: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# actuator-dev  ->  produces  (handlers가 status/이벤트 기록용으로 소비)
# ---------------------------------------------------------------------------

@dataclass
class ActuationResult:
    """actuator.apply()의 결과. handlers가 kopf status에 기록하고 로깅한다.

    dry-run 모드에서도 이 객체를 반환하되 applied=False, dry_run=True로 채운다.
    """

    applied: bool                          # 실제로 클러스터 리소스가 변경되었는가.
    action: ActionType                     # 처리한 Decision.action 에코백.
    detail: str = ""                       # 무엇을 바꿨는지 사람이 읽는 요약. 예: "replicas 4 -> 6".
    dry_run: bool = False                  # dry-run 모드였는지.
    error: Optional[str] = None            # 실행 실패 시 메시지. 성공이면 None.

    def to_dict(self) -> dict:
        return asdict(self)
