"""dashboard 패키지 - TrafficPolicy 운영 현황을 보여주는 읽기 전용 웹 대시보드.

오퍼레이터 본체(handlers/metrics/policy/actuator)와 완전히 분리된 별도 프로세스다.
이 패키지는 클러스터에 아무것도 쓰지 않는다(get/list/watch만) — 관측 전용이므로
오퍼레이터의 판단/실행 로직에 어떤 영향도 주지 않는다.
"""
