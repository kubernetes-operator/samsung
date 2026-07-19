"""Cilium Hubble 저수준 클라이언트 - dashboard와 metrics(정책 엔진 스케일링 판단) 양쪽이 공유.

`hubble` CLI(공식 Cilium 프로젝트 릴리스)를 서브프로세스로 호출해 hubble-relay에 질의한다.
gRPC 프로토콜을 직접 구현하지 않고 공식 CLI를 재사용하는 이유: 클러스터의 Cilium 버전과
안 맞으면 gRPC 스키마 불일치("invalid fieldmask")로 조회가 실패하는 것을 실측했다 —
CLI 버전은 배포 시 클러스터의 Cilium 버전과 맞춰야 한다(Dockerfile.dashboard의
HUBBLE_CLI_VERSION 빌드 인자 참조).

이 모듈이 주는 것은 Gateway API 메트릭(RPS/에러율/지연시간, metrics/adapters/)과 성격이
다르다 — CNI 레벨에서 관측되는 L3/L4 흐름(어느 Pod가 어느 Pod와 통신했는지, TCP/UDP,
allow/deny)이다. HTTP 요청 수·지연시간이 아니라 "연결 단위 트래픽"이라는 점을 소비자
양쪽(dashboard/hubble_flows.py, metrics/hubble_collector.py) 모두에서 명확히 구분해서 다룬다.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

HUBBLE_BINARY = os.getenv("HUBBLE_BINARY", "hubble")
HUBBLE_RELAY_ADDR = os.getenv("HUBBLE_RELAY_ADDR", "hubble-relay.kube-system.svc.cluster.local:80")
HUBBLE_TIMEOUT_S = float(os.getenv("HUBBLE_TIMEOUT_S", "10"))

# reserved:* 로 시작하는 identity는 실제 Pod가 아니라 Cilium이 붙이는 특수 개체다.
_RESERVED_PREFIX = "reserved:"


class HubbleUnavailableError(Exception):
    """hubble CLI 실행 자체가 실패했을 때(바이너리 없음/relay 연결 실패/타임아웃 등)."""


@dataclass
class FlowEndpoint:
    label: str                       # 사람이 읽는 식별자: "ns/pod" 또는 "host"/"world" 등
    namespace: Optional[str] = None
    pod_name: Optional[str] = None
    workload: Optional[str] = None   # Deployment/DaemonSet 등 워크로드 이름(있으면)


@dataclass
class FlowEvent:
    time: str                        # RFC3339 원본 문자열(표시용)
    epoch: Optional[float]           # 파싱된 Unix epoch 초(집계/윈도우 필터링용). 파싱 실패 시 None
    verdict: str                     # FORWARDED | DROPPED | ERROR | AUDIT ...
    direction: Optional[str]         # INGRESS | EGRESS | None
    protocol: str                    # TCP | UDP | ICMP | "?"
    src: FlowEndpoint
    dst: FlowEndpoint
    dst_port: Optional[int] = None


def _entity_label(side: dict) -> FlowEndpoint:
    """Hubble flow의 source/destination 객체를 사람이 읽는 라벨로 변환한다.

    실제 Pod면 "namespace/pod_name"을, reserved 개체(host/world/remote-node/health/
    kube-apiserver 등)면 라벨에서 "reserved:" 접두사를 뗀 이름을 쓴다. 어느 쪽도 없으면
    "unknown"으로 명시한다(값을 추측해서 채우지 않는다 — 결측치 정직 원칙).
    """
    namespace = side.get("namespace")
    pod_name = side.get("pod_name")
    workloads = side.get("workloads") or []
    workload = workloads[0].get("name") if workloads else None

    if namespace and pod_name:
        return FlowEndpoint(label=f"{namespace}/{pod_name}", namespace=namespace, pod_name=pod_name, workload=workload)
    if pod_name:
        return FlowEndpoint(label=pod_name, pod_name=pod_name, workload=workload)

    labels = side.get("labels") or []
    reserved = [l[len(_RESERVED_PREFIX):] for l in labels if l.startswith(_RESERVED_PREFIX)]
    if reserved:
        return FlowEndpoint(label="+".join(sorted(set(reserved))))
    return FlowEndpoint(label="unknown")


def _parse_flow_time(raw: str) -> Optional[float]:
    """Hubble의 RFC3339(나노초 포함) 시각 문자열을 Unix epoch 초(float)로 변환한다.

    예: "2026-07-19T13:58:07.035205452Z". datetime.fromisoformat은 마이크로초(6자리)
    까지만 지원하므로 나노초는 6자리로 잘라낸다. 파싱 실패는 None(집계에서 이 이벤트의
    시각을 신뢰하지 않고 제외 — 억지로 끼워맞추지 않는다).
    """
    if not raw:
        return None
    try:
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "." in s:
            main, _, tail = s.partition(".")
            digits = ""
            idx = 0
            while idx < len(tail) and tail[idx].isdigit():
                digits += tail[idx]
                idx += 1
            tz = tail[idx:]
            s = f"{main}.{digits[:6].ljust(6, '0')}{tz}"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, IndexError):
        return None


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

    time_str = flow.get("time", "")
    return FlowEvent(
        time=time_str,
        epoch=_parse_flow_time(time_str),
        verdict=flow.get("verdict", "UNKNOWN"),
        direction=flow.get("traffic_direction"),
        protocol=protocol,
        src=_entity_label(flow.get("source") or {}),
        dst=_entity_label(flow.get("destination") or {}),
        dst_port=dst_port,
    )


def fetch_flows(last: int, extra_args: Optional[Sequence[str]] = None) -> List[FlowEvent]:
    """hubble CLI로 최근 flow를 가져와 파싱한다.

    extra_args로 `--to-namespace`, `--to-workload`, `--verdict` 등 hubble observe의
    필터 플래그를 그대로 전달할 수 있다(relay 단에서 걸러지므로 대량의 무관한 흐름을
    Python으로 내려받아 거르는 것보다 효율적이다).

    실패(바이너리 없음, relay 연결 불가, 타임아웃)는 삼키지 않고 HubbleUnavailableError로
    올린다 — 상위(정책 엔진/대시보드)가 "흐름 없음"과 "조회 자체 실패"를 구분해야 한다.
    """
    cmd = [HUBBLE_BINARY, "observe", "--server", HUBBLE_RELAY_ADDR, "--last", str(last), "-o", "jsonpb"]
    if extra_args:
        cmd.extend(extra_args)
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
