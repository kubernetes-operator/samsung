"""Gateway API 구현체 어댑터 레지스트리.

새 구현체를 추가하려면:
    1) base.GatewayAdapter 를 상속한 어댑터 클래스를 이 패키지에 작성
    2) 아래 _REGISTRY 에 IMPL_NAME -> 클래스 등록
collector / policy 코드는 손대지 않는다 — 이것이 어댑터 패턴의 목적이다.
"""

from __future__ import annotations

import logging
from typing import Dict, Type

from .base import GatewayAdapter
from .envoy_gateway import EnvoyGatewayAdapter
from .istio import IstioAdapter
from .nginx_gateway_fabric import NginxGatewayFabricAdapter

log = logging.getLogger(__name__)

# 지원 구현체 등록표. 키는 CRD/환경변수에서 오는 구현체 식별자.
_REGISTRY: Dict[str, Type[GatewayAdapter]] = {
    EnvoyGatewayAdapter.IMPL_NAME: EnvoyGatewayAdapter,
    IstioAdapter.IMPL_NAME: IstioAdapter,
    NginxGatewayFabricAdapter.IMPL_NAME: NginxGatewayFabricAdapter,
}

# 미지정/미지원 구현체를 만났을 때의 기본 폴백.
# 표준 Gateway API 구현체 중 가장 널리 쓰이는 Envoy Gateway 메트릭 컨벤션으로 폴백한다
# (스킬 문서의 "지원하지 않는 구현체" 지침: 예외 대신 폴백 시도, 실패 시 상위가 안전 처리).
DEFAULT_IMPL = EnvoyGatewayAdapter.IMPL_NAME

# 별칭(관용 표기)도 흡수한다.
_ALIASES = {
    "envoy": "envoy-gateway",
    "envoygateway": "envoy-gateway",
    "envoy_gateway": "envoy-gateway",
    "istio-gateway": "istio",
    "nginx": "nginx-gateway-fabric",
    "ngf": "nginx-gateway-fabric",
    "nginx-gateway": "nginx-gateway-fabric",
    "nginx_gateway_fabric": "nginx-gateway-fabric",
}


def get_adapter(impl: str) -> GatewayAdapter:
    """구현체 식별자로 어댑터 인스턴스를 얻는다.

    미지원 식별자면 로그를 남기고 DEFAULT_IMPL 로 폴백한다(절대 예외를 던지지 않는다).
    """
    key = (impl or "").strip().lower()
    key = _ALIASES.get(key, key)
    cls = _REGISTRY.get(key)
    if cls is None:
        log.warning(
            "지원하지 않는 Gateway API 구현체 '%s' → 기본 어댑터 '%s' 로 폴백",
            impl,
            DEFAULT_IMPL,
        )
        cls = _REGISTRY[DEFAULT_IMPL]
    return cls()


def supported_implementations() -> list[str]:
    return sorted(_REGISTRY.keys())


__all__ = [
    "GatewayAdapter",
    "EnvoyGatewayAdapter",
    "IstioAdapter",
    "NginxGatewayFabricAdapter",
    "get_adapter",
    "supported_implementations",
    "DEFAULT_IMPL",
]
