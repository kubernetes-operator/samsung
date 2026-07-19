"""대시보드용 Hubble 흐름 요약 - 저수준 조회/파싱은 k8s_traffic_operator.hubble_client 공유.

이 모듈은 "최근 흐름을 사람이 훑어보기 좋게 집계"하는 대시보드 전용 로직만 담당한다.
정책 엔진의 스케일링 판단에 쓰는 집계(윈도우 기준 RPS/에러율 산출)는 별도 관심사이므로
metrics/hubble_collector.py에 분리되어 있다 — 같은 원본 데이터(hubble_client.fetch_flows)를
공유하되, "무엇을 위해 집계하는가"가 다르므로 집계 로직 자체는 섞지 않는다.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..hubble_client import FlowEvent, HubbleUnavailableError, fetch_flows

HUBBLE_LAST = int(os.getenv("HUBBLE_LAST", "300"))  # 한 번에 가져올 최근 flow 개수


@dataclass
class FlowSummary:
    total: int = 0
    verdicts: Dict[str, int] = field(default_factory=dict)
    top_pairs: List[dict] = field(default_factory=list)  # [{src, dst, count, last_seen}]
    fetch_error: Optional[str] = None


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
