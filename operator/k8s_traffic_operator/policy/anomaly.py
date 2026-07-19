"""이상 탐지 - 정적 임계값(1차) + EWMA baseline 이탈(2차) 조합.

권장 조합 (skill: traffic-policy-engine):
    - 정적 임계값(CRD scaleUpErrorRate / maxP99LatencyMs)을 1차 게이트로,
    - EWMA baseline 이탈을 2차 신호로 사용한다.
    - 두 신호가 모두 이상을 가리키면(critical) 라우팅 격리 같은 강한 조치,
      하나만 이상이면(warning) 스케일업 정도의 약한 대응 -> 오탐 과잉대응 억제.

warmup 미완료(신규 배포 직후 등)에는 EWMA 신호를 무시하고 정적 임계값만으로 판단한다
— 데이터 부족 상태에서 통계적 이상탐지는 신뢰할 수 없다. 이 경우 최대 심각도는 warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..schemas import TrafficSnapshot
from .baseline import Baseline


@dataclass
class AnomalyAssessment:
    """이상 탐지 결과. engine이 severity/anomaly_score/대응 선택에 사용한다."""

    severity: str = "none"                          # "none" | "warning" | "critical"
    score: Optional[float] = None                   # 상방 z-score의 최댓값(warmup 전이면 None).
    error_static: bool = False                      # error_rate > scaleUpErrorRate
    error_ewma: bool = False                        # error_rate baseline 상방 이탈
    latency_static: bool = False                    # p99 > maxP99LatencyMs
    latency_ewma: bool = False                      # p99 baseline 상방 이탈
    culprit_backends: List[str] = field(default_factory=list)  # 에러 집중된 backend(격리 후보)
    reasons: List[str] = field(default_factory=list)           # 근거 조각들

    @property
    def error_anomaly(self) -> bool:
        return self.error_static or self.error_ewma

    @property
    def latency_anomaly(self) -> bool:
        return self.latency_static or self.latency_ewma

    @property
    def any_anomaly(self) -> bool:
        return self.error_anomaly or self.latency_anomaly

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "이상 없음"


def _find_culprit_backends(
    snapshot: TrafficSnapshot,
    scale_up_error_rate: Optional[float],
) -> List[str]:
    """에러가 특정 backend에 집중되었는지 판단해 격리 후보를 반환한다.

    조건: 에러율이 임계값을 초과하는 backend가 존재하고, 동시에 임계값 이하로 건강한
    backend도 하나 이상 존재할 때만 "집중(격리 가능)"으로 본다. 모든 backend가 문제면
    전체(systemic) 문제이므로 격리가 아니라 증설로 대응해야 한다 -> 빈 리스트 반환.
    """
    if scale_up_error_rate is None or not snapshot.per_backend:
        return []

    breaching, healthy = [], []
    for b in snapshot.per_backend:
        if b.error_rate is None:
            continue
        if b.error_rate > scale_up_error_rate:
            breaching.append(b.name)
        else:
            healthy.append(b.name)

    # 문제 backend가 있고, 정상 backend도 남아있어야 격리가 의미 있다.
    if breaching and healthy:
        return breaching
    return []


def assess_anomaly(
    snapshot: TrafficSnapshot,
    baseline: Baseline,
    scale_up_error_rate: Optional[float],
    max_p99_latency_ms: Optional[float],
) -> AnomalyAssessment:
    """정적 임계값 + EWMA 이탈을 조합해 이상 여부/심각도를 판단한다."""
    a = AnomalyAssessment()
    warm = baseline.warm
    scores: List[float] = []

    # --- 에러율 ---
    if snapshot.error_rate is not None and scale_up_error_rate is not None:
        if snapshot.error_rate > scale_up_error_rate:
            a.error_static = True
            a.reasons.append(
                f"error_rate={snapshot.error_rate:.4f} > scaleUpErrorRate={scale_up_error_rate:.4f}"
            )
        if warm:
            if baseline.error_rate.is_upper_breach(snapshot.error_rate):
                a.error_ewma = True
                a.reasons.append(
                    f"error_rate baseline 이탈(z={baseline.error_rate.upper_zscore(snapshot.error_rate):.2f}, "
                    f"평소~{baseline.error_rate.mean:.4f})"
                )
            scores.append(baseline.error_rate.upper_zscore(snapshot.error_rate))

    # --- p99 지연시간 ---
    if snapshot.p99_latency_ms is not None and max_p99_latency_ms is not None:
        if snapshot.p99_latency_ms > max_p99_latency_ms:
            a.latency_static = True
            a.reasons.append(
                f"p99={snapshot.p99_latency_ms:.0f}ms > maxP99LatencyMs={max_p99_latency_ms:.0f}ms"
            )
        if warm:
            if baseline.p99_latency_ms.is_upper_breach(snapshot.p99_latency_ms):
                a.latency_ewma = True
                a.reasons.append(
                    f"p99 baseline 이탈(z={baseline.p99_latency_ms.upper_zscore(snapshot.p99_latency_ms):.2f}, "
                    f"평소~{baseline.p99_latency_ms.mean:.0f}ms)"
                )
            scores.append(baseline.p99_latency_ms.upper_zscore(snapshot.p99_latency_ms))

    a.score = max(scores) if scores else None
    a.culprit_backends = _find_culprit_backends(snapshot, scale_up_error_rate)

    # --- 심각도 산정 ---
    # critical: 어느 한 지표에서 정적+EWMA 두 신호가 동시에 이상을 가리킬 때(오탐 가능성 낮음).
    #           단 warmup 전에는 EWMA를 못 쓰므로 critical 승격 불가(최대 warning).
    both_error = a.error_static and a.error_ewma
    both_latency = a.latency_static and a.latency_ewma
    if warm and (both_error or both_latency):
        a.severity = "critical"
    elif a.any_anomaly:
        a.severity = "warning"
    else:
        a.severity = "none"

    if not warm and a.any_anomaly:
        a.reasons.append("(baseline warmup 미완료 -> 정적 임계값 기준, 최대 severity=warning)")

    return a
