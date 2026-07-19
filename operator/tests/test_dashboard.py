"""dashboard 모듈 테스트 - 데이터 요약 로직 + FastAPI 엔드포인트.

kubernetes client는 MagicMock으로 대체(실제 클러스터 접근 없음). 대시보드는 읽기 전용이므로
list_cluster_custom_object/list_namespaced_custom_object 호출만 검증하면 충분하다.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from k8s_traffic_operator.dashboard import app as dashboard_app
from k8s_traffic_operator.dashboard import data


def _cr(
    *,
    namespace="shop",
    name="checkout-policy",
    phase="Reconciled",
    action="scale",
    reason="RPS 초과",
    severity="none",
    snapshot_status="ok",
    applied=True,
    detail="replicas 2 -> 4",
    last_reconcile_at=None,
    created="2026-07-19T12:00:00Z",
):
    return {
        "metadata": {"namespace": namespace, "name": name, "creationTimestamp": created},
        "spec": {"target": {"httpRoute": "checkout-route", "deployment": "checkout-service"}},
        "status": {
            "reconcile": {
                "phase": phase,
                "lastSnapshotStatus": snapshot_status,
                "lastReconcileAt": last_reconcile_at if last_reconcile_at is not None else time.time(),
                "lastDecision": {"action": action, "reason": reason, "severity": severity},
                "lastActuation": {"applied": applied, "detail": detail},
            }
        },
    }


# --------------------------------------------------------------------------- data.fetch_policies
def test_fetch_policies_cluster_wide(monkeypatch):
    monkeypatch.setattr(data, "WATCH_NAMESPACE", "")
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {"items": [_cr(), _cr(name="cart-policy", namespace="shop")]}
    monkeypatch.setattr(data, "_custom_api", lambda: api)

    result = data.fetch_policies()

    assert len(result) == 2
    api.list_cluster_custom_object.assert_called_once_with(
        group="ops.example.com", version="v1alpha1", plural="trafficpolicies"
    )
    api.list_namespaced_custom_object.assert_not_called()


def test_fetch_policies_scoped_to_watch_namespace(monkeypatch):
    monkeypatch.setattr(data, "WATCH_NAMESPACE", "shop")
    api = MagicMock()
    api.list_namespaced_custom_object.return_value = {"items": [_cr()]}
    monkeypatch.setattr(data, "_custom_api", lambda: api)

    result = data.fetch_policies()

    assert len(result) == 1
    api.list_namespaced_custom_object.assert_called_once_with(
        group="ops.example.com", version="v1alpha1", namespace="shop", plural="trafficpolicies"
    )


def test_fetch_policies_summarizes_decision_and_actuation(monkeypatch):
    monkeypatch.setattr(data, "WATCH_NAMESPACE", "")
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {"items": [_cr(action="isolate_backend", severity="critical")]}
    monkeypatch.setattr(data, "_custom_api", lambda: api)

    [summary] = data.fetch_policies()

    assert summary.namespace == "shop"
    assert summary.name == "checkout-policy"
    assert summary.http_route == "checkout-route"
    assert summary.deployment == "checkout-service"
    assert summary.last_action == "isolate_backend"
    assert summary.last_severity == "critical"
    assert summary.last_actuation_applied is True
    assert summary.raw_error is None


def test_fetch_policies_api_error_is_surfaced_not_swallowed(monkeypatch):
    """조회 자체가 실패하면(권한 등) '데이터 없음'이 아니라 명시적 에러 항목을 반환해야 한다."""
    monkeypatch.setattr(data, "WATCH_NAMESPACE", "")
    api = MagicMock()
    api.list_cluster_custom_object.side_effect = ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(data, "_custom_api", lambda: api)

    result = data.fetch_policies()

    assert len(result) == 1
    assert result[0].raw_error is not None
    assert "403" in result[0].raw_error


def test_fetch_policies_empty_list_is_not_an_error(monkeypatch):
    monkeypatch.setattr(data, "WATCH_NAMESPACE", "")
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {"items": []}
    monkeypatch.setattr(data, "_custom_api", lambda: api)

    result = data.fetch_policies()
    assert result == []


def test_missing_status_fields_default_gracefully(monkeypatch):
    """아직 한 번도 reconcile되지 않은(status 비어있는) CR도 죽지 않고 Pending으로 표시."""
    monkeypatch.setattr(data, "WATCH_NAMESPACE", "")
    bare_cr = {"metadata": {"namespace": "shop", "name": "new-policy"}, "spec": {"target": {}}, "status": {}}
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {"items": [bare_cr]}
    monkeypatch.setattr(data, "_custom_api", lambda: api)

    [summary] = data.fetch_policies()
    assert summary.phase == "Pending"
    assert summary.last_action == "-"
    assert summary.last_actuation_applied is None


# --------------------------------------------------------------------------- FastAPI 엔드포인트
@pytest.fixture
def client_with_policies(monkeypatch):
    monkeypatch.setattr(
        dashboard_app.data, "fetch_policies",
        lambda: [data._summarize(_cr(), time.time())],
    )
    return TestClient(dashboard_app.app)


def test_healthz():
    client = TestClient(dashboard_app.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_root_returns_html_table(client_with_policies):
    resp = client_with_policies.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "checkout-policy" in resp.text
    assert "<table>" in resp.text


def test_root_shows_empty_state_when_no_policies(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    client = TestClient(dashboard_app.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "없습니다" in resp.text


def test_api_policies_returns_json(client_with_policies):
    resp = client_with_policies.get("/api/policies")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] == 1
    assert payload["policies"][0]["name"] == "checkout-policy"
    assert "generatedAt" in payload


def test_html_escapes_untrusted_reason_field(monkeypatch):
    """CR의 reason 필드는 오퍼레이터가 생성하지만, 방어적으로 HTML 이스케이프 확인(XSS 방지)."""
    malicious = _cr(reason='<script>alert(1)</script>')
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [data._summarize(malicious, time.time())])
    client = TestClient(dashboard_app.app)
    resp = client.get("/")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_error_row_rendered_for_fetch_failure(monkeypatch):
    err_summary = data.PolicySummary(namespace="shop", name="(조회 실패)", phase="Error", raw_error="권한 없음: 403")
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [err_summary])
    client = TestClient(dashboard_app.app)
    resp = client.get("/")
    assert "권한 없음" in resp.text


# --------------------------------------------------------------------------- /flows, /api/flows (Hubble)
from k8s_traffic_operator.dashboard.hubble_flows import FlowSummary


def test_flows_html_renders_verdicts_and_pairs(monkeypatch):
    summary = FlowSummary(
        total=3,
        verdicts={"FORWARDED": 2, "DROPPED": 1},
        top_pairs=[{"src": "shop/checkout-abc", "dst": "shop/cart-def", "protocol": "TCP",
                    "dst_port": 8080, "count": 2, "last_seen": "2026-07-19T12:00:00.000000000Z"}],
    )
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda: summary)
    client = TestClient(dashboard_app.app)
    resp = client.get("/flows")
    assert resp.status_code == 200
    assert "shop/checkout-abc" in resp.text
    assert "shop/cart-def" in resp.text
    assert "FORWARDED" in resp.text
    assert "DROPPED" in resp.text


def test_flows_html_shows_fetch_error_explicitly(monkeypatch):
    summary = FlowSummary(fetch_error="hubble CLI를 찾을 수 없음")
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda: summary)
    client = TestClient(dashboard_app.app)
    resp = client.get("/flows")
    assert "hubble CLI를 찾을 수 없음" in resp.text


def test_flows_html_shows_empty_state_when_zero_flows(monkeypatch):
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda: FlowSummary(total=0))
    client = TestClient(dashboard_app.app)
    resp = client.get("/flows")
    assert "관측된 흐름이 없습니다" in resp.text


def test_api_flows_returns_json(monkeypatch):
    summary = FlowSummary(total=5, verdicts={"FORWARDED": 5}, top_pairs=[{
        "src": "a", "dst": "b", "protocol": "TCP", "dst_port": 80, "count": 5, "last_seen": "t",
    }])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda: summary)
    client = TestClient(dashboard_app.app)
    resp = client.get("/api/flows")
    payload = resp.json()
    assert payload["total"] == 5
    assert payload["topPairs"][0]["dst"] == "b"
    assert payload["fetchError"] is None


def test_flows_html_escapes_pod_names_for_xss(monkeypatch):
    summary = FlowSummary(total=1, verdicts={"FORWARDED": 1}, top_pairs=[{
        "src": "<script>alert(1)</script>", "dst": "b", "protocol": "TCP",
        "dst_port": 80, "count": 1, "last_seen": "2026-07-19T12:00:00.000000000Z",
    }])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda: summary)
    client = TestClient(dashboard_app.app)
    resp = client.get("/flows")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_nav_links_present_on_both_pages(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda: FlowSummary(total=0))
    client = TestClient(dashboard_app.app)
    assert 'href="/flows"' in client.get("/").text
    assert 'href="/"' in client.get("/flows").text
