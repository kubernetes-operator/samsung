"""actuator 내부 액션 결과 타입.

scaler / router 같은 하위 실행 함수는 이 `ActionOutcome`을 반환하고,
executor.apply()가 이를 모아 공유 계약인 `schemas.ActuationResult`로 변환한다.
schemas.ActuationResult는 handlers가 소비하는 대외 계약이고, ActionOutcome은
actuator 내부의 세분화된 상태("applied"/"skipped"/"failed")를 표현하기 위한
경량 타입이다 — 스키마 계약을 오염시키지 않으려고 별도로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

OutcomeStatus = Literal["applied", "skipped", "failed"]


@dataclass
class ActionOutcome:
    status: OutcomeStatus
    detail: str = ""
    error: Optional[str] = None

    @property
    def applied(self) -> bool:
        return self.status == "applied"

    @property
    def failed(self) -> bool:
        return self.status == "failed"
