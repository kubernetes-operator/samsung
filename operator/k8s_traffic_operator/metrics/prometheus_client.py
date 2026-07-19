"""Prometheus HTTP API 를 직접 호출하는 얇은(thin) 클라이언트.

무거운 의존성(prometheus-api-client)을 끌어오지 않고 `requests` 로 Prometheus 의
`/api/v1/query` (instant query) 엔드포인트만 사용한다. 이 모듈은 Gateway API 구현체를
전혀 모른다 — 순수하게 "PromQL 문자열을 던지고 샘플 목록을 받는" 역할만 한다.

계약:
    - query(promql) 는 성공 시 List[Sample] 을 반환한다. 결과가 없으면 빈 리스트([]).
      (빈 리스트 == "쿼리는 성공했으나 매칭되는 시계열 없음" == 상위에서 no_data 판단 근거)
    - 연결/HTTP/파싱 실패는 PrometheusConnectionError 로 올린다. 상위(collector)가
      이를 잡아 TrafficSnapshot.status="collection_failed" 로 변환한다.
    - 전송 계층 실패에 한해 1회 재시도한다(아키텍처 문서 §에러 핸들링: "재시도 1회").
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover - requirements.txt 에 명시되어 있으나 방어적으로 처리
    requests = None  # type: ignore

log = logging.getLogger(__name__)


class PrometheusError(Exception):
    """Prometheus 관련 일반 오류."""


class PrometheusConnectionError(PrometheusError):
    """연결/타임아웃/HTTP 오류/응답 파싱 실패 등 '수집 자체가 실패'한 경우.

    collector 는 이 예외를 잡아 status='collection_failed' 로 변환한다. 즉 이 예외는
    handlers 의 reconcile 루프까지 절대 전파되지 않는다.
    """


@dataclass
class Sample:
    """Prometheus instant query 결과의 단일 시계열 샘플.

    labels: metric 레이블 딕셔너리(예: {"envoy_cluster_name": "..."}).
    value : 해당 시점 값(float). PromQL 이 'NaN' 을 돌려주면 value 는 float('nan').
    """

    labels: Dict[str, str]
    value: float


class PrometheusClient:
    """Prometheus instant query 전용 최소 클라이언트."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        retries: int = 1,
        session: Optional[object] = None,
    ) -> None:
        # base_url 예: "http://prometheus-server.monitoring.svc:80"
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        # session 주입은 테스트에서 실제 네트워크 없이 대체(fake)하기 위한 확장점이다.
        self._session = session
        if self._session is None and requests is not None:
            self._session = requests.Session()

    def query(self, promql: str) -> List[Sample]:
        """instant query 를 실행하고 Sample 목록을 반환한다.

        결과 없음 -> [] (예외 아님).
        전송/HTTP/파싱 실패 -> PrometheusConnectionError (retries 회 재시도 후).
        """
        if self._session is None:
            raise PrometheusConnectionError(
                "requests 미설치 - Prometheus HTTP API 를 호출할 수 없음"
            )

        url = f"{self.base_url}/api/v1/query"
        last_err: Optional[Exception] = None

        # 총 시도 횟수 = 1(최초) + retries(재시도). 문서 규약상 retries 기본 1.
        for attempt in range(self.retries + 1):
            try:
                resp = self._session.get(  # type: ignore[union-attr]
                    url, params={"query": promql}, timeout=self.timeout
                )
                if resp.status_code != 200:
                    raise PrometheusConnectionError(
                        f"Prometheus HTTP {resp.status_code} for query={promql!r}"
                    )
                payload = resp.json()
                if payload.get("status") != "success":
                    # status=error 는 대개 PromQL 문법/평가 오류 -> 수집 실패로 취급
                    raise PrometheusConnectionError(
                        f"Prometheus status={payload.get('status')} "
                        f"error={payload.get('error')!r} for query={promql!r}"
                    )
                return self._parse_vector(payload.get("data", {}))
            except PrometheusConnectionError as e:
                # HTTP 200 이 아닌 응답 등 명시적 실패. 전송 오류가 아니므로 재시도 의미가
                # 크지 않지만, 문서의 "재시도 1회" 규약을 일관되게 적용한다.
                last_err = e
                log.warning(
                    "Prometheus query 실패(attempt %d/%d): %s",
                    attempt + 1,
                    self.retries + 1,
                    e,
                )
            except Exception as e:  # 연결 오류/타임아웃/JSON 파싱 오류 등
                last_err = e
                log.warning(
                    "Prometheus query 전송 오류(attempt %d/%d): %s",
                    attempt + 1,
                    self.retries + 1,
                    e,
                )

        raise PrometheusConnectionError(
            f"Prometheus query 최종 실패 (query={promql!r}): {last_err}"
        )

    @staticmethod
    def _parse_vector(data: dict) -> List[Sample]:
        """instant query 응답의 data.result(vector) 를 Sample 목록으로 변환."""
        result_type = data.get("resultType")
        result = data.get("result", []) or []
        samples: List[Sample] = []

        for item in result:
            metric = item.get("metric", {}) or {}
            # vector: value=[ts, "val"], scalar: result=[ts, "val"]
            if result_type == "scalar":
                raw_val = data.get("result", [None, None])[1]
                samples.append(Sample(labels={}, value=_to_float(raw_val)))
                break
            value_pair = item.get("value")
            if not value_pair or len(value_pair) < 2:
                continue
            samples.append(Sample(labels=metric, value=_to_float(value_pair[1])))
        return samples


def _to_float(raw) -> float:
    """Prometheus 는 값을 문자열로 준다('123.4', 'NaN', '+Inf'). float 로 변환."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return math.nan
