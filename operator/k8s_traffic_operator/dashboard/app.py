"""읽기 전용 웹 대시보드 - TrafficPolicy 운영 현황 + Cilium Hubble 실시간 Pod 트래픽 흐름.

실행:
    uvicorn k8s_traffic_operator.dashboard.app:app --host 0.0.0.0 --port 8080

- `GET /`             : 메인 화면 = Cilium Hubble 기반 실제 Pod 트래픽 흐름 HTML (기본 1분마다 자동 새로고침, ?refresh로 10초/30초/1분/10분/끄기 선택)
- `GET /flows`        : 위와 동일한 트래픽 흐름 HTML (예전 링크 호환용 별칭)
- `GET /policies`     : TrafficPolicy 현황 HTML (메뉴바의 '정책 현황')
- `GET /api/flows`    : 트래픽 흐름 데이터의 JSON
- `GET /api/policies` : 정책 현황 데이터의 JSON
- `GET /healthz`      : liveness/readiness 프로브용

`/`와 `/flows`는 서로 다른 성격의 데이터다. `/`는 오퍼레이터가 판단한 정책 상태(Gateway API
트래픽 메트릭 기반)이고, `/flows`는 CNI(Cilium) 레벨에서 관측된 실제 L3/L4 연결이다 —
후자를 전자의 RPS인 것처럼 섞어 보여주지 않는다(단위와 의미가 다르므로 오해 방지).

이 앱은 클러스터에 아무것도 쓰지 않는다. `data.fetch_policies()`/`hubble_flows.fetch_summary()`가
유일한 접근 지점이며 조회만 수행한다.

`/healthz`·`/login`·`/logout`을 제외한 모든 경로는 **폼 로그인(세션 쿠키)** 으로 보호된다.
자격증명은 `auth.CredentialStore`가 관리하며 기본값은 admin/password, `/settings`에서
아이디·비밀번호를 바꿀 수 있다(자세한 건 `auth.py`). 프로그램/스크립트 접근 편의를 위해
HTTP Basic 자격증명(저장된 값과 일치)도 함께 허용한다. 아래 인증 미들웨어 참고.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import html
import os
import time
from collections import defaultdict
from typing import List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth, data, hubble_flows
from .data import PolicySummary
from .hubble_flows import FlowSummary

app = FastAPI(title="TrafficPolicy Dashboard", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------- 인증(폼 로그인 + 세션)
# 이 대시보드는 test2.studiobasa.com 아래로 공개 노출되므로 로그인으로 보호한다.
# 자격증명은 auth.CredentialStore가 관리한다(기본 admin/password, /settings에서 변경 가능,
# 볼륨 파일에 저장 — 클러스터에는 여전히 아무것도 쓰지 않는다). 브라우저는 로그인 폼 →
# 서명 세션 쿠키로 인증하고, 스크립트/API는 저장된 자격증명과 일치하는 HTTP Basic도 허용한다.
# /healthz(프로브)·/login·/logout만 인증 없이 접근 가능하다.
_AUTH_REALM = "TrafficPolicy Dashboard"
_PUBLIC_PATHS = frozenset({"/healthz", "/login", "/logout"})

# 현재 요청의 로그인 사용자명(상단바 표시용). 미들웨어가 요청마다 세팅한다.
_current_user_var: contextvars.ContextVar = contextvars.ContextVar("dashboard_user", default=None)


def current_user() -> Optional[str]:
    return _current_user_var.get()


# --------------------------------------------------------------------------- 자동 새로고침
# 콘텐츠 페이지(트래픽 흐름/정책 현황)는 meta http-equiv=refresh로 주기적으로 다시 로드된다.
# 기본 1분이며 쿼리 `refresh`로 10초/30초/1분/10분/끄기를 고를 수 있다. meta refresh는 현재
# URL을 그대로 다시 부르므로, refresh 값을 각 링크에 실어두면 필터/선택이 새로고침에도 유지된다.
_REFRESH_OPTIONS = [("10", "10초"), ("30", "30초"), ("60", "1분"), ("600", "10분"), ("off", "안 함")]
_REFRESH_LABELS = dict(_REFRESH_OPTIONS)
_REFRESH_ALLOWED = frozenset(_REFRESH_LABELS)
_REFRESH_DEFAULT = "60"  # 기본 1분
_refresh_var: contextvars.ContextVar = contextvars.ContextVar("dashboard_refresh", default=_REFRESH_DEFAULT)


def _normalize_refresh(value: Optional[str]) -> str:
    """허용 집합(10/30/60/600/off) 밖이면 기본값(1분)으로."""
    value = (value or "").strip()
    return value if value in _REFRESH_ALLOWED else _REFRESH_DEFAULT


def _refresh_meta_tag() -> str:
    r = _refresh_var.get()
    return "" if r == "off" else f'<meta http-equiv="refresh" content="{int(r)}">'


def _refresh_selector_html(option_url) -> str:
    """자동 새로고침 선택 컨트롤. `option_url(value)`가 각 옵션의 링크 URL을 만든다."""
    cur = _refresh_var.get()
    parts = [
        f'<a href="{html.escape(option_url(val))}" class="{"active" if val == cur else ""}">'
        f"{html.escape(label)}</a>"
        for val, label in _REFRESH_OPTIONS
    ]
    return (
        '<div class="refresh-ctl" style="margin-bottom:.75rem;font-size:.85rem">'
        "자동 새로고침: " + " · ".join(parts) + "</div>"
    )


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


def _authenticate(request: Request) -> Optional[str]:
    """요청을 인증해 사용자명을 돌려준다: (1) 유효한 세션 쿠키, (2) 저장값과 맞는 Basic."""
    store = auth.get_store()
    user = store.session_user(request.cookies.get(auth.COOKIE_NAME))
    if user:
        return user
    creds = _parse_basic(request.headers.get("Authorization", ""))
    if creds and store.verify(creds[0], creds[1]):
        return creds[0]
    return None


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    user = _authenticate(request)
    if user is None:
        if request.url.path.startswith("/api"):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{_AUTH_REALM}"'},
            )
        # 브라우저는 로그인 폼으로 보낸다(원래 가려던 경로를 next로 보존).
        prefix = _url_prefix()
        nxt = prefix + request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(f"{prefix}/login?{urlencode({'next': nxt})}", status_code=302)

    token = _current_user_var.set(user)
    try:
        return await call_next(request)
    finally:
        _current_user_var.reset(token)


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL_SECONDS,
        httponly=True, samesite="lax",
        secure=(request.url.scheme == "https"),
        path=_url_prefix() or "/",
    )


def _safe_next(next_url: Optional[str]) -> str:
    """오픈 리다이렉트 방지: 같은 사이트 상대경로(단일 '/' 시작)만 허용, 그 외엔 홈."""
    home = f"{_url_prefix()}/" or "/"
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return home
    return next_url

_SEVERITY_COLOR = {"none": "#4b5563", "warning": "#b45309", "critical": "#b91c1c"}
_PHASE_COLOR = {
    "Reconciled": "#15803d", "ok": "#15803d",
    "no_data": "#6b7280", "collection_failed": "#b45309",
    "Registered": "#2563eb", "Pending": "#6b7280", "Error": "#b91c1c",
}
_VERDICT_COLOR = {"FORWARDED": "#15803d", "DROPPED": "#b91c1c", "ERROR": "#b91c1c", "AUDIT": "#b45309"}

# 디자인은 seoul(k8s-cluster-tester) 대시보드의 기본 페이지와 통일한다: CSS 변수 기반 라이트/다크
# 테마, border-bottom 형태의 상단바(브랜드 + 탭), 가운데 정렬 main(max-width 1100px),
# findings-table 스타일 표. 색/간격 값은 seoul frontend/static/css/style.css 기준.
_STYLE = """
  :root {
    color-scheme: light dark;
    --bg:#ffffff; --fg:#1a1a1a; --muted:#6b7280; --border:#e2e2e2; --card-bg:#f7f7f8;
    --critical:#dc2626; --warning:#d97706; --info:#2563eb; --primary:#111827; --primary-fg:#ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#121212; --fg:#e5e5e5; --muted:#9ca3af; --border:#2e2e2e; --card-bg:#1c1c1e;
      --critical:#f87171; --warning:#fbbf24; --info:#60a5fa; --primary:#e5e5e5; --primary-fg:#121212;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: var(--bg); color: var(--fg); }
  a { color: var(--info); }
  /* 상단바: 브랜드(제목=홈 링크) + 탭 내비게이션. seoul .topbar와 동일한 border-bottom 형태. */
  .topbar { display: flex; align-items: center; gap: 24px; padding: 12px 20px;
            border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  .topbar h1 { font-size: 18px; margin: 0; }
  .home-link { font: inherit; color: var(--info); font-weight: 800; text-decoration: none; }
  .home-link:hover { text-decoration: underline; }
  .tabs { display: flex; gap: 4px; flex-wrap: wrap; }
  .tab { background: none; border: 1px solid transparent; color: var(--muted);
         padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 14px; text-decoration: none; }
  .tab:hover { color: var(--fg); }
  .tab.active { background: var(--card-bg); color: var(--fg); border-color: var(--border); }
  main { padding: 20px; max-width: 1100px; margin: 0 auto; }
  h2.page-title { font-size: 18px; margin: 0 0 4px; }
  a.active { color: inherit; font-weight: 600; text-decoration: underline; }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  .muted { color: var(--muted); font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; }
  .sub { color: var(--muted); font-size: .78rem; margin-top: .15rem; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; color: #fff;
            font-size: 11px; font-weight: 600; }
  .error-row td { color: var(--critical); }
  .empty { color: var(--muted); padding: 2rem 0; text-align: center; }
  code { background: var(--card-bg); padding: 1px 5px; border-radius: 4px; font-size: .85em; }
  .help { border: 1px solid var(--border); background: var(--card-bg); border-radius: 10px;
          padding: .75rem 1rem; margin-bottom: 1rem; font-size: .9rem; line-height: 1.5; }
  .help p { margin: .3rem 0; }
  .diagram { overflow-x: auto; border: 1px solid var(--border); background: var(--card-bg);
             border-radius: 10px; padding: .5rem .75rem; margin-bottom: .25rem; }
  .diagram-cap { color: var(--muted); font-size: .78rem; margin: 0 0 1.25rem; }
  /* 화살표를 '전기가 흐르듯' 표현: 두께는 균일, 이동하는 파선(dash)이 목적지 쪽으로 흐른다.
     흐름 속도(주기)는 연결 수에 반비례해 각 선의 인라인 animation-duration으로 지정한다
     (연결 잦을수록 빠름). 모션 최소화 선호 시 애니메이션을 끄고 실선으로 둔다(숫자는 그대로). */
  @keyframes flow-dash { to { stroke-dashoffset: -14; } }
  .flow-edge { stroke-dasharray: 6 8; animation-name: flow-dash;
               animation-timing-function: linear; animation-iteration-count: infinite; }
  @media (prefers-reduced-motion: reduce) {
    .flow-edge { animation: none; stroke-dasharray: none; }
  }
  /* 우측 로그인 상태 박스(사용자명·설정·로그아웃)와 로그인/설정 폼. */
  .userbox { margin-left: auto; display: flex; align-items: center; gap: 10px;
             font-size: 13px; color: var(--muted); }
  .userbox a { text-decoration: none; }
  .userbox a:hover { text-decoration: underline; }
  .linkbtn { background: none; border: none; color: var(--info); cursor: pointer;
             font: inherit; padding: 0; }
  .linkbtn:hover { text-decoration: underline; }
  .auth-card { max-width: 380px; margin: 8vh auto 0; border: 1px solid var(--border);
               background: var(--card-bg); border-radius: 12px; padding: 24px; }
  .auth-card h2 { margin: 0 0 4px; font-size: 18px; }
  .auth-card .muted { margin-bottom: 16px; }
  .field { margin-bottom: 12px; display: flex; flex-direction: column; gap: 4px; }
  .field label { font-size: 13px; color: var(--muted); }
  .field input { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px;
                 background: var(--bg); color: var(--fg); font-size: 14px; }
  .btn { background: var(--info); color: #fff; border: none; border-radius: 6px;
         padding: 9px 14px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .btn:hover { opacity: .9; }
  .form-msg { font-size: 13px; margin-bottom: 12px; }
  .form-msg.err { color: var(--critical); }
  .form-msg.ok { color: #15803d; }
"""

_PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{refresh_meta}
<style>{style}</style>
</head>
<body>
<header class="topbar">
  <h1><a href="{prefix}/" class="home-link">TrafficPolicy</a></h1>
  <nav class="tabs">
    <a href="{prefix}/" class="tab {nav_flows}">트래픽 흐름</a>
    <a href="{prefix}/policies" class="tab {nav_policies}">정책 현황</a>
  </nav>
  {userbox}
</header>
<main>
  <h2 class="page-title">{heading}</h2>
  <div class="meta">{meta}</div>
  {refresh}
  {table}
</main>
</body>
</html>"""


def _userbox_html() -> str:
    """상단바 우측: 로그인 사용자명 + 설정/로그아웃. 로그인 사용자를 모르면(=Basic 접근 등) 숨김."""
    user = current_user()
    if not user:
        return ""
    prefix = _url_prefix()
    return (
        '<div class="userbox">'
        f"👤 {html.escape(user)} · "
        f'<a href="{prefix}/settings">설정</a> · '
        f'<form method="post" action="{prefix}/logout" style="display:inline">'
        '<button type="submit" class="linkbtn">로그아웃</button></form>'
        "</div>"
    )


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


def _flows_url(*, scope: str, namespace: Optional[str] = None, focus: Optional[str] = None,
               refresh: Optional[str] = None) -> str:
    """트래픽 흐름(메인) 링크를 프리픽스 + 현재 필터(scope/namespace/focus)를 담은 URL로 만든다.

    트래픽 흐름이 메인 화면('/')이므로 루트 기준으로 URL을 만든다(레거시 '/flows'도 동일 내용).
    필터끼리 서로를 지우지 않고 조합되도록, 각 UI 요소가 유지할 값만 넘겨 호출한다
    (예: 네임스페이스 링크는 focus를 안 넘겨 초기화, 리소스 클릭은 namespace를 유지).
    값은 urlencode로 이스케이프되며, 라벨의 '/'(ns/pod)도 %2F로 안전하게 인코딩된다.
    """
    query = {"scope": scope}
    if namespace:
        query["namespace"] = namespace
    if focus:
        query["focus"] = focus
    # 현재(또는 지정한) 새로고침 값을 모든 흐름 링크에 실어, 필터를 눌러도 선택이 유지되게 한다.
    query["refresh"] = refresh or _refresh_var.get()
    return f"{_url_prefix()}/?{urlencode(query)}"


def _clean_param(value: Optional[str], max_len: int = 512) -> Optional[str]:
    """쿼리 파라미터를 다듬는다: 공백 제거, 빈 값은 None, 과도한 길이는 잘라 남용 방지."""
    if value is None:
        return None
    value = value.strip()[:max_len]
    return value or None


def _page(*, active: str, title: str, heading: str, meta: str, table: str,
          refresh_html: str = "") -> str:
    return _PAGE_TEMPLATE.format(
        title=title, style=_STYLE, heading=heading, meta=meta, table=table,
        prefix=_url_prefix(), userbox=_userbox_html(),
        refresh_meta=_refresh_meta_tag(), refresh=refresh_html,
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
    prefix = _url_prefix()
    refresh_html = _refresh_selector_html(
        lambda val: f"{prefix}/policies?{urlencode({'refresh': val})}")
    return _page(
        active="policies", title="TrafficPolicy Dashboard", heading="정책 현황",
        meta=f"트래픽 기반 자동운영 오퍼레이터 · 읽기 전용 · 자동 새로고침 {_REFRESH_LABELS[_refresh_var.get()]} · {len(policies)}개 정책",
        table=table, refresh_html=refresh_html,
    )


# --------------------------------------------------------------------------- Hubble 트래픽 흐름 (/flows)
def _flow_pair_row_html(pair: dict, *, scope: str, namespace: Optional[str]) -> str:
    port = f":{pair['dst_port']}" if pair.get("dst_port") else ""
    verdict = pair.get("verdict") or "-"
    v_color = _VERDICT_COLOR.get(verdict, "#4b5563")
    # Source/Destination을 클릭하면 그 리소스에 focus가 걸린다("연결된 리소스만 보기"). 현재
    # scope/namespace는 유지한다. focus는 endpoint 라벨(포트 없음)이므로 pair['src']/['dst']를 쓴다.
    src_url = _flows_url(scope=scope, namespace=namespace, focus=pair["src"])
    dst_url = _flows_url(scope=scope, namespace=namespace, focus=pair["dst"])
    return f"""
    <tr>
      <td><a href="{html.escape(src_url)}">{html.escape(pair['src'])}</a></td>
      <td>→</td>
      <td><a href="{html.escape(dst_url)}">{html.escape(pair['dst'])}</a>{html.escape(port)}</td>
      <td>{html.escape(pair['protocol'])}</td>
      <td><strong>{pair['count']}</strong></td>
      <td><span class="badge" style="background:{v_color}">{html.escape(verdict)}</span></td>
      <td class="sub">{html.escape(pair['last_seen'][:19].replace('T', ' '))}</td>
    </tr>"""


_VERDICT_LABEL_KO = {
    "FORWARDED": "정상(허용)",
    "DROPPED": "차단됨",
    "ERROR": "오류",
    "AUDIT": "감사(audit)",
}


def _flows_help_html() -> str:
    """이 페이지가 무엇을 표현하는지 설명하는 상시 표시 패널(자동 새로고침에도 유지되도록 div)."""
    legend = "".join(
        f'<span style="display:inline-block;white-space:nowrap;margin:.1rem .8rem .1rem 0">'
        f'<span class="badge" style="background:{_VERDICT_COLOR.get(v, "#4b5563")}">{v}</span> '
        f"{html.escape(lbl)}</span>"
        for v, lbl in _VERDICT_LABEL_KO.items()
    )
    return (
        '<div class="help">'
        "<p><strong>이 페이지는 무엇인가요?</strong> Cilium(CNI)의 <strong>Hubble</strong>이 관측한 "
        "<em>실제 Pod 간 네트워크 연결</em>입니다(L3/L4 수준). 아래 다이어그램에서 "
        "<strong>화살표</strong>는 연결 방향(source → destination)이며 <strong>전기가 흐르듯</strong> "
        "애니메이션됩니다 — 연결이 잦은 경로일수록 <strong>더 빠르게</strong> 흐릅니다(두께는 모두 동일). "
        "화살표 가운데 <strong>숫자</strong>가 실제 관측된 연결 수이고, <strong>색</strong>은 "
        "Cilium의 정책 <strong>판정(verdict)</strong>입니다.</p>"
        "<p>다이어그램은 <strong>멀티홉 흐름 그래프</strong>입니다 — 각 리소스를 한 번만 그리고 "
        "<strong>왼쪽에서 오른쪽으로 갈수록 다음 단계(hop)</strong>로 이어집니다. 그래서 "
        "<code>A→B</code>와 <code>B→C</code>가 같이 관측되면 <code>A→B→C</code> 사슬로 연결돼 "
        "보입니다(1:1 쌍은 인접한 두 열). 각 <strong>칩</strong>은 흐름에 참여한 리소스이며, "
        "윗줄은 <code>네임스페이스/Pod</code>, 아랫줄(<code>▸</code>)은 그 <strong>워크로드</strong>"
        "(Deployment/DaemonSet 등)입니다. 칩 테두리 색은 앱/인프라/예약 구분입니다.</p>"
        f'<p>{legend}</p>'
        '<p class="sub">· <strong>Count</strong>는 연결(flow) 수이며 HTTP 요청 수/RPS가 아닙니다. '
        "· 목적지 포트는 선 위 툴팁의 <code>:포트</code>로 표시됩니다. "
        "· <code>host</code>/<code>world</code>/<code>remote-node</code> 등은 개별 Pod이 아닌 "
        "예약 대상(클러스터 외부·노드 자신 등)입니다. "
        "· 이 화면은 <code>/</code>(정책 현황)와 <em>다른 데이터</em>입니다 — 그쪽은 오퍼레이터가 "
        "Gateway 트래픽 지표로 내린 <em>스케일링 판단</em>, 여기는 CNI가 본 <em>실제 연결</em>입니다.</p>"
        "</div>"
    )


def _shorten(label: str, n: int = 32) -> str:
    return label if len(label) <= n else label[: n - 1] + "…"


# 노드 성격별 칩 색(테두리/배경). 흐름에 참여하는 리소스가 앱인지 인프라/예약 개체인지 구분.
_KIND_STROKE = {"app": "#60a5fa", "infra": "#9ca3af", "reserved": "#9ca3af"}
_KIND_FILL = {"app": "rgba(96,165,250,.14)", "infra": "rgba(148,163,184,.10)", "reserved": "rgba(148,163,184,.06)"}
_KIND_LABEL = {"app": "앱", "infra": "인프라", "reserved": "예약"}


def _chip_width(label: str, sub: str) -> float:
    """칩 폭 추정(글자수 기반, SVG는 실측정이 없으므로). 앞뒤 여백 포함, 90~240px로 클램프."""
    chars = max(len(label), len(sub))
    return max(90.0, min(240.0, chars * 6.7 + 18))


def _flow_graph_svg(nodes: list, edges: list, limit: int = 10, *, scope: str = "app",
                    namespace: Optional[str] = None) -> str:
    """상위 연결을 계층형(멀티홉) 노드-링크 그래프로 그린다 — '다음 단계로 이어지는' 흐름 시각화.

    bipartite(좌=source, 우=destination)와 달리 각 리소스를 한 번만 그리고, 진입점에서의 홉
    수(layer)에 따라 왼→오 열로 배치한다. 그래서 A→B, B→C 가 있으면 A→B→C 사슬로 이어져
    보인다(1:1 쌍은 인접 두 열). 각 노드는 리소스 칩(라벨=ns/pod, 아래줄=워크로드/성격)이며
    클릭하면 그 리소스에 focus가 걸린다. 선 굵기 ∝ 연결 수, 색 = 대표 verdict.

    외부 라이브러리/JS 없이 서버 인라인 SVG로 생성(자동 새로고침·오프라인 동작). 가독성을 위해
    상위 `limit`개 간선만 그리며(전체는 아래 표), 그 간선에 닿는 노드만 그린다.
    """
    edges = list(edges[:limit])
    if not edges:
        return ""

    used_labels = {lbl for e in edges for lbl in (e["src"], e["dst"])}
    by_label = {n["label"]: n for n in nodes if n["label"] in used_labels}
    for lbl in used_labels:  # summary.nodes에 없던 라벨은 안전하게 합성(레이어 0, 메타 없음).
        by_label.setdefault(lbl, {"label": lbl, "namespace": None, "workload": None, "kind": "app", "layer": 0})

    # 열(layer)별로 노드 그룹핑 → 우선 라벨순으로 안정 정렬(이후 barycenter로 재정렬).
    columns: dict = {}
    for n in by_label.values():
        columns.setdefault(n["layer"], []).append(n)
    for col in columns.values():
        col.sort(key=lambda n: n["label"])
    max_layer = max(columns)

    def _sub(n: dict) -> str:
        if n.get("workload"):
            return "▸ " + n["workload"]
        if n["kind"] != "app":
            return _KIND_LABEL.get(n["kind"], "")
        return ""

    # --- 교차(선 겹침) 최소화: barycenter 휴리스틱으로 각 열의 노드 순서를 이웃 열에 맞춰 재정렬.
    # 각 노드를 인접 열에서 연결된 이웃들의 평균 위치로 옮기면 선이 서로 가로지르는 횟수가 준다.
    # 아래로(선행자 기준)·위로(후행자 기준) 스윕을 몇 차례 반복한다(작은 그래프라 비용은 무시할 수준).
    out_adj: dict = defaultdict(list)
    in_adj: dict = defaultdict(list)
    for e in edges:
        if e["src"] != e["dst"]:
            out_adj[e["src"]].append(e["dst"])
            in_adj[e["dst"]].append(e["src"])

    def _index() -> dict:
        return {n["label"]: i for L in columns for i, n in enumerate(columns[L])}

    def _bary(n: dict, adj: dict, idx: dict) -> float:
        nb = adj[n["label"]]
        return sum(idx[x] for x in nb) / len(nb) if nb else float(idx[n["label"]])

    for _ in range(4):
        for L in range(1, max_layer + 1):
            idx = _index()
            columns[L].sort(key=lambda n, idx=idx: _bary(n, in_adj, idx))
        for L in range(max_layer - 1, -1, -1):
            idx = _index()
            columns[L].sort(key=lambda n, idx=idx: _bary(n, out_adj, idx))

    # --- 좌표 배치. '넓게 표현해도 된다'는 요구에 맞춰 열 간격·행 간격을 넉넉히 준다.
    chip_h, row_h, pad_top, pad_x, col_gap = 36, 60, 50, 16, 150
    slot_w = {
        L: max(_chip_width(_shorten(n["label"], 26), _shorten(_sub(n), 26)) for n in columns[L])
        for L in columns
    }
    node_cw = {
        n["label"]: _chip_width(_shorten(n["label"], 26), _shorten(_sub(n), 26))
        for L in columns for n in columns[L]
    }
    col_x, acc = {}, float(pad_x)
    for L in range(max_layer + 1):
        col_x[L] = acc
        acc += slot_w.get(L, 90) + col_gap
    width = acc - col_gap + pad_x
    content_right = width   # self-loop 고리가 더 오른쪽으로 나가면 아래 루프에서 확장

    rows = max((len(c) for c in columns.values()), default=1)
    node_y = {}
    for L in range(max_layer + 1):
        cnt = len(columns.get(L, []))
        offset = (rows - cnt) * row_h / 2.0   # 짧은 열은 세로 가운데 정렬 → 가파른 대각선/겹침 감소
        for i, n in enumerate(columns[L]):
            node_y[n["label"]] = pad_top + offset + i * row_h
    content_bottom = pad_top + rows * row_h   # 노드 영역 하단(역방향 우회선 bow가 더 내려가면 아래서 확장)

    # --- 포트 분산: 한 노드에 여러 간선이 붙을 때 연결 지점을 칩 높이에 고루 나눠, 화살표가
    # 한 점에 몰려 겹치는 것을 막는다. 각 노드의 나가는/들어오는 간선을 상대편 y로 정렬해
    # 위→아래 순서대로 포트를 배정하면 선끼리 교차도 줄어든다.
    out_e: dict = defaultdict(list)
    in_e: dict = defaultdict(list)
    for i, e in enumerate(edges):
        out_e[e["src"]].append(i)
        in_e[e["dst"]].append(i)
    for lst in out_e.values():
        lst.sort(key=lambda i: node_y[edges[i]["dst"]])
    for lst in in_e.values():
        lst.sort(key=lambda i: node_y[edges[i]["src"]])
    src_py, dst_py = {}, {}
    for lbl, lst in out_e.items():
        k = len(lst)
        for j, i in enumerate(lst):
            src_py[i] = node_y[lbl] + chip_h * (j + 1) / (k + 1)
    for lbl, lst in in_e.items():
        k = len(lst)
        for j, i in enumerate(lst):
            dst_py[i] = node_y[lbl] + chip_h * (j + 1) / (k + 1)

    max_count = max((e["count"] for e in edges), default=1) or 1
    min_count = min((e["count"] for e in edges), default=1)
    span = (max_count - min_count) or 1
    used_verdicts = {e.get("verdict") or "FORWARDED" for e in edges}
    markers = "".join(
        f'<marker id="arw-{html.escape(v)}" markerWidth="8" markerHeight="8" refX="6.5" refY="3" '
        f'orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="{_VERDICT_COLOR.get(v, "#4b5563")}"/></marker>'
        for v in used_verdicts
    )

    EDGE_W = 2.6                    # 모든 화살표 두께 균일(연결 수와 무관)
    DUR_FAST, DUR_SLOW = 0.5, 3.0   # 흐름 애니메이션 주기(초): 연결 많음=빠름(짧은 주기)

    def _cubic_mid(p0, c1, c2, p3):  # 3차 베지어 t=0.5 지점(연결 수 라벨 위치)
        return (0.125 * p0[0] + 0.375 * c1[0] + 0.375 * c2[0] + 0.125 * p3[0],
                0.125 * p0[1] + 0.375 * c1[1] + 0.375 * c2[1] + 0.125 * p3[1])

    edge_svg = []
    for i, e in enumerate(edges):
        v = e.get("verdict") or "FORWARDED"
        color = _VERDICT_COLOR.get(v, "#4b5563")
        s_layer = by_label[e["src"]]["layer"]
        d_layer = by_label[e["dst"]]["layer"]
        port = f":{e['dst_port']}" if e.get("dst_port") else ""
        tip = f'{e["src"]} → {e["dst"]}{port} · {e["protocol"]} · {e["count"]}건 · {v}'
        if e["src"] == e["dst"]:
            # 자기 자신으로의 흐름: 칩 오른쪽에 작은 고리로 그린다(드묾).
            x = col_x[s_layer] + node_cw[e["src"]]
            y = src_py[i]
            content_right = max(content_right, x + 44)  # 고리가 오른쪽으로 잘리지 않게 폭 확장
            p0, c1, c2, p3 = (x, y), (x + 44, y - 24), (x + 44, y + 24), (x, y + 3)
        else:
            sx = col_x[s_layer] + node_cw[e["src"]]
            sy = src_py[i]
            ex = col_x[d_layer] - 9
            ey = dst_py[i]
            if ex > sx:  # 정방향(왼→오): 수평 중간점을 제어점으로 하는 S커브.
                cx = (sx + ex) / 2
                p0, c1, c2, p3 = (sx, sy), (cx, sy), (cx, ey), (ex, ey)
            else:  # 역방향(순환): 아래로 우회(제어점 x는 두 노드 사이로 유지 → 좌우로 안 삐져나감).
                bow = max(sy, ey) + row_h * 0.9
                content_bottom = max(content_bottom, bow)  # 우회선이 뷰박스 밖으로 잘리지 않게 높이 확장
                p0, c1, c2, p3 = (sx, sy), (sx, bow), (ex, bow), (ex, ey)
        d = (f"M{p0[0]:.0f},{p0[1]:.0f} C{c1[0]:.0f},{c1[1]:.0f} "
             f"{c2[0]:.0f},{c2[1]:.0f} {p3[0]:.0f},{p3[1]:.0f}")
        # 연결 많을수록 흐름 주기를 짧게(=빠르게). 상대 빈도(min~max) 기준. d를 attr 맨 앞에 둔다.
        ratio = (e["count"] - min_count) / span
        dur = DUR_SLOW - ratio * (DUR_SLOW - DUR_FAST)
        edge_svg.append(
            f'<path d="{d}" class="flow-edge" fill="none" stroke="{color}" stroke-width="{EDGE_W}" '
            f'stroke-opacity="0.85" stroke-linecap="round" style="animation-duration:{dur:.2f}s" '
            f'marker-end="url(#arw-{html.escape(v)})"><title>{html.escape(tip)}</title></path>'
        )
        # 실제 연결 수를 화살표 가운데에 표기(배경색 halo로 선 위에서도 읽히게). 정적(애니메이션 없음).
        mx, my = _cubic_mid(p0, c1, c2, p3)
        edge_svg.append(
            f'<text x="{mx:.0f}" y="{my - 3:.0f}" text-anchor="middle" font-size="10" '
            f'font-weight="700" fill="{color}" '
            f'style="paint-order:stroke;stroke:var(--card-bg);stroke-width:3px">{e["count"]}</text>'
        )

    node_svg = []
    for lbl, n in by_label.items():
        x = col_x[n["layer"]]
        y = node_y[lbl]
        cw = node_cw[lbl]
        stroke = _KIND_STROKE.get(n["kind"], "#9ca3af")
        fill = _KIND_FILL.get(n["kind"], "none")
        dash = ' stroke-dasharray="3 2"' if n["kind"] == "reserved" else ""
        sub = _shorten(_sub(n), 26)
        label_disp = _shorten(lbl, 26)
        url = _flows_url(scope=scope, namespace=namespace, focus=lbl)
        sub_svg = (
            f'<text x="{x + 10:.0f}" y="{y + 27:.0f}" font-size="9" fill="currentColor" '
            f'opacity="0.6">{html.escape(sub)}</text>' if sub else ""
        )
        node_svg.append(
            f'<a href="{html.escape(url)}"><title>{html.escape(lbl)} 연결만 보기</title>'
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{cw:.0f}" height="{chip_h}" rx="7" '
            f'fill="{fill}" stroke="{stroke}"{dash} stroke-width="1"/>'
            f'<text x="{x + 10:.0f}" y="{y + 16:.0f}" font-size="11.5" fill="currentColor">'
            f'{html.escape(label_disp)}</text>{sub_svg}</a>'
        )

    height = content_bottom + 14
    width = content_right + 8

    caption = (
        f'<text x="{pad_x}" y="30" font-size="11" fill="currentColor" opacity="0.6">'
        f'→ 오른쪽으로 갈수록 다음 단계(hop) · 칩=리소스, 아래줄=워크로드</text>'
    )
    return (
        f'<div class="diagram"><svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
        f'style="max-width:{width:.0f}px;height:auto;display:block" role="img" '
        f'aria-label="멀티홉 Pod 트래픽 흐름 그래프">'
        f"<defs>{markers}</defs>{caption}{''.join(edge_svg)}{''.join(node_svg)}</svg></div>"
    )


def _scope_toggle_html(summary: FlowSummary) -> str:
    """애플리케이션 전용/전체 트래픽 전환 링크. 현재 namespace/focus 필터는 유지한다."""
    app_cls = "active" if summary.scope == "app" else ""
    all_cls = "active" if summary.scope == "all" else ""
    app_url = _flows_url(scope="app", namespace=summary.namespace, focus=summary.focus)
    all_url = _flows_url(scope="all", namespace=summary.namespace, focus=summary.focus)
    return (
        '<div style="margin-bottom:.5rem;font-size:.85rem">보기: '
        f'<a href="{html.escape(app_url)}" class="{app_cls}">내 애플리케이션 트래픽</a> · '
        f'<a href="{html.escape(all_url)}" class="{all_cls}">전체(인프라 포함)</a></div>'
    )


def _namespace_filter_html(summary: FlowSummary) -> str:
    """현재 scope에 등장하는 네임스페이스 선택 링크. '전체'는 ns 해제, 개별 ns는 focus 초기화.

    scope만 적용한 목록(summary.namespaces)을 쓰므로, 어떤 필터가 걸려 있어도 다른
    네임스페이스로 곧장 전환할 수 있다. 다른 ns로 옮기면 이전 리소스 focus는 대개 무의미하므로
    개별 ns 링크는 focus를 넘기지 않아(초기화) 그 네임스페이스 전체를 보여준다.
    """
    if not summary.namespaces:
        return ""
    all_cls = "active" if not summary.namespace else ""
    # '전체'(ns 해제)는 focus는 유지 — "이 리소스가 낀 흐름을 모든 ns에 걸쳐" 보고 싶을 수 있으므로.
    parts = [
        f'<a href="{html.escape(_flows_url(scope=summary.scope, focus=summary.focus))}" '
        f'class="{all_cls}">전체</a>'
    ]
    for ns in summary.namespaces:
        name = ns["name"]
        cls = "active" if name == summary.namespace else ""
        url = _flows_url(scope=summary.scope, namespace=name)
        parts.append(
            f'<a href="{html.escape(url)}" class="{cls}">{html.escape(name)}</a>'
            f'<span class="sub">({ns["count"]})</span>'
        )
    return (
        '<div style="margin-bottom:.5rem;font-size:.85rem">네임스페이스: '
        + " · ".join(parts) + "</div>"
    )


def _focus_banner_html(summary: FlowSummary) -> str:
    """focus(연결된 리소스) 상태 표시 + 해제 링크. focus가 없으면 사용법 힌트를 보여준다."""
    if not summary.focus:
        return (
            '<div class="sub" style="margin-bottom:1rem">💡 아래 표·다이어그램에서 '
            "Source·Destination을 클릭하면 그 리소스와 연결된 흐름만 볼 수 있습니다.</div>"
        )
    clear_url = _flows_url(scope=summary.scope, namespace=summary.namespace)
    return (
        '<div class="help" style="margin-bottom:1rem">'
        f'선택한 리소스 <code>{html.escape(summary.focus)}</code> 와(과) 연결된 흐름만 표시 중 · '
        f'<a href="{html.escape(clear_url)}">× 선택 해제</a></div>'
    )


def _render_flows(summary: FlowSummary) -> str:
    # 필터 컨트롤(스코프/네임스페이스/포커스)은 조회 실패·빈 상태에서도 항상 보이게 상단에 둔다.
    controls = (
        _scope_toggle_html(summary)
        + _namespace_filter_html(summary)
        + _focus_banner_html(summary)
    )
    help_panel = _flows_help_html()
    scope_label = "내 애플리케이션 Pod" if summary.scope == "app" else "전체(인프라 포함)"
    filter_label = "".join(
        s for s in (
            f" · 네임스페이스 {summary.namespace}" if summary.namespace else "",
            f" · 리소스 {summary.focus}" if summary.focus else "",
        )
    )

    if summary.fetch_error:
        table = controls + help_panel + (
            f'<div class="error-row" style="padding:1rem">⚠ Hubble 조회 실패: '
            f'{html.escape(summary.fetch_error)}</div>'
        )
        meta = "Cilium Hubble 기반 실제 Pod 트래픽 흐름 · 조회 실패"
    elif summary.shown == 0:
        # 왜 0건인지 상황별로 다르게 안내한다: (1) 필터(ns/focus) 때문 (2) 앱 흐름만 0 (3) 아예 없음.
        if summary.namespace or summary.focus:
            empty = (
                '<div class="empty">이 필터에 해당하는 흐름이 최근 창에 없습니다. '
                "위에서 필터를 바꾸거나 해제해 보세요.</div>"
            )
        elif summary.scope == "app" and summary.total > 0:
            empty = (
                '<div class="empty">최근 창에서 애플리케이션 Pod 흐름이 관측되지 않았습니다. '
                '전체 흐름은 있으니 위 "전체(인프라 포함)"로 확인하세요.</div>'
            )
        else:
            empty = '<div class="empty">관측된 흐름이 없습니다.</div>'
        table = controls + help_panel + empty
        meta = f"Cilium Hubble 기반 · {scope_label}{filter_label} 0건 (전체 {summary.total}건)"
    else:
        badges = " ".join(
            f'<span class="badge" style="background:{_VERDICT_COLOR.get(v, "#4b5563")}">{html.escape(v)} {c}</span>'
            for v, c in summary.verdicts.items()
        )
        diagram = _flow_graph_svg(summary.nodes, summary.top_pairs,
                                  scope=summary.scope, namespace=summary.namespace)
        shown_in_diagram = min(10, len(summary.top_pairs))
        diagram_cap = (
            f'<p class="diagram-cap">↑ 상위 {shown_in_diagram}개 연결을 멀티홉 그래프로 도식화 '
            f"(전체 {len(summary.top_pairs)}개는 아래 표 참고) · 왼→오 = 다음 단계(hop) · "
            "화살표는 전기 흐르듯 애니메이션(연결 잦을수록 빠름), 가운데 숫자 = 실제 연결 수 · "
            "선 위 마우스 = 상세, 칩 클릭 = 그 리소스 연결만 보기.</p>"
        )
        rows = "\n".join(
            _flow_pair_row_html(p, scope=summary.scope, namespace=summary.namespace)
            for p in summary.top_pairs
        )
        table = controls + help_panel + diagram + diagram_cap + f"""<div style="margin-bottom:1rem">{badges}</div>
<table>
<thead><tr>
  <th>Source</th><th></th><th>Destination</th><th>Proto</th><th>Count</th><th>Verdict</th><th>Last Seen (UTC)</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
        meta = (
            f"Cilium Hubble 기반 실제 Pod 트래픽 흐름(L3/L4, HTTP RPS 아님) · {scope_label}{filter_label} · "
            f"최근 전체 {summary.total}건 중 애플리케이션 {summary.app_flows}건, "
            f"현재 보기 {summary.shown}건에서 상위 {len(summary.top_pairs)}개 연결 쌍"
        )
    refresh_html = _refresh_selector_html(lambda val: _flows_url(
        scope=summary.scope, namespace=summary.namespace, focus=summary.focus, refresh=val))
    return _page(
        active="flows", title="Pod Traffic Flows (Hubble)", heading="실시간 Pod 트래픽 흐름",
        meta=meta, table=table, refresh_html=refresh_html,
    )


def _normalize_scope(scope: str) -> str:
    return "all" if scope == "all" else "app"


def _flows_response(scope: str, namespace: Optional[str], focus: Optional[str]) -> str:
    return _render_flows(hubble_flows.fetch_summary(
        scope=_normalize_scope(scope),
        namespace=_clean_param(namespace),
        focus=_clean_param(focus),
    ))


@app.get("/", response_class=HTMLResponse)
def dashboard(scope: str = "app", namespace: Optional[str] = None, focus: Optional[str] = None,
              refresh: str = _REFRESH_DEFAULT) -> str:
    """메인 화면 = 실시간 트래픽 흐름(Hubble)."""
    _refresh_var.set(_normalize_refresh(refresh))
    return _flows_response(scope, namespace, focus)


@app.get("/flows", response_class=HTMLResponse)
def flows(scope: str = "app", namespace: Optional[str] = None, focus: Optional[str] = None,
          refresh: str = _REFRESH_DEFAULT) -> str:
    """레거시 별칭 — 예전 '/flows' 북마크/링크 호환용. 메인('/')과 동일한 내용."""
    _refresh_var.set(_normalize_refresh(refresh))
    return _flows_response(scope, namespace, focus)


@app.get("/policies", response_class=HTMLResponse)
def policies_page(refresh: str = _REFRESH_DEFAULT) -> str:
    """TrafficPolicy 정책 현황(오퍼레이터 판단 결과). 메뉴바의 '정책 현황'."""
    _refresh_var.set(_normalize_refresh(refresh))
    return _render_policies(data.fetch_policies())


@app.get("/api/policies", response_class=JSONResponse)
def api_policies():
    policies = data.fetch_policies()
    return {
        "generatedAt": time.time(),
        "count": len(policies),
        "policies": [p.__dict__ for p in policies],
    }


@app.get("/api/flows", response_class=JSONResponse)
def api_flows(scope: str = "app", namespace: Optional[str] = None, focus: Optional[str] = None):
    summary = hubble_flows.fetch_summary(
        scope=_normalize_scope(scope),
        namespace=_clean_param(namespace),
        focus=_clean_param(focus),
    )
    return {
        "generatedAt": time.time(),
        "total": summary.total,
        "shown": summary.shown,
        "scope": summary.scope,
        "namespace": summary.namespace,
        "focus": summary.focus,
        "appFlows": summary.app_flows,
        "namespaces": summary.namespaces,
        "verdicts": summary.verdicts,
        "topPairs": summary.top_pairs,
        "nodes": summary.nodes,
        "fetchError": summary.fetch_error,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# --------------------------------------------------------------------------- 로그인/설정 화면
# 자동 새로고침(meta refresh)이 없는 별도 셸 — 폼 입력 중 새로고침으로 값이 날아가지 않게 한다.
_AUTH_PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<header class="topbar">
  <h1><a href="{prefix}/" class="home-link">TrafficPolicy</a></h1>
  {nav}
  {userbox}
</header>
<main>{body}</main>
</body>
</html>"""


def _auth_shell(*, title: str, body: str, with_nav: bool) -> str:
    prefix = _url_prefix()
    nav = (
        '<nav class="tabs">'
        f'<a href="{prefix}/" class="tab">트래픽 흐름</a>'
        f'<a href="{prefix}/policies" class="tab">정책 현황</a></nav>'
    ) if with_nav else ""
    return _AUTH_PAGE_TEMPLATE.format(
        title=title, style=_STYLE, prefix=prefix, body=body,
        nav=nav, userbox=_userbox_html(),
    )


def _form_msg(*, error: Optional[str] = None, message: Optional[str] = None) -> str:
    if error:
        return f'<div class="form-msg err">{html.escape(error)}</div>'
    if message:
        return f'<div class="form-msg ok">{html.escape(message)}</div>'
    return ""


def _login_page(*, next_url: str = "", error: Optional[str] = None) -> str:
    prefix = _url_prefix()
    body = f"""<div class="auth-card">
  <h2>로그인</h2>
  <div class="muted">TrafficPolicy 운영 대시보드</div>
  {_form_msg(error=error)}
  <form method="post" action="{prefix}/login">
    <input type="hidden" name="next" value="{html.escape(next_url)}">
    <div class="field"><label for="username">아이디</label>
      <input id="username" name="username" autocomplete="username" autofocus></div>
    <div class="field"><label for="password">비밀번호</label>
      <input id="password" name="password" type="password" autocomplete="current-password"></div>
    <button class="btn" type="submit">로그인</button>
  </form>
</div>"""
    return _auth_shell(title="로그인 · TrafficPolicy", body=body, with_nav=False)


def _settings_page(*, username: str, error: Optional[str] = None,
                   message: Optional[str] = None) -> str:
    prefix = _url_prefix()
    if auth.get_store().persistent:
        note = "변경 내용은 저장 볼륨에 반영되어 재기동에도 유지됩니다."
    else:
        note = ("⚠ 저장 볼륨이 없어 변경은 이 인스턴스에만 적용되며, 재기동되면 "
                "기본값(admin)으로 되돌아갑니다.")
    body = f"""<div class="auth-card">
  <h2>설정 — 로그인 자격증명 변경</h2>
  <div class="muted">현재 로그인: {html.escape(username)}</div>
  {_form_msg(error=error, message=message)}
  <form method="post" action="{prefix}/settings">
    <div class="field"><label for="cur">현재 비밀번호</label>
      <input id="cur" name="current_password" type="password" autocomplete="current-password"></div>
    <div class="field"><label for="nu">새 아이디</label>
      <input id="nu" name="new_username" value="{html.escape(username)}" autocomplete="username"></div>
    <div class="field"><label for="np">새 비밀번호(변경 시에만 입력)</label>
      <input id="np" name="new_password" type="password" autocomplete="new-password"></div>
    <div class="field"><label for="cp">새 비밀번호 확인</label>
      <input id="cp" name="confirm_password" type="password" autocomplete="new-password"></div>
    <button class="btn" type="submit">변경</button>
  </form>
  <p class="sub" style="margin-top:12px">{html.escape(note)}</p>
</div>"""
    return _auth_shell(title="설정 · TrafficPolicy", body=body, with_nav=True)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = ""):
    # 이미 로그인 상태면 곧장 목적지로.
    if auth.get_store().session_user(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse(_safe_next(next), status_code=302)
    return HTMLResponse(_login_page(next_url=next))


@app.post("/login")
def login_submit(request: Request, username: str = Form(""),
                 password: str = Form(""), next: str = Form("")):
    store = auth.get_store()
    if not store.verify(username, password):
        return HTMLResponse(
            _login_page(next_url=next, error="아이디 또는 비밀번호가 올바르지 않습니다."),
            status_code=401,
        )
    resp = RedirectResponse(_safe_next(next), status_code=302)
    _set_session_cookie(resp, request, store.issue_session(username))
    return resp


@app.post("/logout")
def logout(request: Request):
    resp = RedirectResponse(f"{_url_prefix()}/login", status_code=302)
    resp.delete_cookie(auth.COOKIE_NAME, path=_url_prefix() or "/")
    return resp


@app.get("/settings", response_class=HTMLResponse)
def settings_form():
    # 인증은 미들웨어가 보장. current_user()가 비면(Basic 접근) 저장된 사용자명을 쓴다.
    return HTMLResponse(_settings_page(username=current_user() or auth.get_store().username))


@app.post("/settings")
def settings_submit(request: Request, current_password: str = Form(""),
                    new_username: str = Form(""), new_password: str = Form(""),
                    confirm_password: str = Form("")):
    store = auth.get_store()
    user = current_user() or store.username
    if not store.verify(user, current_password):
        return HTMLResponse(
            _settings_page(username=user, error="현재 비밀번호가 올바르지 않습니다."),
            status_code=400,
        )
    new_username = new_username.strip()
    if new_password or confirm_password:
        if new_password != confirm_password:
            return HTMLResponse(
                _settings_page(username=user, error="새 비밀번호가 일치하지 않습니다."),
                status_code=400,
            )
        if len(new_password) < auth.MIN_PASSWORD_LEN:
            return HTMLResponse(
                _settings_page(username=user,
                               error=f"새 비밀번호는 최소 {auth.MIN_PASSWORD_LEN}자 이상이어야 합니다."),
                status_code=400,
            )
    username_changed = bool(new_username) and new_username != user
    if not username_changed and not new_password:
        return HTMLResponse(
            _settings_page(username=user, error="변경할 내용이 없습니다."),
            status_code=400,
        )
    store.update_credentials(new_username=new_username or None, new_password=new_password or None)
    final_user = new_username or user
    # 세션 키가 회전됐으므로 현재 사용자에게 새 쿠키를 재발급(다른 기기 세션은 무효화).
    resp = HTMLResponse(_settings_page(
        username=final_user, message="변경되었습니다. 다른 기기의 세션은 로그아웃됩니다."))
    _set_session_cookie(resp, request, store.issue_session(final_user))
    return resp
