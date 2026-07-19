"""Cilium Hubble에서 실제 Pod 간 트래픽 흐름을 가져와 요약한다.

Gateway API 메트릭(RPS/에러율/지연시간, metrics/ 패키지)과는 성격이 다르다 — 이건
CNI(Cilium) 레벨에서 관측되는 L3/L4 흐름(어느 Pod가 어느 Pod와 통신했는지, TCP/UDP,
allow/deny 여부)이다. HTTP 요청 수·지연시간이 아니라 "연결/패킷 단위 트래픽"이라는
점을 대시보드 표현에서도 명확히 구분한다 — 이 값을 RPS인 것처럼 보여주면 오해를 만든다.

이 클러스터는 별도 설정(ServiceMonitor, Envoy L7 visibility 등) 없이도 Hubble이
이미 떠 있어(enable-hubble=true) 이 방식이 즉시 동작한다 — Gateway API 메트릭 미노출
문제와 달리 클러스터 관측 인프라를 추가로 바꿀 필요가 없다.

`hubble` CLI 바이너리(공식 Cilium 프로젝트 릴리스)를 서브프로세스로 호출해
hubble-relay에 질의한다. gRPC 프로토콜을 직접 구현하지 않고 공식 CLI를 재사용하는
이유: 클러스터의 Cilium 버전과 안 맞으면 gRPC 스키마 불일치("invalid fieldmask")로
조회가 실패하는 것을 실측했다 — CLI 버전은 배포 시 클러스터의 Cilium 버전과 맞춰야 한다
(Dockerfile.dashboard의 HUBBLE_CLI_VERSION 빌드 인자 참조).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

HUBBLE_BINARY = os.getenv("HUBBLE_BINARY", "hubble")
HUBBLE_RELAY_ADDR = os.getenv("HUBBLE_RELAY_ADDR", "hubble-relay.kube-system.svc.cluster.local:80")
HUBBLE_LAST = int(os.getenv("HUBBLE_LAST", "300"))  # 한 번에 가져올 최근 flow 개수
HUBBLE_TIMEOUT_S = float(os.getenv("HUBBLE_TIMEOUT_S", "8"))

# reserved:* 로 시작하는 identity는 실제 Pod가 아니라 Cilium이 붙이는 특수 개체다.
# namespace/pod_name이 없을 때 사람이 읽을 수 있는 이름으로 대체한다.
_RESERVED_PREFIX = "reserved:"


class HubbleUnavailableError(Exception):
    """hubble CLI 실행 자체가 실패했을 때(바이너리 없음/relay 연결 실패 등)."""


@dataclass
class FlowEndpoint:
    label: str                       # 사람이 읽는 식별자: "ns/pod" 또는 "host"/"world" 등
    namespace: Optional[str] = None
    pod_name: Optional[str] = None


@dataclass
class FlowEvent:
    time: str
    verdict: str                     # FORWARDED | DROPPED | ERROR | AUDIT ...
    direction: Optional[str]         # INGRESS | EGRESS | None
    protocol: str                    # TCP | UDP | ICMP | "?"
    src: FlowEndpoint
    dst: FlowEndpoint
    dst_port: Optional[int] = None


@dataclass
class FlowSummary:
    total: int = 0
    verdicts: Dict[str, int] = field(default_factory=dict)
    top_pairs: List[dict] = field(default_factory=list)  # [{src, dst, count, last_seen}]
    fetch_error: Optional[str] = None


def _entity_label(side: dict) -> FlowEndpoint:
    """Hubble flow의 source/destination 객체를 사람이 읽는 라벨로 변환한다.

    실제 Pod면 "namespace/pod_name"을, reserved 개체(host/world/remote-node/health/
    kube-apiserver 등)면 라벨에서 "reserved:" 접두사를 뗀 이름을 쓴다. 어느 쪽도 없으면
    "unknown"으로 명시한다(값을 추측해서 채우지 않는다 — 결측치 정직 원칙).
    """
    namespace = side.get("namespace")
    pod_name = side.get("pod_name")
    if namespace and pod_name:
        return FlowEndpoint(label=f"{namespace}/{pod_name}", namespace=namespace, pod_name=pod_name)
    if pod_name:
        return FlowEndpoint(label=pod_name, pod_name=pod_name)

    labels = side.get("labels") or []
    reserved = [l[len(_RESERVED_PREFIX):] for l in labels if l.startswith(_RESERVED_PREFIX)]
    if reserved:
        return FlowEndpoint(label="+".join(sorted(set(reserved))))
    return FlowEndpoint(label="unknown")


def _parse_flow_line(line: str) -> Optional[FlowEvent]:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    flow = payload.get("flow")
    if not flow:
        return None  # hubble observe는 LostEvent 등 flow가 없는 라인도 섞어 보낸다.

    l4 = flow.get("l4") or {}
    protocol, dst_port = "?", None
    if "TCP" in l4:
        protocol, dst_port = "TCP", l4["TCP"].get("destination_port")
    elif "UDP" in l4:
        protocol, dst_port = "UDP", l4["UDP"].get("destination_port")
    elif "ICMPv4" in l4 or "ICMPv6" in l4:
        protocol = "ICMP"

    return FlowEvent(
        time=flow.get("time", ""),
        verdict=flow.get("verdict", "UNKNOWN"),
        direction=flow.get("traffic_direction"),
        protocol=protocol,
        src=_entity_label(flow.get("source") or {}),
        dst=_entity_label(flow.get("destination") or {}),
        dst_port=dst_port,
    )


def fetch_flows(last: int = HUBBLE_LAST) -> List[FlowEvent]:
    """hubble CLI로 최근 flow를 가져와 파싱한다.

    실패(바이너리 없음, relay 연결 불가, 타임아웃)는 삼키지 않고 HubbleUnavailableError로
    올린다 — 상위(summarize/대시보드)가 "흐름 없음"과 "조회 자체 실패"를 구분해야 한다.
    """
    cmd = [HUBBLE_BINARY, "observe", "--server", HUBBLE_RELAY_ADDR, "--last", str(last), "-o", "jsonpb"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=HUBBLE_TIMEOUT_S, check=False,
        )
    except FileNotFoundError as exc:
        raise HubbleUnavailableError(f"hubble CLI를 찾을 수 없음({HUBBLE_BINARY}): {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HubbleUnavailableError(f"hubble 조회 타임아웃({HUBBLE_TIMEOUT_S}s): {exc}") from exc

    if result.returncode != 0 and not result.stdout.strip():
        raise HubbleUnavailableError(
            f"hubble observe 실패(exit={result.returncode}): {result.stderr.strip()[:300]}"
        )

    events = [e for e in (_parse_flow_line(l) for l in result.stdout.splitlines()) if e is not None]
    return events


def summarize(flows: List[FlowEvent], top_n: int = 15) -> FlowSummary:
    """flow 목록을 (src,dst) 쌍별 집계 + verdict 집계로 요약한다."""
    verdicts: Counter = Counter()
    pair_counts: Counter = Counter()
    pair_last_seen: Dict[tuple, str] = {}

    for ev in flows:
        verdicts[ev.verdict] += 1
        key = (ev.src.label, ev.dst.label, ev.protocol, ev.dst_port)
        pair_counts[key] += 1
        pair_last_seen[key] = ev.time  # 뒤에서부터 덮어써도 마지막 관측 시각으로 수렴

    top_pairs = [
        {
            "src": src, "dst": dst, "protocol": proto, "dst_port": port,
            "count": count, "last_seen": pair_last_seen[(src, dst, proto, port)],
        }
        for (src, dst, proto, port), count in pair_counts.most_common(top_n)
    ]

    return FlowSummary(total=len(flows), verdicts=dict(verdicts), top_pairs=top_pairs)


def fetch_summary(last: int = HUBBLE_LAST) -> FlowSummary:
    """대시보드가 호출하는 진입점. 실패 시 예외 대신 fetch_error가 채워진 FlowSummary를 반환한다."""
    try:
        flows = fetch_flows(last)
    except HubbleUnavailableError as exc:
        return FlowSummary(fetch_error=str(exc))
    return summarize(flows)
