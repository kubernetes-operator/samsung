"""kopf 실행 엔트리포인트.

로컬 실행:
    kopf run -m k8s_traffic_operator.main --verbose

또는 파이썬으로 직접:
    python -m k8s_traffic_operator.main

`kopf run -m <module>` 방식이 표준이다. 이 모듈은 handlers를 import하여
kopf에 핸들러들을 등록시키는 역할만 한다.
"""

from __future__ import annotations

import kopf

# handlers를 import하는 것만으로 @kopf.on.* / @kopf.timer 데코레이터가 등록된다.
from . import handlers  # noqa: F401


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, logger, **kwargs):
    """오퍼레이터 기동 시 kopf 런타임 설정."""
    # 동일 오브젝트에 대한 핸들러 재실행 시 서버측 상태 저장 방식.
    settings.persistence.finalizer = "ops.example.com/traffic-operator"
    # timer/handler 예외가 나도 오퍼레이터 프로세스는 계속 살아있게 한다.
    settings.batching.error_delays = [10, 30, 60]
    logger.info("k8s-traffic-operator 기동 완료.")


def main() -> None:
    """`python -m k8s_traffic_operator.main` 진입점."""
    # 프로그램적 실행. 실무에서는 `kopf run -m k8s_traffic_operator.main` 사용 권장.
    kopf.run(clusterwide=True)


if __name__ == "__main__":
    main()
