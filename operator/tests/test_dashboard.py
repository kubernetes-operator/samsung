"""dashboard 모듈 테스트 - 데이터 요약 로직 + FastAPI 엔드포인트.

kubernetes client는 MagicMock으로 대체(실제 클러스터 접근 없음). 대시보드는 읽기 전용이므로
list_cluster_custom_object/list_namespaced_custom_object 호출만 검증하면 충분하다.
"""

from __future__ import annotations

import base64
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from k8s_traffic_operator.dashboard import app as dashboard_app
from k8s_traffic_operator.dashboard import data

# 대시보드는 /healthz를 뺀 모든 경로가 HTTP Basic 인증으로 보호된다. 대부분의 테스트는
# 엔드포인트의 '내용'을 검증하므로, 자격증명을 env로 세팅(autouse)하고 유효한 Authorization
# 헤더를 기본 탑재한 클라이언트(_authed_client)를 쓴다. 인증 '동작' 자체는 별도 섹션에서 검증한다.
_TEST_USER = "test-admin"
_TEST_PASS = "test-secret-pw"


def _basic_header(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(autouse=True)
def _dashboard_auth_env(monkeypatch):
    """모든 테스트에서 인증이 '설정된' 상태가 되도록 env를 채운다(개별 테스트가 delenv로 덮어쓸 수 있음)."""
    monkeypatch.setenv("DASHBOARD_USERNAME", _TEST_USER)
    monkeypatch.setenv("DASHBOARD_PASSWORD", _TEST_PASS)


def _authed_client() -> TestClient:
    client = TestClient(dashboard_app.app)
    client.headers.update(_basic_header(_TEST_USER, _TEST_PASS))
    return client


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
    return _authed_client()


def test_healthz():
    client = _authed_client()
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
    client = _authed_client()
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
    client = _authed_client()
    resp = client.get("/")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_error_row_rendered_for_fetch_failure(monkeypatch):
    err_summary = data.PolicySummary(namespace="shop", name="(조회 실패)", phase="Error", raw_error="권한 없음: 403")
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [err_summary])
    client = _authed_client()
    resp = client.get("/")
    assert "권한 없음" in resp.text


# --------------------------------------------------------------------------- /flows, /api/flows (Hubble)
from k8s_traffic_operator.dashboard.hubble_flows import FlowSummary


def test_flows_html_renders_verdicts_and_pairs(monkeypatch):
    summary = FlowSummary(
        total=3,
        shown=3,
        app_flows=3,
        verdicts={"FORWARDED": 2, "DROPPED": 1},
        top_pairs=[{"src": "shop/checkout-abc", "dst": "shop/cart-def", "protocol": "TCP",
                    "dst_port": 8080, "count": 2, "last_seen": "2026-07-19T12:00:00.000000000Z"}],
    )
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    client = _authed_client()
    resp = client.get("/flows")
    assert resp.status_code == 200
    assert "shop/checkout-abc" in resp.text
    assert "shop/cart-def" in resp.text
    assert "FORWARDED" in resp.text
    assert "DROPPED" in resp.text


def test_flows_html_shows_fetch_error_explicitly(monkeypatch):
    summary = FlowSummary(fetch_error="hubble CLI를 찾을 수 없음")
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    client = _authed_client()
    resp = client.get("/flows")
    assert "hubble CLI를 찾을 수 없음" in resp.text


def test_flows_html_shows_empty_state_when_zero_flows(monkeypatch):
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: FlowSummary(total=0))
    client = _authed_client()
    resp = client.get("/flows")
    assert "관측된 흐름이 없습니다" in resp.text


def test_api_flows_returns_json(monkeypatch):
    summary = FlowSummary(total=5, shown=5, app_flows=5, verdicts={"FORWARDED": 5}, top_pairs=[{
        "src": "a", "dst": "b", "protocol": "TCP", "dst_port": 80, "count": 5, "last_seen": "t",
    }])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    client = _authed_client()
    resp = client.get("/api/flows")
    payload = resp.json()
    assert payload["total"] == 5
    assert payload["topPairs"][0]["dst"] == "b"
    assert payload["fetchError"] is None


def test_flows_html_escapes_pod_names_for_xss(monkeypatch):
    summary = FlowSummary(total=1, shown=1, app_flows=1, verdicts={"FORWARDED": 1}, top_pairs=[{
        "src": "<script>alert(1)</script>", "dst": "b", "protocol": "TCP",
        "dst_port": 80, "count": 1, "last_seen": "2026-07-19T12:00:00.000000000Z",
    }])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    client = _authed_client()
    resp = client.get("/flows")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_nav_links_present_on_both_pages(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: FlowSummary(total=0))
    client = _authed_client()
    assert 'href="/flows"' in client.get("/").text
    assert 'href="/"' in client.get("/flows").text


def test_links_use_external_prefix_when_configured(monkeypatch):
    """서브패스(/traffic-dashboard) 노출 시 nav/토글 링크가 프리픽스를 포함해야 한다.

    프리픽스 없이 절대경로 '/flows'를 쓰면 브라우저가 프리픽스 밖으로 나가 게이트웨이에서
    404가 난다(실제 발생했던 버그). 라우트 자체는 게이트웨이가 프리픽스를 떼므로 '/'/'/flows'.
    """
    monkeypatch.setenv("DASHBOARD_URL_PREFIX", "/traffic-dashboard")
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: FlowSummary(total=0))
    client = _authed_client()

    root_html = client.get("/").text
    assert 'href="/traffic-dashboard/"' in root_html
    assert 'href="/traffic-dashboard/flows"' in root_html

    flows_html = client.get("/flows").text
    # 스코프 토글 링크도 프리픽스를 포함해야 한다.
    assert 'href="/traffic-dashboard/flows?scope=app"' in flows_html
    assert 'href="/traffic-dashboard/flows?scope=all"' in flows_html


def test_prefix_is_stripped_and_normalized(monkeypatch):
    """트레일링 슬래시/누락된 선행 슬래시를 정규화한다."""
    monkeypatch.setenv("DASHBOARD_URL_PREFIX", "traffic-dashboard/")
    assert dashboard_app._url_prefix() == "/traffic-dashboard"
    monkeypatch.setenv("DASHBOARD_URL_PREFIX", "")
    assert dashboard_app._url_prefix() == ""


# --------------------------------------------------------------------------- 설명 패널 + 흐름 다이어그램
def test_flows_page_shows_explanation_panel_even_when_empty(monkeypatch):
    """데이터가 없어도 '이 페이지가 무엇인지' 설명 패널은 항상 보여야 한다."""
    monkeypatch.setattr(
        dashboard_app.hubble_flows, "fetch_summary",
        lambda **kw: FlowSummary(total=0, shown=0, scope="app"),
    )
    text = _authed_client().get("/flows").text
    assert 'class="help"' in text
    assert "Hubble" in text
    assert "연결 방향" in text  # 다이어그램 읽는 법 설명


def test_flows_page_renders_inline_svg_diagram_when_data(monkeypatch):
    summary = FlowSummary(
        total=3, shown=3, app_flows=3, verdicts={"FORWARDED": 3},
        top_pairs=[{
            "src": "shop/checkout-abc", "dst": "shop/cart-def", "protocol": "TCP",
            "dst_port": 8080, "count": 3, "verdict": "FORWARDED",
            "last_seen": "2026-07-19T12:00:00.000000000Z",
        }],
    )
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    text = _authed_client().get("/flows").text
    assert 'class="diagram"' in text
    assert "<svg" in text and "marker-end" in text   # 화살표 있는 노드-링크 다이어그램
    assert "shop/checkout-abc" in text               # source 노드 라벨
    assert "shop/cart-def" in text                   # destination 노드 라벨


def test_flow_graph_svg_empty_for_no_edges():
    assert dashboard_app._flow_graph_svg([], []) == ""


def test_flow_graph_svg_limits_to_top_n():
    """그래프는 가독성을 위해 상위 N개 간선만 그린다(표에는 전체가 있음). 노드는 합성 가능."""
    edges = [
        {"src": f"ns/src-{i}", "dst": f"ns/dst-{i}", "protocol": "TCP",
         "dst_port": 80, "count": 100 - i, "verdict": "FORWARDED",
         "last_seen": "2026-07-19T12:00:00Z"}
        for i in range(20)
    ]
    svg = dashboard_app._flow_graph_svg([], edges, limit=10)  # nodes 비어도 합성됨
    # 상위 10개 간선의 노드만 그려진다 — 11번째는 없어야 한다.
    assert "ns/src-9" in svg
    assert "ns/src-10" not in svg


def test_flow_graph_svg_renders_multihop_chain():
    """A→B→C 사슬: 중간 노드 B는 한 번만 그려지고, 두 간선 모두 존재해야 한다(다음 단계 흐름)."""
    nodes = [
        {"label": "shop/a", "namespace": "shop", "workload": "front", "kind": "app", "layer": 0},
        {"label": "shop/b", "namespace": "shop", "workload": "mid", "kind": "app", "layer": 1},
        {"label": "shop/c", "namespace": "shop", "workload": "back", "kind": "app", "layer": 2},
    ]
    edges = [
        {"src": "shop/a", "dst": "shop/b", "protocol": "TCP", "dst_port": 80, "count": 5, "verdict": "FORWARDED"},
        {"src": "shop/b", "dst": "shop/c", "protocol": "TCP", "dst_port": 90, "count": 3, "verdict": "FORWARDED"},
    ]
    svg = dashboard_app._flow_graph_svg(nodes, edges)
    assert svg.count("shop/b</text>") == 1          # 중간 노드는 한 번만 (사슬로 이어짐)
    assert svg.count("marker-end") == 2             # 간선 두 개
    # 워크로드(리소스)가 칩 아랫줄에 표시된다.
    assert "▸ front" in svg and "▸ mid" in svg and "▸ back" in svg


def test_flow_graph_svg_spreads_edge_ports_to_avoid_overlap():
    """한 노드에서 나가는 여러 간선이 한 점에 몰리지 않고 서로 다른 y에서 출발해야 한다.

    (화살표 겹침 방지 — 포트를 칩 높이에 분산 배치했는지 검증.)
    """
    import re
    nodes = [
        {"label": "hub", "namespace": "shop", "workload": None, "kind": "app", "layer": 0},
        {"label": "b", "namespace": "shop", "workload": None, "kind": "app", "layer": 1},
        {"label": "c", "namespace": "shop", "workload": None, "kind": "app", "layer": 1},
        {"label": "d", "namespace": "shop", "workload": None, "kind": "app", "layer": 1},
    ]
    edges = [
        {"src": "hub", "dst": t, "protocol": "TCP", "dst_port": 80, "count": 1, "verdict": "FORWARDED"}
        for t in ("b", "c", "d")
    ]
    svg = dashboard_app._flow_graph_svg(nodes, edges)
    # 간선(cubic 'C' 포함)의 시작 y좌표만 뽑는다(화살표 마커 path는 'L'이라 제외됨).
    starts = re.findall(r'<path d="M[\d.]+,([\d.]+) C', svg)
    assert len(starts) == 3
    assert len(set(starts)) == 3   # 세 간선이 서로 다른 지점에서 출발


def test_flow_graph_svg_nodes_are_focus_links():
    nodes = [{"label": "shop/a", "namespace": "shop", "workload": None, "kind": "app", "layer": 0}]
    edges = [{"src": "shop/a", "dst": "shop/b", "protocol": "TCP", "dst_port": 80,
              "count": 1, "verdict": "FORWARDED"}]
    svg = dashboard_app._flow_graph_svg(nodes, edges)
    assert "focus=shop%2Fa" in svg and "focus=shop%2Fb" in svg


def test_flows_page_renders_multihop_graph_with_resources(monkeypatch):
    """페이지 렌더에 summary.nodes(계층/워크로드)가 그래프로 반영되는지 end-to-end로 확인."""
    summary = FlowSummary(
        total=3, shown=3, app_flows=3, verdicts={"FORWARDED": 3},
        top_pairs=[
            {"src": "shop/a", "dst": "shop/b", "protocol": "TCP", "dst_port": 80, "count": 3, "verdict": "FORWARDED", "last_seen": "2026-07-23T12:00:00Z"},
            {"src": "shop/b", "dst": "shop/c", "protocol": "TCP", "dst_port": 90, "count": 2, "verdict": "FORWARDED", "last_seen": "2026-07-23T12:00:00Z"},
        ],
        nodes=[
            {"label": "shop/a", "namespace": "shop", "workload": "front", "kind": "app", "layer": 0},
            {"label": "shop/b", "namespace": "shop", "workload": "mid", "kind": "app", "layer": 1},
            {"label": "shop/c", "namespace": "shop", "workload": "back", "kind": "app", "layer": 2},
        ],
    )
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    text = _authed_client().get("/flows").text
    assert "다음 단계" in text                 # 멀티홉 설명/캡션
    assert "▸ mid" in text                      # 중간 노드의 워크로드(리소스)
    assert text.count("shop/b</text>") == 1     # 중간 노드는 한 번만


def test_api_flows_includes_nodes(monkeypatch):
    summary = FlowSummary(total=1, shown=1, app_flows=1, verdicts={"FORWARDED": 1},
        top_pairs=[{"src": "shop/a", "dst": "shop/b", "protocol": "TCP", "dst_port": 80, "count": 1, "verdict": "FORWARDED", "last_seen": "t"}],
        nodes=[{"label": "shop/a", "namespace": "shop", "workload": "front", "kind": "app", "layer": 0}])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    payload = _authed_client().get("/api/flows").json()
    assert payload["nodes"][0]["workload"] == "front"
    assert payload["nodes"][0]["layer"] == 0


# --------------------------------------------------------------------------- namespace 필터 + focus(연결 리소스)
def _flows_summary(**kw) -> FlowSummary:
    base = dict(total=3, shown=3, app_flows=3, verdicts={"FORWARDED": 3}, top_pairs=[{
        "src": "shop/checkout-abc", "dst": "shop/cart-def", "protocol": "TCP",
        "dst_port": 8080, "count": 3, "verdict": "FORWARDED",
        "last_seen": "2026-07-19T12:00:00.000000000Z",
    }])
    base.update(kw)
    return FlowSummary(**base)


def test_flows_page_shows_namespace_filter_links(monkeypatch):
    """네임스페이스 목록이 선택 링크로 렌더되고, 각 링크는 scope를 유지한 /flows URL이어야 한다."""
    summary = _flows_summary(namespaces=[{"name": "shop", "count": 5}, {"name": "pay", "count": 2}])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    text = _authed_client().get("/flows").text
    assert "네임스페이스:" in text
    assert 'href="/flows?scope=app&amp;namespace=shop"' in text
    assert 'href="/flows?scope=app&amp;namespace=pay"' in text
    assert ">shop</a>" in text and "(5)" in text


def test_flows_page_marks_active_namespace(monkeypatch):
    summary = _flows_summary(namespace="shop", namespaces=[{"name": "shop", "count": 5}])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    text = _authed_client().get("/flows").text
    assert 'class="active">shop</a>' in text


def test_flows_source_destination_are_focus_links(monkeypatch):
    """표의 Source·Destination은 그 리소스에 focus를 거는 링크여야 한다(라벨의 / 는 %2F로 인코딩)."""
    summary = _flows_summary()
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    text = _authed_client().get("/flows").text
    assert "focus=shop%2Fcheckout-abc" in text
    assert "focus=shop%2Fcart-def" in text


def test_flows_focus_banner_and_clear_link(monkeypatch):
    """focus가 걸리면 배너 + 해제 링크(focus 없는 URL)를 보여준다."""
    summary = _flows_summary(focus="shop/checkout-abc", namespace="shop")
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    text = _authed_client().get("/flows").text
    assert "shop/checkout-abc" in text
    assert "선택 해제" in text
    # 해제 링크는 scope/namespace는 유지하되 focus는 빠진다.
    assert 'href="/flows?scope=app&amp;namespace=shop"' in text


def test_flows_scope_toggle_preserves_namespace_and_focus(monkeypatch):
    summary = _flows_summary(namespace="shop", focus="shop/checkout-abc",
                             namespaces=[{"name": "shop", "count": 5}])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    text = _authed_client().get("/flows").text
    # scope=all 토글 링크가 현재 namespace/focus를 그대로 실어야 한다.
    assert "scope=all" in text and "namespace=shop" in text and "focus=shop%2Fcheckout-abc" in text


def test_flows_empty_with_filter_hints_to_relax(monkeypatch):
    """필터 때문에 0건이면, '앱 흐름 없음'이 아니라 '필터 완화' 안내를 보여준다."""
    monkeypatch.setattr(
        dashboard_app.hubble_flows, "fetch_summary",
        lambda **kw: FlowSummary(total=10, shown=0, scope="app", namespace="shop"),
    )
    text = _authed_client().get("/flows").text
    assert "필터" in text


def test_flows_route_passes_namespace_and_focus_to_fetch(monkeypatch):
    captured = {}

    def fake(**kw):
        captured.update(kw)
        return FlowSummary(total=0)

    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", fake)
    _authed_client().get("/flows?scope=all&namespace=shop&focus=shop/checkout-abc")
    assert captured == {"scope": "all", "namespace": "shop", "focus": "shop/checkout-abc"}


def test_flows_route_blank_filters_become_none(monkeypatch):
    """빈/공백 파라미터는 None으로 정규화해 '필터 없음'과 같게 취급한다."""
    captured = {}
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: captured.update(kw) or FlowSummary(total=0))
    _authed_client().get("/flows?namespace=&focus=%20%20")
    assert captured["namespace"] is None
    assert captured["focus"] is None


def test_api_flows_includes_namespace_focus_and_namespaces(monkeypatch):
    summary = _flows_summary(namespace="shop", focus="shop/checkout-abc",
                             namespaces=[{"name": "shop", "count": 5}])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: summary)
    payload = _authed_client().get("/api/flows").json()
    assert payload["namespace"] == "shop"
    assert payload["focus"] == "shop/checkout-abc"
    assert payload["namespaces"] == [{"name": "shop", "count": 5}]


# --------------------------------------------------------------------------- HTTP Basic 인증
def test_healthz_is_public_without_credentials():
    """프로브 경로 /healthz는 자격증명 없이도 200이어야 한다(k8s liveness/readiness)."""
    resp = TestClient(dashboard_app.app).get("/healthz")
    assert resp.status_code == 200


def test_protected_route_requires_auth_returns_401_with_challenge(monkeypatch):
    """자격증명 없이 보호 경로 접근 시 401 + WWW-Authenticate(Basic) 챌린지를 줘야 한다."""
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    resp = TestClient(dashboard_app.app).get("/")  # 인증 헤더 없음
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_api_route_is_also_protected(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    resp = TestClient(dashboard_app.app).get("/api/policies")
    assert resp.status_code == 401


def test_wrong_credentials_rejected(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    client = TestClient(dashboard_app.app)
    client.headers.update(_basic_header(_TEST_USER, "wrong-password"))
    assert client.get("/").status_code == 401


def test_malformed_authorization_header_rejected(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    client = TestClient(dashboard_app.app)
    client.headers.update({"Authorization": "Basic not-valid-base64!!"})
    assert client.get("/").status_code == 401


def test_valid_credentials_allowed(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    resp = _authed_client().get("/")
    assert resp.status_code == 200


def test_unconfigured_auth_fails_closed_with_503(monkeypatch):
    """DASHBOARD_USERNAME/PASSWORD 미설정이면 인증 불가 => 공개하지 않고 503으로 막는다."""
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    # 자격증명을 보내더라도 서버가 설정 안 됐으면 503(열리면 안 됨).
    client = TestClient(dashboard_app.app)
    client.headers.update(_basic_header(_TEST_USER, _TEST_PASS))
    resp = client.get("/")
    assert resp.status_code == 503
    # 그래도 프로브는 살아 있어야 한다.
    assert TestClient(dashboard_app.app).get("/healthz").status_code == 200
