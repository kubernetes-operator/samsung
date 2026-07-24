"""대시보드용 Hubble 흐름 요약 - 저수준 조회/파싱은 k8s_traffic_operator.hubble_client 공유.

이 모듈은 "최근 흐름을 사람이 훑어보기 좋게 집계"하는 대시보드 전용 로직만 담당한다.
정책 엔진의 스케일링 판단에 쓰는 집계(윈도우 기준 RPS/에러율 산출)는 별도 관심사이므로
metrics/hubble_collector.py에 분리되어 있다 — 같은 원본 데이터(hubble_client.fetch_flows)를
공유하되, "무엇을 위해 집계하는가"가 다르므로 집계 로직 자체는 섞지 않는다.

## 애플리케이션 트래픽 vs 인프라 트래픽 구분

클러스터의 최근 흐름은 대부분 인프라 상호작용(노드 health check, kube-system의 coredns/
metrics-server, gpu-operator의 node-feature-discovery/dcgm-exporter, velero 백업 등)이
차지한다. 단순히 "가장 빈번한 연결 쌍"을 보여주면 사용자가 실제로 운영하는 애플리케이션
Pod 트래픽이 이 노이즈에 파묻혀 보이지 않는다("내 Pod 흐름이 안 보인다"는 실제 증상).

그래서 각 흐름을 application/infrastructure로 분류하고, 대시보드는 기본적으로
애플리케이션 흐름만 부각해서 보여준다(scope="app"). 전체를 보고 싶으면 scope="all".
분류 기준:
  - endpoint가 reserved 개체(host/world/remote-node/health/kube-apiserver 등, 즉
    namespace가 없는 것)이면 인프라 쪽으로 본다.
  - endpoint의 namespace가 SYSTEM_NAMESPACES에 속하면 인프라.
  - 위 어느 쪽에도 안 걸리는, 실제 애플리케이션 네임스페이스 Pod가 한쪽 endpoint라도
    있으면 "애플리케이션 흐름"으로 본다(한쪽만 앱이어도 사용자 관심사이므로 포함).
SYSTEM_NAMESPACES는 환경변수 SYSTEM_NAMESPACES(콤마 구분)로 덮어쓸 수 있다.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..hubble_client import FlowEndpoint, FlowEvent, HubbleUnavailableError, fetch_flows

HUBBLE_LAST = int(os.getenv("HUBBLE_LAST", "300"))  # 한 번에 가져올 최근 flow 개수

# 인프라로 간주할 네임스페이스(환경변수로 덮어쓰기 가능). 클러스터 운영 컴포넌트들 —
# 사용자의 "내 애플리케이션"이 아닌 것들. 목록에 없는 네임스페이스는 애플리케이션으로 본다
# (즉 기본은 "모르면 사용자 것으로 취급" — 사용자 트래픽을 실수로 숨기지 않기 위함).
_DEFAULT_SYSTEM_NAMESPACES = {
    "kube-system", "kube-public", "kube-node-lease", "gpu-operator", "monitoring",
    "logging", "velero", "metallb-system", "cert-manager", "cilium-secrets",
    "nginx-gateway", "arc-systems", "traffic-ops-system", "traffic-policy-dashboard",
}
SYSTEM_NAMESPACES = {
    ns.strip() for ns in os.getenv("SYSTEM_NAMESPACES", "").split(",") if ns.strip()
} or _DEFAULT_SYSTEM_NAMESPACES


@dataclass
class FlowSummary:
    total: int = 0                 # 조회된 전체 흐름 수(분류 무관)
    shown: int = 0                 # 모든 필터(scope+namespace+focus) 적용 후 집계에 쓰인 흐름 수
    scope: str = "app"             # "app"(애플리케이션만) | "all"(전체)
    app_flows: int = 0             # 애플리케이션으로 분류된 흐름 수(scope 무관, 참고용)
    namespace: Optional[str] = None  # 활성 네임스페이스 필터(없으면 전체)
    focus: Optional[str] = None      # 활성 '연결 리소스' 포커스(endpoint 라벨). 이 리소스가 낀 흐름만
    # scope 적용 후(namespace/focus 적용 전) 등장하는 네임스페이스 목록 — 필터 UI용.
    namespaces: List[dict] = field(default_factory=list)  # [{name, count}]
    verdicts: Dict[str, int] = field(default_factory=dict)
    top_pairs: List[dict] = field(default_factory=list)  # [{src, dst, count, last_seen, verdict}]
    fetch_error: Optional[str] = None


def _is_system_endpoint(ep: FlowEndpoint) -> bool:
    """endpoint가 인프라 쪽인가. namespace가 없으면(reserved 개체) 인프라, 시스템 ns면 인프라."""
    if ep.namespace is None:
        return True  # host/world/remote-node/health/kube-apiserver 등
    return ep.namespace in SYSTEM_NAMESPACES


def is_application_flow(ev: FlowEvent) -> bool:
    """한쪽 endpoint라도 실제 애플리케이션 네임스페이스 Pod이면 애플리케이션 흐름으로 본다.

    양쪽이 모두 인프라(reserved 또는 시스템 ns)일 때만 인프라 흐름으로 제외한다 — 사용자
    애플리케이션이 인프라와 주고받는 트래픽(예: my-app -> kube-dns)도 사용자 관심사이므로
    한쪽만 앱이어도 포함한다.
    """
    return not (_is_system_endpoint(ev.src) and _is_system_endpoint(ev.dst))


def _flow_namespaces(ev: FlowEvent) -> set:
    """이 흐름이 걸치는 (reserved가 아닌) 네임스페이스 집합. 필터 UI 목록 산출에 쓴다."""
    return {ep.namespace for ep in (ev.src, ev.dst) if ep.namespace}


def _flow_in_namespace(ev: FlowEvent, namespace: str) -> bool:
    """한쪽 endpoint라도 해당 네임스페이스면 포함(is_application_flow와 같은 '한쪽이라도' 관점).

    네임스페이스 A를 선택하면 "A 안팎으로 오가는 모든 흐름"을 보여준다 — A→B, B→A 모두.
    """
    return ev.src.namespace == namespace or ev.dst.namespace == namespace


def _flow_touches(ev: FlowEvent, label: str) -> bool:
    """특정 리소스(endpoint 라벨)가 이 흐름의 한쪽 끝인가 — '연결된 리소스만 보기'용."""
    return ev.src.label == label or ev.dst.label == label


def summarize(
    flows: List[FlowEvent],
    top_n: int = 15,
    scope: str = "app",
    namespace: Optional[str] = None,
    focus: Optional[str] = None,
) -> FlowSummary:
    """flow 목록을 (src,dst) 쌍별 집계 + verdict 집계로 요약한다.

    필터는 3단계로 순차 적용된다:
      1) scope="app"(기본)이면 애플리케이션 흐름만 남긴다. scope="all"이면 전체.
      2) namespace가 주어지면 그 네임스페이스가 한쪽이라도 낀 흐름만 남긴다.
      3) focus(endpoint 라벨)가 주어지면 그 리소스가 한쪽 끝인 흐름만 남긴다("연결된 리소스").

    total/app_flows는 scope와 무관하게 원본 기준으로 항상 채워, 사용자가 "전체 중 앱이 몇 건"
    인지 알 수 있게 한다. namespaces 목록은 scope만 적용한 뒤(namespace/focus 적용 전) 기준으로
    산출해, 어떤 필터가 걸려 있든 필터 UI에서 다른 네임스페이스로 자유롭게 전환할 수 있게 한다.
    """
    total = len(flows)
    app_flows = sum(1 for ev in flows if is_application_flow(ev))

    scoped = flows if scope == "all" else [ev for ev in flows if is_application_flow(ev)]

    # 필터 UI가 보여줄 네임스페이스 목록(scope 적용 후, namespace/focus 적용 전) — 흐름 수 내림차순.
    ns_counter: Counter = Counter()
    for ev in scoped:
        for ns in _flow_namespaces(ev):
            ns_counter[ns] += 1
    namespaces = [
        {"name": name, "count": count}
        for name, count in sorted(ns_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    selected = scoped
    if namespace:
        selected = [ev for ev in selected if _flow_in_namespace(ev, namespace)]
    if focus:
        selected = [ev for ev in selected if _flow_touches(ev, focus)]

    verdicts: Counter = Counter()
    pair_counts: Counter = Counter()
    pair_last_seen: Dict[tuple, str] = {}
    pair_verdicts: Dict[tuple, Counter] = defaultdict(Counter)

    for ev in selected:
        verdicts[ev.verdict] += 1
        key = (ev.src.label, ev.dst.label, ev.protocol, ev.dst_port)
        pair_counts[key] += 1
        pair_verdicts[key][ev.verdict] += 1
        pair_last_seen[key] = ev.time  # 뒤에서부터 덮어써도 마지막 관측 시각으로 수렴

    top_pairs = [
        {
            "src": src, "dst": dst, "protocol": proto, "dst_port": port,
            "count": count, "last_seen": pair_last_seen[(src, dst, proto, port)],
            # 쌍별 '대표 verdict'(가장 많이 관측된 판정). 다이어그램에서 화살표 색을 이걸로 칠한다
            # — 그 연결이 대체로 정상(FORWARDED)인지 차단(DROPPED)인지 한눈에 보이게.
            "verdict": pair_verdicts[(src, dst, proto, port)].most_common(1)[0][0],
        }
        for (src, dst, proto, port), count in pair_counts.most_common(top_n)
    ]

    return FlowSummary(
        total=total,
        shown=len(selected),
        scope=scope,
        app_flows=app_flows,
        namespace=namespace,
        focus=focus,
        namespaces=namespaces,
        verdicts=dict(verdicts),
        top_pairs=top_pairs,
    )


def fetch_summary(
    last: int = HUBBLE_LAST,
    scope: str = "app",
    namespace: Optional[str] = None,
    focus: Optional[str] = None,
) -> FlowSummary:
    """대시보드가 호출하는 진입점. 실패 시 예외 대신 fetch_error가 채워진 FlowSummary를 반환한다."""
    try:
        flows = fetch_flows(last)
    except HubbleUnavailableError as exc:
        return FlowSummary(scope=scope, namespace=namespace, focus=focus, fetch_error=str(exc))
    return summarize(flows, scope=scope, namespace=namespace, focus=focus)
