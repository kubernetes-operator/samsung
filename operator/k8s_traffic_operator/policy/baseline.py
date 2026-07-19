"""EWMA baseline 상태 관리 - "평소 대비 이상"을 잡는 2차 신호.

정적 임계값(CRD 명시값)이 1차 방어선이라면, EWMA baseline 이탈은 2차 신호다.
정적 임계값만으로는 "평소보다 명백히 나쁜데 아직 상한 미만"인 완만한 열화를 놓치고,
또 순간 스파이크에 과민 반응한다. EWMA는 서서히 적응하는 baseline을 유지해
추세 기반으로 이탈을 판단한다.

상태 보존에 관한 가정 (assumption):
    evaluate(spec, snapshot, status) 시그니처에는 CR 식별자(name/namespace)가 없고,
    Decision 반환만 하므로 baseline을 CR status에 직접 기록할 수 없다. 따라서 baseline은
    **오퍼레이터 프로세스 내 메모리**에 spec.target 기반 키로 보관한다.
      - kopf 오퍼레이터는 장기 실행 프로세스이므로 reconcile(30s) 간 상태가 유지된다.
      - 오퍼레이터 재시작 시 baseline은 초기화된다 -> warmup 기간 동안 EWMA 신호를
        무시하고 정적 임계값만으로 판단한다(안전한 저하, fail-safe). 재시작이 흔치 않고
        정적 임계값이 항상 1차 방어선으로 남으므로 허용 가능한 트레이드오프로 판단.
    영속 baseline이 필요하면 후속 과제로 handlers가 status에 baseline을 기록/주입하도록
    확장할 수 있다(그때 이 모듈의 update/get을 재사용).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

# EWMA 파라미터
_ALPHA = 0.2            # 평활 계수. 작을수록 baseline이 느리게 적응(노이즈에 둔감).
_MIN_SAMPLES = 10       # 이 표본 수 미만이면 warmup 미완료 -> EWMA 신호 신뢰하지 않음.
_K = 3.0                # 밴드 폭(표준편차 배수). x > mean + K*stddev 이면 이탈(상방 단측).
_REL_MARGIN = 0.5       # stddev가 사실상 0일 때의 상대 이탈 기준(평소 대비 +50%).
_EPS = 1e-9


@dataclass
class _Stat:
    """단일 지표의 EWMA 평균/분산 추적기(West 방식 지수가중 분산)."""

    mean: float = 0.0
    var: float = 0.0
    count: int = 0

    def update(self, x: float) -> None:
        if self.count == 0:
            self.mean = x
            self.var = 0.0
        else:
            diff = x - self.mean
            incr = _ALPHA * diff
            self.mean += incr
            # var_new = (1-alpha)*(var_old + diff*incr)
            self.var = (1.0 - _ALPHA) * (self.var + diff * incr)
        self.count += 1

    @property
    def stddev(self) -> float:
        return math.sqrt(self.var) if self.var > 0 else 0.0

    def upper_zscore(self, x: float) -> float:
        """상방 z-score. 이탈 정도의 정량 지표(음수면 평소 이하 -> 0으로 클램프)."""
        sd = self.stddev
        if sd <= _EPS:
            # 분산이 사실상 0(평탄한 이력): 상대 마진으로 대체 판단.
            if self.mean <= _EPS:
                return 0.0
            rel = (x - self.mean) / self.mean
            # 상대 초과분을 대략적인 점수로 환산(_REL_MARGIN을 1 sigma로 간주).
            return max(0.0, rel / _REL_MARGIN)
        return max(0.0, (x - self.mean) / sd)

    def is_upper_breach(self, x: float) -> bool:
        """x가 baseline을 상방으로 유의하게 이탈했는가."""
        sd = self.stddev
        if sd <= _EPS:
            if self.mean <= _EPS:
                return False
            return x > self.mean * (1.0 + _REL_MARGIN)
        return x > self.mean + _K * sd


@dataclass
class Baseline:
    """대상 하나(target)의 지표별 baseline 묶음."""

    error_rate: _Stat = field(default_factory=_Stat)
    p99_latency_ms: _Stat = field(default_factory=_Stat)

    @property
    def warm(self) -> bool:
        """EWMA 신호를 신뢰할 만큼 표본이 쌓였는가(warmup 완료)."""
        return (
            self.error_rate.count >= _MIN_SAMPLES
            and self.p99_latency_ms.count >= _MIN_SAMPLES
        )

    def observe(self, error_rate: Optional[float], p99_latency_ms: Optional[float]) -> None:
        """정상 관측치로 baseline을 갱신한다.

        주의: 확정된 심각 이상(critical) 구간에서는 호출하지 않는다(baseline 오염 방지).
        engine이 severity를 보고 호출 여부를 결정한다.
        """
        if error_rate is not None:
            self.error_rate.update(error_rate)
        if p99_latency_ms is not None:
            self.p99_latency_ms.update(p99_latency_ms)


# 프로세스 내 baseline 저장소. 키는 spec.target에서 파생(namespace/deployment/httpRoute).
_BASELINES: Dict[str, Baseline] = {}


def target_key(spec: dict) -> str:
    """spec.target으로 baseline 저장 키를 만든다(CR 식별자 대용)."""
    target = spec.get("target", {}) or {}
    ns = target.get("namespace", "") or ""
    dep = target.get("deployment", "") or ""
    route = target.get("httpRoute", "") or ""
    return f"{ns}/{dep}/{route}"


def get_baseline(spec: dict) -> Baseline:
    key = target_key(spec)
    bl = _BASELINES.get(key)
    if bl is None:
        bl = Baseline()
        _BASELINES[key] = bl
    return bl


def reset() -> None:
    """테스트/재초기화용."""
    _BASELINES.clear()
