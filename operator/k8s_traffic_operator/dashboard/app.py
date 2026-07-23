"""읽기 전용 웹 대시보드 - TrafficPolicy 운영 현황 + Cilium Hubble 실시간 Pod 트래픽 흐름.

실행:
    uvicorn k8s_traffic_operator.dashboard.app:app --host 0.0.0.0 --port 8080

- `GET /`             : TrafficPolicy 현황 HTML (10초마다 자동 새로고침)
- `GET /api/policies` : 동일 데이터의 JSON
- `GET /flows`        : Cilium Hubble 기반 실제 Pod 트래픽 흐름 HTML
- `GET /api/flows`    : 동일 데이터의 JSON
- `GET /healthz`      : liveness/readiness 프로브용

`/`와 `/flows`는 서로 다른 성격의 데이터다. `/`는 오퍼레이터가 판단한 정책 상태(Gateway API
트래픽 메트릭 기반)이고, `/flows`는 CNI(Cilium) 레벨에서 관측된 실제 L3/L4 연결이다 —
후자를 전자의 RPS인 것처럼 섞어 보여주지 않는다(단위와 의미가 다르므로 오해 방지).

이 앱은 클러스터에 아무것도 쓰지 않는다. `data.fetch_policies()`/`hubble_flows.fetch_summary()`가
유일한 접근 지점이며 조회만 수행한다.

`/healthz`를 제외한 모든 경로는 HTTP Basic 인증으로 보호된다(자격증명은 env
DASHBOARD_USERNAME/DASHBOARD_PASSWORD로 주입, 미설정 시 503 fail-closed). 아래 인증
미들웨어 참고.
"""

from __future__ import annotations

import base64
import binascii
import html
import os
import secrets
import time
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import data, hubble_flows
from .data import PolicySummary
from .hubble_flows import FlowSummary

app = FastAPI(title="TrafficPolicy Dashboard", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------- 인증(HTTP Basic)
# 이 대시보드는 test2.studiobasa.com 아래로 공개 노출되므로 HTTP Basic 인증으로 보호한다.
# 자격증명은 **코드/이미지에 절대 넣지 않고** 런타임 env(DASHBOARD_USERNAME/PASSWORD)로만
# 주입한다(배포에서는 K8s Secret -> env). env가 비어 있으면 "설정 누락"으로 보고 모든 보호
# 경로를 503으로 막는다(fail-closed) — 인증이 안 걸린 채 실수로 공개되는 상황을 방지.
# /healthz만 예외(쿠버네티스 프로브가 자격증명 없이 접근해야 함).
_AUTH_REALM = "TrafficPolicy Dashboard"
_PUBLIC_PATHS = frozenset({"/healthz"})


def _auth_credentials() -> Tuple[Optional[str], Optional[str]]:
    """설정된 (username, password)를 env에서 읽는다(요청마다 읽어 재기동 없이 반영/테스트 용이)."""
    return os.getenv("DASHBOARD_USERNAME") or None, os.getenv("DASHBOARD_PASSWORD") or None


def _parse_basic(header: str) -> Optional[Tuple[str, str]]:
    """Authorization 헤더에서 Basic 자격증명을 (user, password)로 파싱. 형식 불량이면 None."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    return user, password


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    username, password = _auth_credentials()
    if not username or not password:
        # 자격증명 미설정 = 보호 불가 => 열어두지 않고 막는다(fail-closed).
        return PlainTextResponse(
            "Dashboard authentication is not configured (DASHBOARD_USERNAME/PASSWORD).",
            status_code=503,
        )

    unauthorized = Response(
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_AUTH_REALM}"'},
    )
    creds = _parse_basic(request.headers.get("Authorization", ""))
    if creds is None:
        return unauthorized
    # 사용자명/비밀번호 모두 상수 시간 비교(타이밍 공격 방지). 단축 평가로 새지 않도록 둘 다 계산.
    user_ok = secrets.compare_digest(creds[0], username)
    pass_ok = secrets.compare_digest(creds[1], password)
    if not (user_ok and pass_ok):
        return unauthorized
    return await call_next(request)

_SEVERITY_COLOR = {"none": "#4b5563", "warning": "#b45309", "critical": "#b91c1c"}
_PHASE_COLOR = {
    "Reconciled": "#15803d", "ok": "#15803d",
    "no_data": "#6b7280", "collection_failed": "#b45309",
    "Registered": "#2563eb", "Pending": "#6b7280", "Error": "#b91c1c",
}
_VERDICT_COLOR = {"FORWARDED": "#15803d", "DROPPED": "#b91c1c", "ERROR": "#b91c1c", "AUDIT": "#b45309"}

_STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "Segoe UI", sans-serif; margin: 0; padding: 2rem;
         background: #0b0d12; color: #e5e7eb; }
  @media (prefers-color-scheme: light) { body { background: #f8fafc; color: #111827; } }
  h1 { font-size: 1.25rem; margin-bottom: .25rem; }
  nav { margin-bottom: 1rem; font-size: .85rem; }
  nav a { color: #60a5fa; text-decoration: none; margin-right: 1rem; }
  a { color: #60a5fa; }
  a.active { color: inherit; font-weight: 600; text-decoration: underline; }
  .meta { color: #9ca3af; font-size: .85rem; margin-bottom: 1.5rem; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  th { text-align: left; padding: .5rem .75rem; border-bottom: 2px solid #374151; color: #9ca3af;
        font-weight: 600; text-transform: uppercase; font-size: .75rem; }
  td { padding: .6rem .75rem; border-bottom: 1px solid #1f2937; vertical-align: top; }
  @media (prefers-color-scheme: light) {
    th { border-bottom-color: #e5e7eb; } td { border-bottom-color: #f1f5f9; }
  }
  .sub { color: #9ca3af; font-size: .78rem; margin-top: .15rem; }
  .badge { display: inline-block; padding: .15rem .5rem; border-radius: .35rem; color: white;
            font-size: .78rem; font-weight: 600; }
  .error-row td { color: #b91c1c; }
  .empty { color: #9ca3af; padding: 2rem 0; text-align: center; }
  code { font-size: .82em; }
"""

_PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta http-equiv="refresh" content="10">
<style>{style}</style>
</head>
<body>
<nav>
  <a href="{prefix}/" class="{nav_policies}">TrafficPolicy 현황</a>
  <a href="{prefix}/flows" class="{nav_flows}">실시간 Pod 트래픽 흐름 (Hubble)</a>
</nav>
<h1>{heading}</h1>
<div class="meta">{meta}</div>
{table}
</body>
</html>"""


def _url_prefix() -> str:
    """대시보드가 외부에 노출되는 경로 프리픽스(예: '/traffic-dashboard').

    Gateway(HTTPRoute)가 이 프리픽스를 떼고 백엔드로 전달하므로 FastAPI 라우트 자체는
    '/', '/flows'로 두지만, HTML 안의 링크에는 이 프리픽스를 붙여야 브라우저가 프리픽스
    포함 URL로 재요청한다(안 붙이면 '/flows'가 프리픽스 밖으로 나가 게이트웨이에서 404).
    env DASHBOARD_URL_PREFIX로 설정하며, 비면(로컬 실행/테스트) 절대경로 그대로 쓴다.
    """
    raw = (os.getenv("DASHBOARD_URL_PREFIX") or "").strip().rstrip("/")
    if raw and not raw.startswith("/"):
        raw = "/" + raw
    return raw


def _page(*, active: str, title: str, heading: str, meta: str, table: str) -> str:
    return _PAGE_TEMPLATE.format(
        title=title, style=_STYLE, heading=heading, meta=meta, table=table,
        prefix=_url_prefix(),
        nav_policies="active" if active == "policies" else "",
        nav_flows="active" if active == "flows" else "",
    )


def _fmt_age(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# --------------------------------------------------------------------------- TrafficPolicy 현황 (/)
def _policy_row_html(p: PolicySummary) -> str:
    if p.raw_error:
        return (
            f'<tr class="error-row">'
            f'<td colspan="7">⚠ {html.escape(p.namespace)}: {html.escape(p.raw_error)}</td>'
            f"</tr>"
        )
    phase_color = _PHASE_COLOR.get(p.phase, "#6b7280")
    sev_color = _SEVERITY_COLOR.get(p.last_severity, "#4b5563")
    applied = "-" if p.last_actuation_applied is None else ("✅" if p.last_actuation_applied else "—")
    return f"""
    <tr>
      <td>{html.escape(p.namespace)}</td>
      <td><strong>{html.escape(p.name)}</strong><div class="sub">{html.escape(p.http_route)} → {html.escape(p.deployment)}</div></td>
      <td><span class="badge" style="background:{phase_color}">{html.escape(p.phase)}</span></td>
      <td>{html.escape(p.last_snapshot_status)}</td>
      <td><span class="badge" style="background:{sev_color}">{html.escape(p.last_action)}</span>
          <div class="sub" title="{html.escape(p.last_reason)}">{html.escape(p.last_reason[:80])}</div></td>
      <td>{applied}<div class="sub">{html.escape(p.last_actuation_detail[:60])}</div></td>
      <td>{_fmt_age(p.last_reconcile_age_s)} ago<div class="sub">age {_fmt_age(p.age_s)}</div></td>
    </tr>"""


def _render_policies(policies: List[PolicySummary]) -> str:
    if not policies:
        table = '<div class="empty">TrafficPolicy가 없습니다.</div>'
    else:
        rows = "\n".join(_policy_row_html(p) for p in policies)
        table = f"""<table>
<thead><tr>
  <th>Namespace</th><th>Name</th><th>Phase</th><th>Snapshot</th>
  <th>Last Decision</th><th>Applied</th><th>Reconciled</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
    return _page(
        active="policies", title="TrafficPolicy Dashboard", heading="TrafficPolicy Dashboard",
        meta=f"트래픽 기반 자동운영 오퍼레이터 · 읽기 전용 · 10초마다 새로고침 · {len(policies)}개 정책",
        table=table,
    )


# --------------------------------------------------------------------------- Hubble 트래픽 흐름 (/flows)
def _flow_pair_row_html(pair: dict) -> str:
    port = f":{pair['dst_port']}" if pair.get("dst_port") else ""
    return f"""
    <tr>
      <td>{html.escape(pair['src'])}</td>
      <td>→</td>
      <td>{html.escape(pair['dst'])}{html.escape(port)}</td>
      <td>{html.escape(pair['protocol'])}</td>
      <td><strong>{pair['count']}</strong></td>
      <td class="sub">{html.escape(pair['last_seen'][:19].replace('T', ' '))}</td>
    </tr>"""


def _scope_toggle_html(scope: str) -> str:
    """애플리케이션 전용/전체 트래픽 전환 링크."""
    prefix = _url_prefix()
    app_cls = "active" if scope == "app" else ""
    all_cls = "active" if scope == "all" else ""
    return (
        '<div style="margin-bottom:1rem;font-size:.85rem">보기: '
        f'<a href="{prefix}/flows?scope=app" class="{app_cls}">내 애플리케이션 트래픽</a> · '
        f'<a href="{prefix}/flows?scope=all" class="{all_cls}">전체(인프라 포함)</a></div>'
    )


def _render_flows(summary: FlowSummary) -> str:
    toggle = _scope_toggle_html(summary.scope)
    scope_label = "내 애플리케이션 Pod" if summary.scope == "app" else "전체(인프라 포함)"

    if summary.fetch_error:
        table = toggle + (
            f'<div class="error-row" style="padding:1rem">⚠ Hubble 조회 실패: '
            f'{html.escape(summary.fetch_error)}</div>'
        )
        meta = "Cilium Hubble 기반 실제 Pod 트래픽 흐름 · 조회 실패"
    elif summary.shown == 0:
        # 전체는 있는데 앱 흐름만 0인 경우와, 애초에 아무 흐름도 없는 경우를 구분해서 안내한다.
        if summary.scope == "app" and summary.total > 0:
            empty = (
                '<div class="empty">최근 창에서 애플리케이션 Pod 흐름이 관측되지 않았습니다. '
                '전체 흐름은 있으니 아래 "전체(인프라 포함)"로 확인하세요.</div>'
            )
        else:
            empty = '<div class="empty">관측된 흐름이 없습니다.</div>'
        table = toggle + empty
        meta = f"Cilium Hubble 기반 · {scope_label} 0건 (전체 {summary.total}건)"
    else:
        badges = " ".join(
            f'<span class="badge" style="background:{_VERDICT_COLOR.get(v, "#4b5563")}">{html.escape(v)} {c}</span>'
            for v, c in summary.verdicts.items()
        )
        rows = "\n".join(_flow_pair_row_html(p) for p in summary.top_pairs)
        table = toggle + f"""<div style="margin-bottom:1rem">{badges}</div>
<table>
<thead><tr>
  <th>Source</th><th></th><th>Destination</th><th>Proto</th><th>Count</th><th>Last Seen (UTC)</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
        meta = (
            f"Cilium Hubble 기반 실제 Pod 트래픽 흐름(L3/L4, HTTP RPS 아님) · {scope_label} · "
            f"최근 전체 {summary.total}건 중 애플리케이션 {summary.app_flows}건, "
            f"현재 보기 {summary.shown}건에서 상위 {len(summary.top_pairs)}개 연결 쌍"
        )
    return _page(
        active="flows", title="Pod Traffic Flows (Hubble)", heading="실시간 Pod 트래픽 흐름",
        meta=meta, table=table,
    )


def _normalize_scope(scope: str) -> str:
    return "all" if scope == "all" else "app"


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return _render_policies(data.fetch_policies())


@app.get("/api/policies", response_class=JSONResponse)
def api_policies():
    policies = data.fetch_policies()
    return {
        "generatedAt": time.time(),
        "count": len(policies),
        "policies": [p.__dict__ for p in policies],
    }


@app.get("/flows", response_class=HTMLResponse)
def flows(scope: str = "app") -> str:
    return _render_flows(hubble_flows.fetch_summary(scope=_normalize_scope(scope)))


@app.get("/api/flows", response_class=JSONResponse)
def api_flows(scope: str = "app"):
    summary = hubble_flows.fetch_summary(scope=_normalize_scope(scope))
    return {
        "generatedAt": time.time(),
        "total": summary.total,
        "shown": summary.shown,
        "scope": summary.scope,
        "appFlows": summary.app_flows,
        "verdicts": summary.verdicts,
        "topPairs": summary.top_pairs,
        "fetchError": summary.fetch_error,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
