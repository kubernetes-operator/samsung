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

# 대시보드는 /healthz·/login·/logout을 뺀 모든 경로가 로그인(세션 쿠키)으로 보호되며,
# 저장된 자격증명과 맞는 HTTP Basic도 허용한다. 대부분의 테스트는 엔드포인트의 '내용'을
# 검증하므로, 자격증명 저장소를 임시 파일로 격리(기본 admin/password)하고 Basic 헤더를 기본
# 탑재한 클라이언트(_authed_client)를 쓴다. 로그인/설정 '동작'은 별도 섹션에서 검증한다.
_TEST_USER = "admin"
_TEST_PASS = "password"


def _basic_header(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(autouse=True)
def _dashboard_auth_env(monkeypatch, tmp_path):
    """각 테스트를 격리: 자격증명 저장소를 임시 파일로(기본 admin/password), 캐시 초기화."""
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DASHBOARD_CREDENTIALS_PATH", str(tmp_path / "creds.json"))
    dashboard_app.auth.reset_store_cache()
    yield
    dashboard_app.auth.reset_store_cache()


def _authed_client() -> TestClient:
    client = TestClient(dashboard_app.app)
    client.headers.update(_basic_header(_TEST_USER, _TEST_PASS))
    return client


def _logged_in_client(user: str = _TEST_USER, password: str = _TEST_PASS) -> TestClient:
    """폼 로그인으로 세션 쿠키를 받은 클라이언트."""
    client = TestClient(dashboard_app.app)
    resp = client.post("/login", data={"username": user, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 302
    assert dashboard_app.auth.COOKIE_NAME in client.cookies
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


def test_policies_page_returns_html_table(client_with_policies):
    resp = client_with_policies.get("/policies")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "checkout-policy" in resp.text
    assert "<table>" in resp.text


def test_policies_page_shows_empty_state_when_no_policies(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    client = _authed_client()
    resp = client.get("/policies")
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
    resp = client.get("/policies")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_error_row_rendered_for_fetch_failure(monkeypatch):
    err_summary = data.PolicySummary(namespace="shop", name="(조회 실패)", phase="Error", raw_error="권한 없음: 403")
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [err_summary])
    client = _authed_client()
    resp = client.get("/policies")
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


def test_menu_bar_present_on_both_pages(monkeypatch):
    """메뉴바(브랜드 + 트래픽 흐름/정책 현황 탭)가 두 페이지 모두에 있어야 한다."""
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: FlowSummary(total=0))
    client = _authed_client()
    for path in ("/", "/policies"):
        text = client.get(path).text
        assert 'class="topbar"' in text                 # seoul 스타일 상단바 컨테이너
        assert 'class="home-link"' in text              # 제목=홈 링크
        assert 'href="/"' in text and 'href="/policies"' in text
        assert "트래픽 흐름" in text and "정책 현황" in text


def test_brand_title_is_blue_and_bold(monkeypatch):
    """상단 'TrafficPolicy' 브랜드 제목은 앱 강조색(파란 --info) + 굵게여야 한다."""
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    text = _authed_client().get("/").text
    rule = text.split(".home-link")[1].split("}")[0]
    assert "color: var(--info)" in rule
    assert "font-weight: 800" in rule


def test_main_page_is_traffic_flows(monkeypatch):
    """메인('/')은 정책이 아니라 트래픽 흐름 화면이어야 한다."""
    monkeypatch.setattr(
        dashboard_app.hubble_flows, "fetch_summary",
        lambda **kw: FlowSummary(total=0, shown=0, scope="app"),
    )
    text = _authed_client().get("/").text
    assert "Hubble" in text                       # 흐름 페이지 설명 패널
    assert 'class="tabs"' in text
    # '트래픽 흐름' 탭이 활성(seoul .tab.active 스타일).
    assert 'href="/" class="tab active"' in text


def test_flows_alias_still_works(monkeypatch):
    """예전 '/flows' 링크도 동일한 흐름 화면을 계속 보여줘야 한다(하위호환)."""
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    assert "Hubble" in _authed_client().get("/flows").text


# --- 자동 새로고침(기본 1분 · 10초/30초/1분/10분/끄기 선택) ----------------
def _flows_client(monkeypatch):
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    return _authed_client()


def test_default_refresh_is_one_minute(monkeypatch):
    """기본 자동 새로고침은 1분(60초)이어야 한다."""
    assert 'http-equiv="refresh" content="60"' in _flows_client(monkeypatch).get("/").text


def test_refresh_selector_has_all_five_options(monkeypatch):
    text = _flows_client(monkeypatch).get("/").text
    assert 'class="refresh-seg"' in text            # 셀 형태 세그먼트 버튼
    for label in ("10초", "30초", "1분", "10분", "안 함"):
        assert label in text


def test_refresh_selector_is_in_topbar(monkeypatch):
    """자동 새로고침 컨트롤은 본문이 아니라 상단바(.topbar-right) 안에 있어야 한다."""
    text = _flows_client(monkeypatch).get("/").text
    topbar = text.split('<header class="topbar">')[1].split("</header>")[0]
    body = text.split("</header>")[1]
    assert 'class="refresh-seg"' in topbar          # 상단바 안에 존재
    assert "refresh-seg" not in body                # 본문에는 없음
    # 현재 선택(기본 1분)이 active 셀로 강조되어야 한다.
    assert 'class="seg active"' in text and 'aria-current="true"' in text


def test_refresh_override_changes_meta(monkeypatch):
    c = _flows_client(monkeypatch)
    assert 'content="10"' in c.get("/?refresh=10").text
    assert 'content="600"' in c.get("/?refresh=600").text


def test_refresh_off_removes_meta_tag(monkeypatch):
    text = _flows_client(monkeypatch).get("/?refresh=off").text
    assert 'http-equiv="refresh"' not in text
    assert "refresh=off" in text            # 선택 컨트롤 링크는 그대로 있어야 함


def test_invalid_refresh_falls_back_to_default(monkeypatch):
    assert 'content="60"' in _flows_client(monkeypatch).get("/?refresh=bogus").text


def test_refresh_preserved_in_flow_filter_links(monkeypatch):
    """refresh 선택은 스코프/필터 링크에도 실려 새로고침 시 유지되어야 한다."""
    text = _flows_client(monkeypatch).get("/?refresh=off").text
    assert text.count("refresh=off") >= 2   # 스코프 토글(app/all) 등


def test_policies_page_refresh_selector_and_meta(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    text = _authed_client().get("/policies?refresh=30").text
    assert 'content="30"' in text
    assert "자동 새로고침 30초" in text
    assert "/policies?refresh=600" in text  # 옵션 링크가 정책 경로 기준


def test_links_use_external_prefix_when_configured(monkeypatch):
    """서브패스(/traffic-dashboard) 노출 시 nav/토글 링크가 프리픽스를 포함해야 한다.

    프리픽스 없이 절대경로 '/flows'를 쓰면 브라우저가 프리픽스 밖으로 나가 게이트웨이에서
    404가 난다(실제 발생했던 버그). 라우트 자체는 게이트웨이가 프리픽스를 떼므로 '/'/'/flows'.
    """
    monkeypatch.setenv("DASHBOARD_URL_PREFIX", "/traffic-dashboard")
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary", lambda **kw: FlowSummary(total=0))
    client = _authed_client()

    # 메뉴바 링크가 프리픽스를 포함해야 한다(트래픽 흐름=루트, 정책 현황=/policies).
    root_html = client.get("/").text
    assert 'href="/traffic-dashboard/"' in root_html
    assert 'href="/traffic-dashboard/policies"' in root_html

    # 스코프 토글 등 흐름 필터 링크도 프리픽스를 포함하고, 메인(루트) 기준이어야 한다.
    # (링크에는 현재 refresh 값도 함께 실린다 — trailing quote 없이 검사.)
    assert 'href="/traffic-dashboard/?scope=app' in root_html
    assert 'href="/traffic-dashboard/?scope=all' in root_html


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


def test_flow_graph_edges_uniform_width_and_speed_by_count():
    """화살표는 두께 균일 + '전기 흐르듯' 애니메이션 주기가 연결 수에 반비례(많을수록 빠름)."""
    import re
    nodes = [
        {"label": "hub", "namespace": "x", "workload": None, "kind": "app", "layer": 0},
        {"label": "busy", "namespace": "x", "workload": None, "kind": "app", "layer": 1},
        {"label": "quiet", "namespace": "x", "workload": None, "kind": "app", "layer": 1},
    ]
    edges = [
        {"src": "hub", "dst": "busy", "protocol": "TCP", "dst_port": 80, "count": 100, "verdict": "FORWARDED"},
        {"src": "hub", "dst": "quiet", "protocol": "TCP", "dst_port": 80, "count": 2, "verdict": "FORWARDED"},
    ]
    svg = dashboard_app._flow_graph_svg(nodes, edges)
    widths = set(re.findall(r'class="flow-edge"[^>]*stroke-width="([\d.]+)"', svg))
    assert widths == {"2.6"}                         # 모든 화살표 두께 동일(연결 수와 무관)
    durs = sorted(float(d) for d in re.findall(r"animation-duration:([\d.]+)s", svg))
    assert len(durs) == 2 and durs[0] < durs[1]      # 연결 많은 쪽이 더 짧은 주기(빠른 흐름)
    assert abs(durs[0] - 0.5) < 0.01                 # 최다 연결 = 가장 빠름(DUR_FAST)
    assert abs(durs[1] - 3.0) < 0.01                 # 최소 연결 = 가장 느림(DUR_SLOW)


def test_flow_graph_shows_actual_counts():
    """실제 연결 수가 화살표에 숫자 텍스트로 표기된다."""
    nodes = [{"label": "a", "namespace": "x", "workload": None, "kind": "app", "layer": 0}]
    edges = [{"src": "a", "dst": "b", "protocol": "TCP", "dst_port": 80, "count": 42, "verdict": "FORWARDED"}]
    svg = dashboard_app._flow_graph_svg(nodes, edges)
    assert ">42</text>" in svg


def test_flows_page_includes_flow_animation_css(monkeypatch):
    """페이지에 흐름 애니메이션 CSS와 '전기' 설명이 포함된다(모션 최소화 대응 포함)."""
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    text = _authed_client().get("/").text
    assert "@keyframes flow-dash" in text
    assert "prefers-reduced-motion" in text
    assert "전기" in text


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
    text = _authed_client().get("/").text
    assert "네임스페이스:" in text
    assert 'href="/?scope=app&amp;namespace=shop' in text
    assert 'href="/?scope=app&amp;namespace=pay' in text
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
    text = _authed_client().get("/").text
    assert "shop/checkout-abc" in text
    assert "선택 해제" in text
    # 해제 링크는 scope/namespace는 유지하되 focus는 빠진다(메인=루트 기준).
    assert 'href="/?scope=app&amp;namespace=shop' in text


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


def test_unauthenticated_html_redirects_to_login():
    """자격증명 없이 보호 HTML 경로 접근 시 로그인 폼으로 302 리다이렉트."""
    resp = TestClient(dashboard_app.app).get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


def test_api_route_is_also_protected():
    """API 경로는 브라우저 리다이렉트 대신 401(스크립트 친화)."""
    resp = TestClient(dashboard_app.app).get("/api/policies")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_wrong_basic_credentials_rejected():
    client = TestClient(dashboard_app.app)
    client.headers.update(_basic_header(_TEST_USER, "wrong-password"))
    assert client.get("/api/policies").status_code == 401


def test_malformed_authorization_header_rejected():
    client = TestClient(dashboard_app.app)
    client.headers.update({"Authorization": "Basic not-valid-base64!!"})
    assert client.get("/api/policies").status_code == 401


def test_valid_basic_credentials_allowed(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    resp = _authed_client().get("/policies")
    assert resp.status_code == 200


def test_default_credentials_are_admin_password(monkeypatch):
    """저장소가 비어 있으면 기본 자격증명은 admin/password여야 한다."""
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    client = TestClient(dashboard_app.app)
    client.headers.update(_basic_header("admin", "password"))
    assert client.get("/policies").status_code == 200


def test_healthz_is_public():
    assert TestClient(dashboard_app.app).get("/healthz").status_code == 200


# --- 폼 로그인 / 로그아웃 --------------------------------------------------
def test_login_page_renders():
    resp = TestClient(dashboard_app.app).get("/login")
    assert resp.status_code == 200
    assert "로그인" in resp.text and 'name="password"' in resp.text


def test_login_success_sets_session_and_allows_access(monkeypatch):
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    client = _logged_in_client()
    assert client.get("/").status_code == 200


def test_login_failure_shows_error():
    resp = TestClient(dashboard_app.app).post(
        "/login", data={"username": "admin", "password": "nope"})
    assert resp.status_code == 401
    assert "올바르지 않습니다" in resp.text


def test_logout_clears_session(monkeypatch):
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    client = _logged_in_client()
    assert client.get("/").status_code == 200
    client.post("/logout", follow_redirects=False)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302 and "/login" in resp.headers["location"]


def test_topbar_shows_user_and_logout_when_logged_in(monkeypatch):
    """세션 로그인 시 상단바에 사용자명 + 로그아웃이 보여야 한다(contextvar 전파 확인)."""
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    text = _logged_in_client().get("/").text
    assert "로그아웃" in text and "admin" in text


def test_brand_title_has_no_daeshboard_word(monkeypatch):
    """메인 상단 브랜드는 '대시보드' 없이 'TrafficPolicy'만 표기한다."""
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    text = _logged_in_client().get("/").text
    brand = text.split('class="home-link">')[1].split("</a>")[0]
    assert brand == "TrafficPolicy"


# --- 설정(자격증명 변경) ---------------------------------------------------
def test_settings_requires_auth():
    resp = TestClient(dashboard_app.app).get("/settings", follow_redirects=False)
    assert resp.status_code == 302 and "/login" in resp.headers["location"]


def test_change_password_updates_credentials(monkeypatch):
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    client = _logged_in_client()
    resp = client.post("/settings", data={
        "current_password": "password", "new_username": "admin",
        "new_password": "newsecret", "confirm_password": "newsecret"})
    assert resp.status_code == 200 and "변경되었습니다" in resp.text
    # 새 비밀번호는 통과, 옛 비밀번호는 거부.
    ok = TestClient(dashboard_app.app)
    ok.headers.update(_basic_header("admin", "newsecret"))
    assert ok.get("/policies").status_code == 200
    old = TestClient(dashboard_app.app)
    old.headers.update(_basic_header("admin", "password"))
    assert old.get("/api/policies").status_code == 401


def test_change_username_and_login_with_new_name(monkeypatch):
    monkeypatch.setattr(dashboard_app.data, "fetch_policies", lambda: [])
    client = _logged_in_client()
    resp = client.post("/settings", data={
        "current_password": "password", "new_username": "operator",
        "new_password": "", "confirm_password": ""})
    assert resp.status_code == 200
    assert dashboard_app.auth.get_store().username == "operator"


def test_change_password_wrong_current_rejected():
    client = _logged_in_client()
    resp = client.post("/settings", data={
        "current_password": "WRONG", "new_username": "admin",
        "new_password": "newsecret", "confirm_password": "newsecret"})
    assert resp.status_code == 400 and "현재 비밀번호" in resp.text


def test_change_password_mismatch_rejected():
    client = _logged_in_client()
    resp = client.post("/settings", data={
        "current_password": "password", "new_username": "admin",
        "new_password": "aaaa", "confirm_password": "bbbb"})
    assert resp.status_code == 400 and "일치하지 않" in resp.text


def test_password_change_rotates_other_sessions(monkeypatch):
    """비밀번호 변경 시 다른 기기의 기존 세션은 무효화되어야 한다(세션 키 회전)."""
    monkeypatch.setattr(dashboard_app.hubble_flows, "fetch_summary",
                        lambda **kw: FlowSummary(total=0, shown=0, scope="app"))
    changer = _logged_in_client()
    other = _logged_in_client()          # 같은 자격증명으로 로그인한 다른 세션
    assert other.get("/", follow_redirects=False).status_code == 200
    changer.post("/settings", data={
        "current_password": "password", "new_username": "admin",
        "new_password": "newsecret", "confirm_password": "newsecret"})
    # 기존 세션(other)의 쿠키는 이제 무효 → 로그인으로 리다이렉트.
    resp = other.get("/", follow_redirects=False)
    assert resp.status_code == 302 and "/login" in resp.headers["location"]
