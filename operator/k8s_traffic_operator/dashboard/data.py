"""TrafficPolicy CR을 조회해 대시보드가 그리기 좋은 형태로 요약한다.

이 모듈은 클러스터에 대한 **읽기 전용**(get/list/watch) 접근만 한다. 오퍼레이터 본체
(schemas.py/handlers.py)가 CR.status에 기록하는 구조를 그대로 소비하는 쪽이며, 별도의
계약을 새로 만들지 않는다 — status.reconcile.{phase,lastDecision,lastActuation}은
handlers.reconcile()이 채우는 필드 그대로다(schemas.Decision/ActuationResult.to_dict()).
"""

from __future__ import annotations

import calendar
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kubernetes import client
from kubernetes.client.rest import ApiException

API_GROUP = "ops.example.com"
API_VERSION = "v1alpha1"
CRD_PLURAL = "trafficpolicies"

# 대시보드가 관찰할 네임스페이스. 비어있으면(기본) 클러스터 전체.
# 특정 네임스페이스만 보고 싶으면 배포 시 이 값을 좁혀서(RBAC도 함께) 범위를 제한한다.
WATCH_NAMESPACE = os.getenv("WATCH_NAMESPACE", "").strip()


@dataclass
class PolicySummary:
    """대시보드 한 행(row)에 해당하는 요약 정보."""

    namespace: str
    name: str
    phase: str = "Unknown"
    http_route: str = ""
    deployment: str = ""
    last_action: str = "-"
    last_reason: str = ""
    last_severity: str = "none"
    last_snapshot_status: str = "-"
    last_actuation_applied: Optional[bool] = None
    last_actuation_detail: str = ""
    last_reconcile_age_s: Optional[float] = None
    age_s: float = 0.0
    raw_error: Optional[str] = None  # 이 CR 자체를 파싱하다 문제가 있었으면 원인을 남긴다(숨기지 않음).


def _custom_api() -> client.CustomObjectsApi:
    """kubeconfig 로드 순서: in-cluster ServiceAccount 우선, 실패 시 로컬 kubeconfig."""
    try:
        client.Configuration.get_default_copy()
    except Exception:  # noqa: BLE001
        pass
    from kubernetes import config as kube_config

    try:
        kube_config.load_incluster_config()
    except Exception:  # noqa: BLE001
        kube_config.load_kube_config()
    return client.CustomObjectsApi()


def _age_seconds(iso_timestamp: Optional[str], now: float) -> float:
    if not iso_timestamp:
        return 0.0
    try:
        # Kubernetes creationTimestamp: RFC3339 UTC, 예 "2026-07-19T12:18:10Z".
        # time.mktime()은 struct_time을 로컬 시간대로 해석하므로 UTC 문자열에는 쓰면 안 된다
        # (로컬 tz 오프셋만큼 age가 어긋남). calendar.timegm()이 UTC 그대로 epoch로 변환한다.
        struct = time.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ")
        return max(0.0, now - calendar.timegm(struct))
    except (ValueError, TypeError):
        return 0.0


def _summarize(cr: dict, now: float) -> PolicySummary:
    meta = cr.get("metadata", {}) or {}
    spec = cr.get("spec", {}) or {}
    status = cr.get("status", {}) or {}
    reconcile = status.get("reconcile", {}) or {}
    target = spec.get("target", {}) or {}

    last_decision = reconcile.get("lastDecision") or {}
    last_actuation = reconcile.get("lastActuation") or {}

    last_reconcile_at = reconcile.get("lastReconcileAt")
    last_reconcile_age_s = (now - last_reconcile_at) if isinstance(last_reconcile_at, (int, float)) else None

    return PolicySummary(
        namespace=meta.get("namespace", ""),
        name=meta.get("name", ""),
        phase=reconcile.get("phase") or status.get("register_policy", {}).get("phase") or "Pending",
        http_route=target.get("httpRoute", ""),
        deployment=target.get("deployment", ""),
        last_action=last_decision.get("action", "-"),
        last_reason=last_decision.get("reason", ""),
        last_severity=last_decision.get("severity", "none"),
        last_snapshot_status=reconcile.get("lastSnapshotStatus", "-"),
        last_actuation_applied=last_actuation.get("applied"),
        last_actuation_detail=last_actuation.get("detail", ""),
        last_reconcile_age_s=last_reconcile_age_s,
        age_s=_age_seconds(meta.get("creationTimestamp"), now),
    )


def fetch_policies() -> List[PolicySummary]:
    """모든(또는 WATCH_NAMESPACE로 좁힌) TrafficPolicy CR을 조회해 요약 목록으로 반환한다.

    클러스터 접근 실패는 예외를 삼키지 않고 별도 항목(raw_error)으로 노출한다 — 대시보드가
    "데이터 없음"과 "조회 자체가 실패함"을 구분해서 보여줘야 운영자가 원인을 알 수 있다
    (오퍼레이터 본체의 no_data/collection_failed 구분 원칙과 동일한 정신).
    """
    now = time.time()
    api = _custom_api()
    try:
        if WATCH_NAMESPACE:
            resp = api.list_namespaced_custom_object(
                group=API_GROUP, version=API_VERSION, namespace=WATCH_NAMESPACE, plural=CRD_PLURAL,
            )
        else:
            resp = api.list_cluster_custom_object(group=API_GROUP, version=API_VERSION, plural=CRD_PLURAL)
    except ApiException as exc:
        return [
            PolicySummary(
                namespace=WATCH_NAMESPACE or "(all)",
                name="(조회 실패)",
                phase="Error",
                raw_error=f"TrafficPolicy 목록 조회 실패: {exc.status} {exc.reason}",
            )
        ]

    items = resp.get("items", []) or []
    return [_summarize(cr, now) for cr in items]
