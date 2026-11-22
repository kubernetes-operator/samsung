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
from urllib.parse import urlencode, urlsplit, unquote

from fastapi import FastAPI, Form, Request, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth, data, hubble_flows, page_policy
from .accounts import AccountError
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


def _seg_control_html(label: str, options) -> str:
    """라벨 + 선택 버튼(pill) 그룹. options = [(inner_html, url, active), ...].

    본문 필터(보기·네임스페이스)를 텍스트 링크 대신 선택 버튼 형태로 렌더한다. `inner_html`은
    이미 안전하게 구성된 조각(호출부에서 이스케이프)이며, `url`은 이스케이프해서 넣는다.
    """
    cells = []
    for inner, url, active in options:
        cls = "seg active" if active else "seg"
        cur_attr = ' aria-current="true"' if active else ""
        cells.append(f'<a href="{html.escape(url)}" class="{cls}"{cur_attr}>{inner}</a>')
    return (
        '<div class="segctl">'
        f'<span class="segctl-label">{html.escape(label)}</span>'
        '<span class="seg-cells">' + "".join(cells) + "</span></div>"
    )


def _refresh_selector_html(option_url) -> str:
    """자동 새로고침 컨트롤(상단바): **드롭다운(스크롤 선택)** + **수동 새로고침 버튼(↻)**.

    `option_url(value)`가 각 옵션(주기)에 대한 이동 URL을 만든다. 드롭다운은 onchange로 해당
    URL로 이동하고, ↻ 버튼은 현재 페이지를 즉시 다시 불러온다(location.reload()).
    """
    cur = _refresh_var.get()
    opts = "".join(
        f'<option value="{html.escape(option_url(val))}"{" selected" if val == cur else ""}>'
        f"{html.escape(label)}</option>"
        for val, label in _REFRESH_OPTIONS
    )
    return (
        '<div class="refresh-ctl">'
        '<label class="refresh-ctl-label" for="refresh-select">자동 새로고침</label>'
        '<select id="refresh-select" class="refresh-select" aria-label="자동 새로고침 주기" '
        'onchange="if(this.value)location.href=this.value">' + opts + "</select>"
        '<button type="button" class="refresh-now" title="지금 새로고침" '
        'aria-label="지금 새로고침" onclick="location.reload()">↻</button>'
        "</div>"
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
        if request.url.path.startswith('/api'):
            return Response(status_code=401,headers={'WWW-Authenticate':f'Basic realm="{_AUTH_REALM}"'})
        prefix=_url_prefix();nxt=prefix+request.url.path
        if request.url.query:nxt+='?'+request.url.query
        return RedirectResponse(f"{prefix}/login?{urlencode({'next':nxt})}",status_code=302)
    try:access=auth.get_store().access(user)
    except AccountError:return JSONResponse({'detail':'다시 로그인해 주세요'},status_code=401)
    page=_web_page(request.url.path)
    if access['role']!='admin' and (page is None or not page_policy.allowed('samsung',access['role'],access['pages'],page)):
        return JSONResponse({'detail':'이 페이지에 접근할 권한이 없습니다'},status_code=403)
    if request.url.path.startswith('/api/auth/') and request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        if origin and urlsplit(origin).netloc!=request.url.netloc:return JSONResponse({'detail':'요청 출처가 올바르지 않습니다'},status_code=403)
    token=_current_user_var.set(user)
    try:
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        return response
    finally:_current_user_var.reset(token)


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
  /* 상단바 우측 그룹: 자동 새로고침(드롭다운 + 수동 새로고침 버튼) + 로그인 박스. */
  .topbar-right { margin-left: auto; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  .refresh-ctl { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .refresh-ctl-label { color: var(--muted); white-space: nowrap; }
  .refresh-select { font: inherit; font-size: 12px; padding: 4px 6px; border: 1px solid var(--border);
                    border-radius: 6px; background: var(--bg); color: var(--fg); cursor: pointer; }
  .refresh-now { font-size: 13px; line-height: 1; padding: 4px 9px; border: 1px solid var(--border);
                 border-radius: 6px; background: var(--card-bg); color: var(--fg); cursor: pointer; }
  .refresh-now:hover { background: var(--info); color: #fff; border-color: var(--info); }
  /* 본문 필터(보기·네임스페이스) = 선택 버튼(pill). 많은 항목은 줄바꿈된다. 현재 선택 강조. */
  .segctl { display: flex; align-items: center; gap: 8px; margin-bottom: .6rem; font-size: 13px; flex-wrap: wrap; }
  .segctl-label { color: var(--muted); font-weight: 600; white-space: nowrap; }
  .seg-cells { display: flex; flex-wrap: wrap; gap: 4px; }
  .seg { padding: 3px 11px; border: 1px solid var(--border); border-radius: 999px; color: var(--muted);
         text-decoration: none; white-space: nowrap; background: var(--card-bg); }
  .seg:hover { color: var(--fg); }
  .seg.active { background: var(--info); color: #fff; border-color: var(--info); font-weight: 600; }
  .segcount { opacity: .7; font-size: .85em; }
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

# BEGIN WEB STANDARD GENERATED UI
_STYLE += """
/* Web Standard v0.4.0 — generated by scripts/build.py */
/* 공통 크기: 앱의 업무 레이아웃과 편집기 치수는 변경하지 않는다. */
:root, .ws-theme-light, .ws-theme-dark {
  --ws-font: system-ui, -apple-system, 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif;
  --ws-font-mono: ui-monospace, 'SFMono-Regular', Consolas, 'D2Coding', monospace;
  --ws-text-sm: 12px;
  --ws-text-md: 14px;
  --ws-text-lg: 18px;
  --ws-space-1: 4px;
  --ws-space-2: 8px;
  --ws-space-3: 16px;
  --ws-space-4: 24px;
  --ws-radius: 6px;
  --ws-radius-lg: 10px;
  --ws-content-width: 1100px;
}
:root, .ws-theme-light {
  color-scheme: light;
  --ws-bg: #ffffff;
  --ws-panel: #f7f7f8;
  --ws-text: #1a1a1a;
  --ws-muted: #626b79;
  --ws-border: #d4d8de;
  --ws-accent: #2563eb;
  --ws-accent-hover: #1d4ed8;
  --ws-on-accent: #ffffff;
  --ws-success: #15803d;
  --ws-success-bg: #eefbf2;
  --ws-warning: #925200;
  --ws-warning-bg: #fff7e0;
  --ws-danger: #b91c1c;
  --ws-danger-bg: #fff0f0;
  --ws-danger-border: #edb4b4;
  --ws-info-bg: #eff6ff;
  --ws-hover: #eceff3;
  --ws-selected: #dbeafe;
  --ws-overlay: rgba(15, 23, 42, .5);
  --ws-shadow: 0 2px 10px rgb(15 23 42 / .08);
  --ws-link: #1d4ed8;
  --ws-log-bg: #171d26;
  --ws-log-text: #d7e0ea;
}
@media (prefers-color-scheme: dark) {
  :root:not(.ws-theme-light):not(.ws-theme-dark) {
  color-scheme: dark;
  --ws-bg: #121212;
  --ws-panel: #1c1c1e;
  --ws-text: #e5e5e5;
  --ws-muted: #a0a9b7;
  --ws-border: #454b55;
  --ws-accent: #60a5fa;
  --ws-accent-hover: #93c5fd;
  --ws-on-accent: #101827;
  --ws-success: #4ade80;
  --ws-success-bg: #142c20;
  --ws-warning: #fbbf24;
  --ws-warning-bg: #332a12;
  --ws-danger: #f87171;
  --ws-danger-bg: #351b20;
  --ws-danger-border: #87424c;
  --ws-info-bg: #182b42;
  --ws-hover: #292d33;
  --ws-selected: #203b60;
  --ws-overlay: rgba(0, 0, 0, .65);
  --ws-shadow: 0 2px 10px rgb(0 0 0 / .25);
  --ws-link: #93c5fd;
  --ws-log-bg: #171d26;
  --ws-log-text: #d7e0ea;
  }
}
.ws-theme-dark {
  color-scheme: dark;
  --ws-bg: #121212;
  --ws-panel: #1c1c1e;
  --ws-text: #e5e5e5;
  --ws-muted: #a0a9b7;
  --ws-border: #454b55;
  --ws-accent: #60a5fa;
  --ws-accent-hover: #93c5fd;
  --ws-on-accent: #101827;
  --ws-success: #4ade80;
  --ws-success-bg: #142c20;
  --ws-warning: #fbbf24;
  --ws-warning-bg: #332a12;
  --ws-danger: #f87171;
  --ws-danger-bg: #351b20;
  --ws-danger-border: #87424c;
  --ws-info-bg: #182b42;
  --ws-hover: #292d33;
  --ws-selected: #203b60;
  --ws-overlay: rgba(0, 0, 0, .65);
  --ws-shadow: 0 2px 10px rgb(0 0 0 / .25);
  --ws-link: #93c5fd;
  --ws-log-bg: #171d26;
  --ws-log-text: #d7e0ea;
}
/* 명시적으로 참여한 화면만 변경한다. display/hidden/높이/overflow는 보존한다. */
html.ws-app body, .ws-app {
  font-family: var(--ws-font);
  color: var(--ws-text);
  background-color: var(--ws-bg);
}
.ws-app, .ws-app *, .ws-app *::before, .ws-app *::after { box-sizing: border-box; }
.ws-app :where(button, input, select, textarea) { font-family: var(--ws-font); }
.ws-app :is(button, input, select, textarea, a, summary, [tabindex]):focus-visible {
  outline: 2px solid var(--ws-accent);
  outline-offset: 2px;
}
.ws-app :where(button:disabled, input:disabled, select:disabled, textarea:disabled) { cursor: not-allowed; }
.ws-app :where(input, textarea)::placeholder { color: var(--ws-muted); opacity: 1; }
.ws-app :where(a) { color: var(--ws-link); }
.ws-app :where(code, pre, .mono) { font-family: var(--ws-font-mono); }
@media (prefers-reduced-motion: reduce) {
  .ws-app *, .ws-app *::before, .ws-app *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
}

.ws-app :where(button, select, input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]), textarea) { color: var(--ws-text); background-color: var(--ws-bg); border-color: var(--ws-border); }
/* 공통 UI: 기존 앱에는 프로젝트 Adapter로 연결한다. */
.ws-app .ws-card { padding: var(--ws-space-3); border: 1px solid var(--ws-border); border-radius: var(--ws-radius-lg); background: var(--ws-panel); }
.ws-app .ws-button { padding: var(--ws-space-2) var(--ws-space-3); border: 1px solid var(--ws-border); border-radius: var(--ws-radius); color: var(--ws-text); background: var(--ws-panel); font: inherit; cursor: pointer; }
.ws-app .ws-button:hover:not(:disabled) { background: var(--ws-hover); }
.ws-app .ws-button:active:not(:disabled) { background: var(--ws-selected); }
.ws-app .ws-button:disabled { opacity: .55; cursor: not-allowed; }
.ws-app .ws-button-primary { background: var(--ws-accent); color: var(--ws-on-accent); border-color: var(--ws-accent); }
.ws-app .ws-button-primary:hover:not(:disabled) { background: var(--ws-accent-hover); }
.ws-app .ws-input { min-height: 36px; padding: var(--ws-space-2); border: 1px solid var(--ws-border); border-radius: var(--ws-radius); background: var(--ws-bg); color: var(--ws-text); font: inherit; }
.ws-app .ws-input[aria-invalid="true"] { border-color: var(--ws-danger); }
.ws-app .ws-input:disabled, .ws-app .ws-input[readonly] { background: var(--ws-panel); color: var(--ws-muted); }
.ws-app .ws-table { width: 100%; border-collapse: collapse; font-size: var(--ws-text-md); }
.ws-app .ws-table :is(th, td) { padding: var(--ws-space-2); text-align: start; border-bottom: 1px solid var(--ws-border); }
.ws-app .ws-table th { color: var(--ws-muted); }
.ws-app .ws-table-scroll { overflow-x: auto; }
.ws-app .ws-alert { padding: var(--ws-space-2); border: 1px solid var(--ws-border); border-radius: var(--ws-radius); }
.ws-app .ws-alert-danger { color: var(--ws-danger); background: var(--ws-danger-bg); border-color: var(--ws-danger-border); }
.ws-app .ws-muted { color: var(--ws-muted); }
.ws-app .ws-dialog { color: var(--ws-text); background: var(--ws-bg); border: 1px solid var(--ws-border); border-radius: var(--ws-radius-lg); max-width: calc(100vw - 32px); }
.ws-app .ws-dialog::backdrop { background: var(--ws-overlay); }
/* 선택해서 사용하는 레이아웃. 기존 앱 배치를 강제로 대체하지 않는다. */
.ws-app .ws-container { width: min(100%, var(--ws-content-width)); margin-inline: auto; padding: var(--ws-space-3); }
.ws-app .ws-stack { display: flex; flex-direction: column; gap: var(--ws-space-3); }
.ws-app .ws-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--ws-space-3); }
.ws-app .ws-grid > * { min-width: 0; }
.ws-app .ws-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: var(--ws-space-2); }
@media (max-width: 760px) { .ws-app .ws-grid { grid-template-columns: 1fr; } }
/* 인증 UI는 색상 연결이 아닌 동일한 구조/치수/동작을 공유한다. */
.ws-app .ws-auth-screen:not([hidden]) { display:grid !important; place-items:center; min-height:100svh !important; padding:32px 20px !important; width:100% !important; max-width:none !important; margin:0 !important; background:radial-gradient(ellipse at 50% 0%,var(--ws-info-bg),transparent 65%),var(--ws-bg) !important; }
.ws-app .ws-auth-card { position:relative; width:420px !important; max-width:100% !important; margin:0 !important; padding:32px !important; border:1px solid var(--ws-border) !important; border-radius:16px !important; background:var(--ws-bg) !important; color:var(--ws-text) !important; box-shadow:0 18px 64px rgb(0 0 0 / .08) !important; font-family:var(--ws-font) !important; }
.ws-auth-page > .topbar { display:none !important; }
.ws-app .ws-auth-heading { margin:0 0 28px !important; text-align:left !important; }
.ws-app .ws-auth-mark { display:grid;place-items:center;width:44px;height:44px;margin-bottom:20px;border-radius:12px;background:var(--ws-accent);color:var(--ws-on-accent); }
.ws-app .ws-auth-mark svg { width:24px;height:24px; }
.ws-app .ws-auth-heading h2 { font-size:24px !important;line-height:1.4 !important;letter-spacing:-.03em;margin:0 0 6px !important;color:var(--ws-text) !important; }
.ws-app .ws-auth-heading p { color:var(--ws-muted);font-size:14px;line-height:1.6;margin:0; }
.ws-app .ws-auth-card form, .ws-app form.ws-auth-card { gap:16px; }
.ws-app :is(.ws-auth-card,.ws-password-card) label { display:block !important;font-size:13px !important;font-weight:600;color:var(--ws-text) !important;text-align:left;line-height:1.5;margin-bottom:6px; }
.ws-app :is(.ws-auth-card,.ws-password-card) input:not([type=hidden]):not([type=checkbox]) { display:block !important;width:100% !important;min-width:0;min-height:44px !important;height:44px !important;padding:10px 12px !important;margin:6px 0 0 !important;box-sizing:border-box !important;font:400 14px var(--ws-font) !important;border:1px solid var(--ws-border) !important;border-radius:8px !important;background:var(--ws-bg) !important;color:var(--ws-text) !important; }
.ws-app :is(.ws-auth-card,.ws-password-card) input:focus-visible { outline:3px solid color-mix(in srgb,var(--ws-accent) 30%,transparent) !important;outline-offset:1px !important;border-color:var(--ws-accent) !important; }
.ws-app .ws-auth-card :is(.field,.ws-auth-field) { margin:0 0 16px !important; }
.ws-app .ws-auth-card :is(button[type=submit],button.ws-auth-submit) { width:100% !important;min-height:44px !important;margin:8px 0 0 !important;border:1px solid var(--ws-accent) !important;border-radius:8px !important;background:var(--ws-accent) !important;color:var(--ws-on-accent) !important;font:600 14px var(--ws-font) !important;cursor:pointer; }
.ws-app .ws-auth-card .modal-actions { display:block !important;margin:0 !important; }
.ws-app .ws-auth-card :is(.error,.err,#login-error,.ws-auth-feedback) { color:var(--ws-danger) !important;font-size:13px;line-height:1.5; }
.ws-app .ws-auth-card :is(.error,.err,#login-error,.ws-auth-feedback):empty { display:none !important; }
.ws-app [data-ws-auth-hide], .ws-app [data-ws-original-action] { display:none !important; }
.ws-app .ws-auth-card > .ws-auth-heading ~ label { margin-top:16px; }
.ws-app dialog.ws-auth-card { margin:auto !important;max-height:calc(100svh - 32px);overflow:auto; }
.ws-app dialog.ws-auth-card .account-head { position:absolute;right:12px;top:12px;margin:0; }
.ws-app dialog.ws-auth-card .account-head h2 { display:none; }
.ws-app dialog.ws-auth-card .account-head button { font-size:12px;padding:6px 8px; }
.ws-app .ws-password-card { width:440px !important;max-width:calc(100vw - 32px) !important;padding:28px !important;border:1px solid var(--ws-border) !important;border-radius:16px !important;background:var(--ws-bg) !important;color:var(--ws-text) !important;box-shadow:0 24px 80px rgb(0 0 0 / .2);font:14px var(--ws-font);max-height:calc(100svh - 32px);overflow:auto; }
.ws-app dialog.ws-password-card { margin:auto; }
.ws-app .ws-password-card::backdrop { background:var(--ws-overlay);backdrop-filter:blur(3px); }
.ws-app .ws-password-card .ws-auth-heading { margin-bottom:24px !important; }
.ws-app .ws-password-card .ws-auth-heading h2 { font-size:20px !important; }
.ws-app .ws-password-card form { display:flex !important;flex-direction:column !important;gap:16px !important;margin:0 !important; }
.ws-app .ws-password-card .field { margin:0 !important; }
.ws-app .ws-password-actions { display:flex !important;justify-content:flex-end;gap:8px !important;margin-top:8px !important; }
.ws-app .ws-password-actions button { width:auto !important;min-height:40px;padding:8px 16px !important;border:1px solid var(--ws-border);border-radius:8px;font:600 14px var(--ws-font);background:var(--ws-bg);color:var(--ws-text);cursor:pointer; }
.ws-app .ws-password-actions button[type=submit],.ws-app .ws-password-actions button.primary { color:var(--ws-on-accent) !important;background:var(--ws-accent) !important;border-color:var(--ws-accent) !important; }
.ws-app .ws-auth-feedback { font-size:13px;color:var(--ws-danger);margin:0; }
.ws-app .ws-security-panel { padding:24px;border:1px solid var(--ws-border);border-radius:12px;background:var(--ws-panel); }
.ws-app .ws-security-open { padding:10px 16px;border:1px solid var(--ws-border);border-radius:8px;background:var(--ws-bg);color:var(--ws-text);font:600 14px var(--ws-font);cursor:pointer; }
.ws-app .ws-account-menu { position:relative;flex:none;font:14px var(--ws-font); }
.ws-app .ws-account-menu > summary { display:flex;align-items:center;gap:8px;list-style:none;cursor:pointer;padding:8px 12px;min-height:38px;border:1px solid var(--ws-border);border-radius:8px;background:var(--ws-bg);color:var(--ws-text);font-weight:600;white-space:nowrap; }
.ws-app .ws-account-menu > summary::-webkit-details-marker { display:none; }
.ws-app .ws-account-menu > summary::after { content:'⌄';color:var(--ws-muted);margin-left:8px; }
.ws-app .ws-account-dropdown { position:absolute;right:0;top:calc(100% + 8px);width:210px;max-width:calc(100vw - 32px);z-index:1200;padding:6px;border:1px solid var(--ws-border);border-radius:12px;box-shadow:0 12px 36px rgb(0 0 0 / .15);background:var(--ws-bg); }
.ws-app .ws-account-dropdown button { display:block;width:100%;text-align:left;border:0;border-radius:6px;padding:11px 12px;background:transparent;color:var(--ws-text);font:14px var(--ws-font);cursor:pointer; }
.ws-app .ws-account-dropdown button:hover { background:var(--ws-hover); }
.ws-app .ws-account-dropdown .ws-signout { border-top:1px solid var(--ws-border);border-radius:0;color:var(--ws-danger);margin-top:4px; }
@media(max-width:480px) { .ws-app .ws-auth-card { padding:28px 24px !important; } .ws-app .ws-password-card { padding:24px !important; } }

.ws-app .ws-account-trigger { min-height:38px !important;padding:8px 12px !important;border:1px solid var(--ws-border) !important;border-radius:8px !important;background:var(--ws-bg) !important;color:var(--ws-text) !important;font-weight:600; }

.ws-app .ws-auth-card { max-width:min(420px,calc(100vw - 40px)) !important; }
.ws-app form.login.ws-auth-card { display:block !important; }
.ws-app .ws-auth-card > .ws-auth-label { margin:16px 0 6px !important; }
.ws-app .ws-auth-card > .ws-auth-heading + .ws-auth-label { margin-top:0 !important; }
.ws-app .ws-auth-card > input { margin:0 !important; }
.ws-app .ws-password-card { min-width:0 !important; }
.ws-app .ws-auth-advanced { color:var(--ws-muted);font-size:13px; }
.ws-app .ws-auth-advanced summary { cursor:pointer;margin-bottom:12px; }
/* 동일한 애플리케이션 프레임. /ai/pangyo에는 연결하지 않는다. */
:root { --ws-header-height:64px; --ws-sidebar-width:248px; --ws-content-padding:24px; }
body.ws-shell-ready { margin:0 !important;padding:var(--ws-header-height) 0 0 var(--ws-sidebar-width) !important;min-height:100svh;box-sizing:border-box;background:var(--ws-panel); }
.ws-shell-header { position:fixed !important;inset:0 0 auto 0;z-index:1050;height:var(--ws-header-height) !important;min-height:var(--ws-header-height);display:flex;align-items:center;gap:16px;padding:0 24px !important;border-bottom:1px solid var(--ws-border);background:var(--ws-bg);color:var(--ws-text);font:14px var(--ws-font);box-sizing:border-box; }
.ws-shell-brand { display:flex;align-items:center;gap:12px;min-width:0;color:var(--ws-text);text-decoration:none;font-weight:700; }
.ws-ci { display:inline-grid;grid-template-columns:repeat(2,8px);grid-template-rows:repeat(2,8px);gap:3px;place-content:center;flex:none;width:36px;height:36px;border-radius:10px;background:var(--ws-accent);color:var(--ws-on-accent); }
.ws-ci i { display:block;border-radius:2px;background:currentColor; }.ws-ci i:last-child { opacity:.55; }
.ws-shell-title { overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:16px;letter-spacing:-.02em; }
.ws-shell-version { font-size:11px;color:var(--ws-muted);border:1px solid var(--ws-border);border-radius:5px;padding:3px 6px; }
.ws-shell-header-end { margin-left:auto;display:flex;align-items:center;gap:12px; }
.ws-shell-sidebar { position:fixed;inset:var(--ws-header-height) auto 0 0;z-index:1040;width:var(--ws-sidebar-width);box-sizing:border-box;background:var(--ws-bg);border-right:1px solid var(--ws-border);display:flex;flex-direction:column;padding:24px 12px 12px;overflow-y:auto;color:var(--ws-text);font:14px var(--ws-font); }
.ws-nav-caption { margin:0 12px 12px;color:var(--ws-muted);font-size:11px;font-weight:600;letter-spacing:.08em; }
.ws-shell-nav { display:flex;flex-direction:column;gap:4px; }
.ws-shell-nav button { display:flex;align-items:center;gap:12px;width:100%;min-height:42px;border:1px solid transparent;border-radius:8px;padding:10px 12px;margin:0;background:transparent;color:var(--ws-muted);text-align:left;font:500 14px var(--ws-font);cursor:pointer; }
.ws-shell-nav button::before { content:'';width:14px;height:14px;flex:none;border:1.5px solid currentColor;border-radius:4px;opacity:.7; }
.ws-shell-nav button:hover { background:var(--ws-hover);color:var(--ws-text); }
.ws-shell-nav button[aria-current=page] { background:var(--ws-info-bg);color:var(--ws-accent);font-weight:600; }
.ws-shell-nav button:focus-visible,.ws-shell-menu-toggle:focus-visible { outline:2px solid var(--ws-accent);outline-offset:2px; }
.ws-shell-nav-bottom { margin-top:auto;border-top:1px solid var(--ws-border);padding-top:12px; }
.ws-shell-nav-bottom p { margin:12px;color:var(--ws-muted);font-size:11px; }
.ws-shell-menu-toggle { display:none;align-items:center;justify-content:center;width:36px;height:36px;border:1px solid var(--ws-border);border-radius:8px;color:var(--ws-text);background:var(--ws-bg);font-size:20px;cursor:pointer; }
.ws-shell-scrim { display:none; }
body:not(.ws-shell-ready) > :is(.ws-shell-header,.ws-shell-sidebar,.ws-shell-scrim) { display:none !important; }
body.ws-shell-ready [data-ws-shell-original] { display:none !important; }
body.ws-shell-ready > :is(#root,#app,.app) { min-width:0; }
body.ws-shell-ready .app { min-height:calc(100svh - var(--ws-header-height));padding:var(--ws-content-padding) !important; }
body.ws-shell-ready .app > header { flex-wrap:wrap;margin-bottom:20px;padding:12px 16px;background:var(--ws-bg);border:1px solid var(--ws-border);border-radius:10px; }
body.ws-shell-ready .app > header .brand { font-size:16px; }
body.ws-shell-ready > main,body.ws-shell-ready > #app > #main-content { max-width:none;margin:0;padding:var(--ws-content-padding);box-sizing:border-box; }
body.ws-shell-ready .el-container { min-width:0; }.ws-project-gimpo body.ws-shell-ready #app > .el-container { height:calc(100svh - var(--ws-header-height)) !important; }
body.ws-shell-ready .el-main { padding:var(--ws-content-padding);background:var(--ws-panel); }
body.ws-shell-ready :is(.card,.panel,.tile,.el-card) { border-color:var(--ws-border);border-radius:10px;background-color:var(--ws-bg); }
body.ws-shell-ready :is(.page-header h2,h2.page-title) { font-size:20px;line-height:1.4;letter-spacing:-.02em; }
body.ws-shell-ready .ws-account-menu { position:relative; }
@media(max-width:760px) {
 body.ws-shell-ready { padding-left:0 !important;--ws-content-padding:16px; }
 .ws-shell-header { padding-inline:16px !important;gap:10px; }
 .ws-shell-menu-toggle { display:flex; }
 .ws-shell-sidebar { transform:translateX(-100%);visibility:hidden;transition:transform .16s ease,visibility .16s; }
 body.ws-nav-open .ws-shell-sidebar { transform:none;visibility:visible; }
 body.ws-nav-open .ws-shell-scrim { display:block;position:fixed;inset:var(--ws-header-height) 0 0;z-index:1030;background:var(--ws-overlay);border:0; }
 body.ws-nav-open { overflow:hidden; }
 .ws-shell-version { display:none; }
 .ws-shell-header .ws-shell-brand { gap:8px; }.ws-shell-header .ws-shell-title { font-size:14px; }
}
@media(prefers-reduced-motion:reduce) { .ws-shell-sidebar { transition:none; } }

.ws-shell-brand { flex:1;max-width:360px; }
@media(max-width:760px) { .ws-shell-version { display:none; } }

.ws-shell-theme { margin-left:auto;max-width:96px;min-height:36px;border:1px solid var(--ws-border);border-radius:8px;padding:6px;color:var(--ws-text);background:var(--ws-bg);font:13px var(--ws-font); }.ws-shell-header-end { margin-left:0; }

:root.ws-project-samsung {
  --bg: var(--ws-bg);
  --fg: var(--ws-text);
  --muted: var(--ws-muted);
  --border: var(--ws-border);
  --card-bg: var(--ws-panel);
  --critical: var(--ws-danger);
  --warning: var(--ws-warning);
  --ok: var(--ws-success);
  --info: var(--ws-accent);
  --primary: var(--ws-accent);
  --primary-fg: var(--ws-on-accent);
}
/* SVG와 상태 배지의 의미별 색상, 흐름 애니메이션은 유지한다. */
.ws-project-samsung .seg.active, .ws-project-samsung .refresh-now:hover, .ws-project-samsung .btn { color: var(--ws-on-accent); }
.ws-project-samsung .form-msg.ok { color: var(--ws-success); }
.ws-project-samsung main { overflow-x: auto; }
.ws-project-samsung .help { border-radius: var(--ws-radius-lg); }

.ws-project-samsung body { font-size: var(--ws-text-md); }
.ws-project-samsung :is(button, select, input:not([type="checkbox"]):not([type="radio"]), textarea) { font-family: var(--ws-font); }
.ws-project-samsung :is(button, select, input:not([type="checkbox"]):not([type="radio"]), textarea) { border-radius: var(--ws-radius); }
.ws-project-samsung input:not([type="checkbox"]):not([type="radio"]), .ws-project-samsung textarea { background-color: var(--ws-bg); color: var(--ws-text); border-color: var(--ws-border); }
"""
# END WEB STANDARD GENERATED UI

_PAGE_TEMPLATE = """<!doctype html>
<html lang="ko" class="ws-app ws-project-samsung">
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
  <div class="topbar-right">{refresh}{userbox}</div>
</header>
<main>
  <h2 class="page-title">{heading}</h2>
  <div class="meta">{meta}</div>
  {table}
</main>
<script data-ws-auth>/* 선택적 테마 동작. 업무 데이터/계정 저장소와 별도 키를 사용한다. */
(function () {{
  'use strict';
  var key = 'web-standard:theme';
  var choices = ['system', 'light', 'dark'];
  function apply(value, persist) {{
    if (choices.indexOf(value) < 0) value = 'system';
    var root = document.documentElement;
    root.classList.toggle('ws-theme-light', value === 'light');
    root.classList.toggle('ws-theme-dark', value === 'dark');
    root.setAttribute('data-ws-theme', value);
    if (persist) {{ try {{ localStorage.setItem(key, value); }} catch (_) {{}} }}
    document.querySelectorAll('[data-ws-theme-select]').forEach(function (el) {{ el.value = value; }});
  }}
  var initial = 'system';
  try {{ initial = localStorage.getItem(key) || initial; }} catch (_) {{}}
  apply(initial, false);
  document.addEventListener('DOMContentLoaded', function () {{ apply(document.documentElement.getAttribute('data-ws-theme') || initial, false); }}, {{ once: true }});
  document.addEventListener('change', function (event) {{
    if (event.target.matches('[data-ws-theme-select]')) apply(event.target.value, true);
  }});
  window.addEventListener('storage', function (event) {{ if (event.key === key) apply(event.newValue, false); }});
}}());

/* 인증 요청/토큰/쿠키를 다루지 않는 공통 표현 계층. 기존 노드와 이벤트를 보존한다. */
(function(){{
'use strict';
var project=(document.documentElement.className.match(/ws-project-([\\w-]+)/)||[])[1];
var names={{bali:'메모 앱',canggu:'Kubernetes 자동 운영',pangyo:'MemoNest',samsung:'TrafficPolicy',seongnam:'OS Monitor',seoul:'클러스터 점검'}};
if(project==='pangyo'||!names[project])return;
var pendingPassword=project==='samsung'&&/\\/settings\\/?$/.test(location.pathname);
var passwordDialog=null;
function element(tag,cls,text){{var e=document.createElement(tag);if(cls)e.className=cls;if(text)e.textContent=text;return e;}}
function heading(title,subtitle,mark){{var h=element('div','ws-auth-heading');if(mark){{var icon=element('div','ws-auth-mark');icon.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="2" fill="currentColor" stroke="none"/><rect x="14" y="3" width="7" height="7" rx="2" fill="currentColor" stroke="none"/><rect x="3" y="14" width="7" height="7" rx="2" fill="currentColor" stroke="none"/><rect x="14" y="14" width="7" height="7" rx="2" fill="currentColor" stroke="none" opacity=".55"/></svg>';h.append(icon);}}h.append(element('h2','',title),element('p','',subtitle));return h;}}
function hide(e){{if(e)e.setAttribute('data-ws-auth-hide','');}}
function original(e){{if(e)e.setAttribute('data-ws-original-action','');}}
function available(e){{return !!e&&e.isConnected&&!e.closest('[hidden]');}}
function login(card,screen,form){{if(!card||card.dataset.wsAuthReady)return;card.dataset.wsAuthReady='1';card.classList.add('ws-auth-card');if(screen)screen.classList.add('ws-auth-screen');
 if(project==='bali'){{hide(card.firstElementChild);var buttons=card.querySelectorAll('button');buttons.forEach(function(b){{if(/로그인/.test(b.textContent))b.classList.add('ws-auth-submit');}});hide(card.lastElementChild&&card.lastElementChild.tagName==='P'?card.lastElementChild:null);}}
 else {{Array.from(card.children).forEach(function(c){{if(c.matches('h1,h2,h3,.sub,.muted'))hide(c);}});}}
 card.prepend(heading('로그인',names[project]+' 계정으로 로그인하세요.',true));
 var fields=card.querySelectorAll('input:not([type=hidden]):not([type=checkbox])');fields.forEach(function(input,index){{if(!input.id)input.id='ws-login-'+project+'-'+index;if(!input.closest('label')&&!card.querySelector('label[for="'+input.id+'"]')){{var label=element('label','ws-auth-label',input.type==='password'?'비밀번호':'아이디');label.htmlFor=input.id;input.before(label);}}if(input.type==='password')input.autocomplete='current-password';else if(!input.autocomplete)input.autocomplete='username';}});
 if(form)form.setAttribute('aria-label','로그인');
}}
function stylePassword(card,form,cancel){{card.classList.add('ws-password-card');var old=card.querySelector(':scope > h2,:scope > h3');hide(old);card.prepend(heading('비밀번호 변경','현재 비밀번호를 확인한 뒤 새 비밀번호를 저장합니다.',false));
 var action=form?form.querySelector('button[type=submit]'):card.querySelector('.edit-actions .primary');if(!action&&form)action=form.querySelector('button.btn');
 var actions=element('div','ws-password-actions');if(form){{if(cancel)actions.append(cancel);if(action)actions.append(action);form.append(actions);}}else{{var row=card.querySelector('.edit-actions');if(row)row.classList.add('ws-password-actions');}}
 if(action)action.textContent='변경 사항 저장';
 var inputs=Array.from((form||card).querySelectorAll('input[type=password]'));
 if(inputs.length===2){{var label=element('label','ws-auth-field','새 비밀번호 확인');var confirm=element('input');confirm.type='password';confirm.autocomplete='new-password';confirm.required=true;confirm.dataset.wsConfirm='1';label.append(confirm);if(form)form.insertBefore(label,actions);else{{var grid=card.querySelector('.edit-grid');grid.append(label);}}var status=element('p','ws-auth-feedback');status.setAttribute('role','alert');label.after(status);
 var validate=function(event){{if(confirm.value!==inputs[1].value||!confirm.value){{event.preventDefault();event.stopImmediatePropagation();status.textContent='새 비밀번호가 일치하지 않습니다.';confirm.focus();}}else status.textContent='';}};if(action)action.addEventListener('click',validate,true);if(form)form.addEventListener('submit',validate,true);}}
 if(form){{var user=form.querySelector('input[name=new_username]');if(user){{var field=user.closest('.field');if(field){{var advanced=element('details','ws-auth-advanced');advanced.append(element('summary','','아이디 변경 (선택)'),field);form.insertBefore(advanced,actions);}}}}}}card.setAttribute('aria-label','비밀번호 변경');
}}
function modalize(form){{if(!form||form.dataset.wsPasswordReady)return;form.dataset.wsPasswordReady='1';var parent=form.parentElement;parent.classList.add('ws-security-panel');var dialog=element('dialog','ws-password-card');var open=element('button','ws-security-open','비밀번호 변경');open.type='button';var cancel=element('button','','취소');cancel.type='button';cancel.addEventListener('click',function(){{dialog.close();}});form.before(open,dialog);dialog.append(form);stylePassword(dialog,form,cancel);open.addEventListener('click',function(){{dialog.showModal();}});passwordDialog=dialog;if(pendingPassword){{pendingPassword=false;dialog.showModal();}}}}
function menu(host,logout,password,account){{if(!host||host.querySelector(':scope > .ws-account-menu'))return;var detail=element('details','ws-account-menu');var summary=element('summary','','계정');var panel=element('div','ws-account-dropdown');detail.append(summary,panel);
 function action(text,callback,cls){{var b=element('button',cls,text);b.type='button';b.addEventListener('click',function(){{detail.open=false;callback();}});panel.append(b);}}
 if(account){{action('계정 관리',function(){{account.click();}});original(account);}}
 if(password){{action('비밀번호 변경',function(){{if(typeof password==='function')password();else password.click();}});if(typeof password!=='function')original(password);}}
 if(logout){{action('로그아웃',function(){{logout.click();}},'ws-signout');original(logout);}}
 host.append(detail);
}}
function requestPassword(){{pendingPassword=true;var settings=document.querySelector('[data-tab=settings],[data-hash="#settings"]');if(settings)settings.click();setTimeout(schedule,0);}}
function enhance(){{
 if(project==='bali'){{var input=document.getElementById('username');if(input){{var card=input.closest('div[style]');while(card&&(!card.querySelector('button')||!card.querySelector('input[type=password]')))card=card.parentElement;if(card){{var outer=card.parentElement;if(outer&&outer.querySelectorAll('input').length===2){{login(outer,outer.parentElement,null);}}}}}}var logout=Array.from(document.querySelectorAll('header button')).find(function(b){{return b.textContent==='로그아웃'&&!b.closest('.ws-account-menu');}});if(logout)menu(logout.parentElement,logout,null,null);}}
 if(project==='canggu'){{var form=document.querySelector('form.login');if(form)login(form,form.parentElement,form);var pw=Array.from(document.querySelectorAll('header > button')).find(function(b){{return b.textContent==='비밀번호 변경';}});var lo=Array.from(document.querySelectorAll('header > button')).find(function(b){{return b.textContent==='로그아웃';}});if(lo)menu(lo.parentElement,lo,pw,null);var modal=document.querySelector('.modal-backdrop .modal');if(modal&&modal.querySelector('input[type=password]')&&!modal.dataset.wsPasswordReady){{modal.dataset.wsPasswordReady='1';stylePassword(modal,null,null);modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');}}}}
 if(project==='seoul'||project==='seongnam'){{var overlay=document.getElementById('login-overlay');var form=document.getElementById('login-form');if(form)login(project==='seoul'?form:form.parentElement,overlay,form);var lo=document.getElementById('logout-btn');if(available(lo))menu(lo.parentElement,lo,requestPassword,null);modalize(document.getElementById(project==='seoul'?'password-form':'pw-form'));}}
 if(project==='samsung'){{var form=document.querySelector('form[action$="/login"]');if(form){{login(form.parentElement,form.closest('main'),form);document.body.classList.add('ws-auth-page');}}var settings=document.querySelector('form[action$="/settings"]');if(settings){{if(window.WSSettings&&window.WSSettings.supported){{if(!settings.dataset.wsSdkReady){{settings.dataset.wsSdkReady='1';original(settings);var open=element('button','ws-security-open','개인 설정 열기');open.type='button';open.addEventListener('click',function(){{window.WSSettings.open('personal');}});settings.before(open);}}if(pendingPassword){{pendingPassword=false;setTimeout(function(){{var view=new URLSearchParams(location.search).get('ws-panel');window.WSSettings.open(view==='users'?'users':'personal');}},0);}}}}else modalize(settings);}}var logout=document.querySelector('form[action$="/logout"] button');var link=document.querySelector('.userbox a[href$="/settings"]');if(logout)menu(logout.closest('.userbox'),logout,link,null);}}
 if(project==='pangyo'){{var btn=document.getElementById('account-btn');if(btn)btn.classList.add('ws-account-trigger');var form=document.getElementById('account-login-form');if(form){{var dialog=form.closest('dialog');if(dialog){{login(dialog,null,form);dialog.classList.add('ws-auth-card');}}}}else{{var dialog=document.getElementById('account-dialog');if(dialog&&dialog.dataset.wsAuthReady){{dialog.classList.remove('ws-auth-card');delete dialog.dataset.wsAuthReady;var h=dialog.querySelector(':scope > .ws-auth-heading');if(h)h.remove();}}}}}}
 if(pendingPassword&&passwordDialog&&passwordDialog.isConnected&&passwordDialog.parentElement.getClientRects().length){{pendingPassword=false;if(!passwordDialog.open)passwordDialog.showModal();}}
}}
var scheduled=false;function schedule(){{if(scheduled)return;scheduled=true;requestAnimationFrame(function(){{scheduled=false;enhance();}});}}
document.addEventListener('keydown',function(e){{var modal=document.querySelector('.modal-backdrop .ws-password-card');if(modal&&e.key==='Tab'){{var nodes=Array.from(modal.querySelectorAll('input,button')).filter(function(n){{return !n.disabled&&n.getClientRects().length;}});var first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){{e.preventDefault();last.focus();}}else if(!e.shiftKey&&document.activeElement===last){{e.preventDefault();first.focus();}}}}if(modal&&e.key==='Enter'&&e.target.tagName==='INPUT'){{e.preventDefault();var submit=modal.querySelector('button.primary');if(submit)submit.click();}}if(e.key==='Escape'){{document.querySelectorAll('.ws-account-menu[open]').forEach(function(d){{d.open=false;}});var card=document.querySelector('.modal-backdrop .ws-password-card');if(card){{var cancel=card.querySelector('.edit-actions button');if(cancel)cancel.click();}}}}}});
document.addEventListener('click',function(e){{document.querySelectorAll('.ws-account-menu[open]').forEach(function(d){{if(!d.contains(e.target))d.open=false;}});}});
function start(){{enhance();new MutationObserver(schedule).observe(document.body,{{childList:true,subtree:true,attributes:true,attributeFilter:['hidden']}});}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{{once:true}});else start();
}}());

/* 공통 고정 프레임. 기존 React/Vue 소유 노드와 업무 이벤트를 재배치하지 않는다. */
(function(){{
'use strict';
var root=document.documentElement;
var project=(root.className.match(/ws-project-([\\w-]+)/)||[])[1];
var titles={{bali:'메모',canggu:'클러스터 운영',gimpo:'백업·복구',samsung:'트래픽 운영',seongnam:'OS 모니터링',seoul:'클러스터 점검',tashkent:'모델 팩토리'}};
if(project==='pangyo'||!titles[project])return;
var header,sidebar,nav,footer,account,scrim,toggle,items=[],actions=[],signature='',accountSignature='',queued=false,activePage='overview';
var mobile=window.matchMedia('(max-width:760px)');
function el(tag,cls,text){{var n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n;}}
function text(n,value){{if(n.textContent!==value)n.textContent=value;}}
function hide(n){{if(n&&!n.hasAttribute('data-ws-shell-original'))n.setAttribute('data-ws-shell-original','');}}
function closeMenu(focus){{document.body.classList.remove('ws-nav-open');toggle.setAttribute('aria-expanded','false');sidebar.inert=mobile.matches;if(focus)toggle.focus();}}
function openMenu(){{document.body.classList.add('ws-nav-open');toggle.setAttribute('aria-expanded','true');sidebar.inert=false;var first=nav.querySelector('button');if(first)first.focus();}}
function setup(){{
 header=el('header','ws-shell-header');header.setAttribute('aria-label','공통 상단 메뉴');
 toggle=el('button','ws-shell-menu-toggle','☰');toggle.type='button';toggle.setAttribute('aria-label','서비스 메뉴');toggle.setAttribute('aria-controls','ws-shell-sidebar');toggle.setAttribute('aria-expanded','false');toggle.addEventListener('click',function(){{document.body.classList.contains('ws-nav-open')?closeMenu(true):openMenu();}});
 var brand=el('a','ws-shell-brand');brand.href=location.pathname;brand.setAttribute('aria-label',titles[project]+' 홈');var mark=el('span','ws-ci');mark.setAttribute('aria-hidden','true');mark.innerHTML='<i></i><i></i><i></i><i></i>';brand.append(mark,el('span','ws-shell-title',titles[project]));
 var theme=el('select','ws-shell-theme');theme.setAttribute('aria-label','테마');theme.setAttribute('data-ws-theme-select','');[['system','시스템'],['light','Light'],['dark','Dark']].forEach(function(pair){{var option=el('option','',pair[1]);option.value=pair[0];theme.append(option);}});theme.value=root.getAttribute('data-ws-theme')||'system';account=el('div','ws-shell-header-end');header.append(toggle,brand,el('span','ws-shell-version','WEB STANDARD'),theme,account);
 sidebar=el('aside','ws-shell-sidebar');sidebar.id='ws-shell-sidebar';sidebar.setAttribute('aria-label','서비스 탐색');nav=el('nav','ws-shell-nav');nav.setAttribute('aria-label','주 메뉴');footer=el('div','ws-shell-nav-bottom');footer.append(el('p','','공통 디자인 · 개인 설정'));sidebar.append(el('p','ws-nav-caption','WORKSPACE'),nav,footer);
 scrim=el('button','ws-shell-scrim');scrim.type='button';scrim.setAttribute('aria-label','서비스 메뉴 닫기');scrim.tabIndex=-1;scrim.addEventListener('click',function(){{closeMenu(true);}});document.body.prepend(header,sidebar,scrim);sidebar.inert=mobile.matches;
 mobile.addEventListener('change',function(){{closeMenu(false);}});
}}
function visible(n){{return n&&n.isConnected&&!n.closest('[hidden]')&&n.getClientRects().length>0;}}
function activate(node){{if(node&&node.isConnected){{node.click();closeMenu(false);}}}}
function collect(){{var list=[];
 function add(label,node,key){{if(!label||!node||node.hidden||node.closest('[hidden]'))return;list.push({{label:label,key:key||label,run:function(){{activate(node);}}}});}}
 if(project==='seoul'||project==='seongnam'){{
  document.querySelectorAll('#tabs button').forEach(function(n){{add(n.textContent.trim(),n,n.dataset.hash||n.dataset.tab);}});hide(document.querySelector('body > .topbar'));
 }}else if(project==='gimpo'){{
  document.querySelectorAll('.el-aside .el-menu-item').forEach(function(n){{add(n.textContent.trim(),n,n.dataset.wsPage);}});hide(document.querySelector('.el-aside'));hide(document.querySelector('.el-header'));
 }}else if(project==='samsung'){{
  var sdk=window.WSSettings;var grants=sdk&&sdk.supported?(sdk.pages||[]):null;document.querySelectorAll('body > .topbar .tabs > a').forEach(function(n){{var href=n.getAttribute('href');var page=/\\/policies(?:\\?|$)/.test(href)?'policies':'flows';if(grants===null||grants.includes(page))add(n.textContent.trim(),n,href);}});hide(document.querySelector('body > .topbar'));
 }}else if(project==='tashkent'){{
  var sdk=window.WSSettings;var grants=sdk&&sdk.supported?(sdk.pages||[]):null;document.querySelectorAll('body > header:not(.ws-shell-header) nav > a').forEach(function(n){{var href=n.getAttribute('href');var page=href==='/products/new'?'new':href==='/presets'?'presets':href==='/debug'||href==='/dbcheck'?'debug':'products';if(grants===null||grants.includes(page))add(n.textContent.trim(),n,href);}});hide(document.querySelector('body > header:not(.ws-shell-header)'));
 }}else if(project==='canggu'){{
  var sdk=window.WSSettings||{{}};var grants=Array.isArray(sdk.pages)?sdk.pages:null;
  var labels={{overview:'개요',resources:'리소스',operations:'운영',activity:'활동',backup:'백업·복구',access:'접근 발급'}};
  var categories=function(title){{if(/웹 사용자/.test(title))return 'users';if(/백업|Velero/.test(title))return 'backup';if(/접근 발급|kubeconfig/.test(title))return 'access';if(/피드|이벤트|감사|이력/.test(title))return 'activity';if(/이슈|Operating|규칙/.test(title))return 'operations';return 'resources';}};
  var allowed=function(page){{return grants===null||grants.includes(page);}};
  if(!allowed(activePage))activePage=Object.keys(labels).find(allowed)||'settings';
  Object.keys(labels).filter(allowed).forEach(function(page){{list.push({{label:labels[page],key:page,run:function(){{activePage=page;schedule();window.scrollTo(0,0);closeMenu(false);}}}});}});
  document.querySelectorAll('.app .panel').forEach(function(panel){{var heading=panel.querySelector('h2');if(!heading)return;var page=categories(heading.textContent);var hidden=!allowed(page)||page!==activePage;if(panel.hidden!==hidden)panel.hidden=hidden;}});
  var overview=document.querySelector('.app .overview');if(overview){{var hidden=activePage!=='overview'||!allowed('overview');if(overview.hidden!==hidden)overview.hidden=hidden;}}

  hide(document.querySelector('.app > header .ws-account-menu'));
 }}else if(project==='bali'){{
  var sdk=window.WSSettings;var granted=sdk&&sdk.supported?(sdk.pages||[]):['notes'];if(granted.includes('notes'))list.push({{label:'메모',key:'notes',run:function(){{window.scrollTo(0,0);closeMenu(false);}}}});hide(document.querySelector('#root header'));
 }}
 return list;
}}
function accountActions(){{var list=Array.from(document.querySelectorAll('.ws-account-dropdown button')).filter(function(n){{return !header.contains(n);}}).map(function(n){{return {{label:n.textContent.trim(),run:function(){{activate(n);}}}};}});if(list.length&&window.WSSettings&&window.WSSettings.supported&&!list.some(function(a){{return a.label==='비밀번호 변경';}}))list.unshift({{label:'비밀번호 변경',run:function(){{window.WSSettings.open('personal');}}}});if(!list.length&&window.WSSettings&&window.WSSettings.supported&&(project==='tashkent'||project==='gimpo'))list=[{{label:'비밀번호 변경',run:function(){{window.WSSettings.open('personal');}}}},{{label:'로그아웃',run:function(){{window.WSSettings.logout();}}}}];return list;}}
function refresh(){{
 if(!header)setup();
 var signIn=Array.from(document.querySelectorAll('.ws-auth-card')).some(visible);
 var ready=!signIn&&(project==='gimpo'||project==='tashkent'||project==='samsung'||(project==='bali'&&!!document.querySelector('#root header'))||!!document.querySelector('.app > header,#tabs:not([hidden])'));
 if(document.body.classList.contains('ws-shell-ready')!==ready){{document.body.classList.toggle('ws-shell-ready',ready);document.dispatchEvent(new CustomEvent('ws:shell-ready',{{detail:{{project:project,ready:ready}}}}));}}
 if(!ready){{closeMenu(false);return;}}
 settingsNavigation();items=collect();var next=items.map(function(x){{return x.key+'|'+x.label;}}).join('\\n');
 if(signature!==next){{signature=next;var children=items.map(function(item,index){{var b=el('button','',item.label);b.type='button';b.addEventListener('click',function(){{if(items[index])items[index].run();}});return b;}});nav.replaceChildren.apply(nav,children);}}
 Array.from(nav.children).forEach(function(b,index){{var item=items[index];var current=item&&(item.key===location.hash||item.key===location.pathname||(project==='gimpo'&&item.key&&location.pathname.endsWith(item.key))||(project==='bali'&&index===0)||(project==='canggu'&&item&&item.key===activePage));if(current){{if(b.getAttribute('aria-current')!=='page')b.setAttribute('aria-current','page');}}else if(b.hasAttribute('aria-current'))b.removeAttribute('aria-current');}});
 actions=accountActions();var key=actions.map(function(x){{return x.label;}}).join('|');
 if(key!==accountSignature){{accountSignature=key;account.replaceChildren();if(actions.length){{var d=el('details','ws-account-menu');var sum=el('summary','','계정');var panel=el('div','ws-account-dropdown');actions.forEach(function(a,index){{var b=el('button',a.label==='로그아웃'?'ws-signout':'',a.label);b.type='button';b.addEventListener('click',function(){{d.open=false;if(a.label==='비밀번호 변경'&&window.WSSettings&&window.WSSettings.supported)window.WSSettings.open('personal');else if(actions[index])actions[index].run();}});panel.append(b);}});d.append(sum,panel);account.append(d);}}}}
}}
var footerKey='';function settingsNavigation(){{var sdk=window.WSSettings;var key=sdk&&sdk.supported?'personal'+(sdk.canManageUsers?'|users':''):'';if(key===footerKey)return;footerKey=key;footer.replaceChildren(el('p','','공통 디자인 · 개인 설정'));if(key){{var group=el('nav','ws-shell-nav');[['personal','개인 설정'],['users','사용자 관리']].forEach(function(pair){{if(pair[0]==='users'&&!sdk.canManageUsers)return;var b=el('button','',pair[1]);b.type='button';b.addEventListener('click',function(){{closeMenu(false);sdk.open(pair[0]);}});group.append(b);}});footer.prepend(group);}}}}
function schedule(){{if(queued)return;queued=true;requestAnimationFrame(function(){{queued=false;refresh();}});}}
function start(){{refresh();new MutationObserver(schedule).observe(document.body,{{childList:true,subtree:true,attributes:true,attributeFilter:['hidden']}});document.addEventListener('ws:settings:ready',schedule);window.addEventListener('hashchange',schedule);window.addEventListener('popstate',schedule);document.addEventListener('keydown',function(e){{if(e.key==='Escape'&&document.body.classList.contains('ws-nav-open'))closeMenu(true);if(e.key==='Tab'&&mobile.matches&&document.body.classList.contains('ws-nav-open')){{var focus=Array.from(sidebar.querySelectorAll('button')).filter(visible);var first=focus[0],last=focus[focus.length-1];if(e.shiftKey&&document.activeElement===first){{e.preventDefault();last.focus();}}else if(!e.shiftKey&&document.activeElement===last){{e.preventDefault();first.focus();}}}}}});}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{{once:true}});else start();
}}());

/* 공통 개인 설정/사용자 관리. 실제 권한과 비밀번호 검증은 네이티브 서버가 수행한다. */
(function(){{
'use strict';
var project=(document.documentElement.className.match(/ws-project-([\\w-]+)/)||[])[1];
if(!['bali','canggu','gimpo','samsung','seoul','seongnam','tashkent'].includes(project))return; // 나머지 서버 어댑터는 별도 검증 후 연결한다.
var prefix=location.pathname.indexOf('/operating')===0?'/operating/':'/';
if(project==='gimpo')prefix=document.documentElement.getAttribute('data-ws-base')||'/velero/';
if(project==='bali'){{var mount=location.pathname.match(/^\\/(demo-app|bali)(?:\\/|$)/);prefix=mount?'/'+mount[1]+'/':'/';}}
if(project==='samsung'){{var source=document.querySelector('form[action$="/logout"],form[action$="/login"],form[action$="/settings"]');if(source)prefix=new URL(source.getAttribute('action'),location.href).pathname.replace(/\\/(logout|login|settings)\\/?$/,'')+'/';}}
if(project==='seongnam')prefix=(window.API_BASE_URL||'').replace(/\\/$/,'')+'/';
if(project==='seoul'){{var matched=location.pathname.match(/^\\/(k8stest|seoul)(?:\\/|$)/);prefix=matched?'/'+matched[1]+'/':'/';}}
var generation=0,session=null,panel=null;
function el(tag,props,text){{var n=document.createElement(tag);Object.keys(props||{{}}).forEach(function(k){{if(k==='className')n.className=props[k];else if(k in n)n[k]=props[k];else n.setAttribute(k,props[k]);}});if(text)n.textContent=text;return n;}}
async function request(path,method,body){{var headers={{'Content-Type':'application/json'}};var apiPath='api/auth/';if(project==='bali'){{headers.Authorization='Bearer '+(localStorage.getItem('access_token')||'');}}else if(project==='canggu'){{var token=localStorage.getItem('canggu_token')||'';headers.Authorization='Bearer '+token;}}else if(project==='gimpo'||project==='samsung'){{apiPath='api/auth/';}}else if(project==='tashkent'){{apiPath='api/auth/';}}else if(project==='seongnam'){{apiPath='api/v1/auth/';headers.Authorization='Bearer '+(localStorage.getItem('session_token')||'');if(path==='users'||path.indexOf('users/')===0)path='ui/'+path;}}else{{apiPath='api/';if(path==='users'||path.indexOf('users/')===0)path='ui/'+path;}}var response=await fetch(prefix+apiPath+path,{{method:method||'GET',headers:headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)}});var result=await response.json().catch(function(){{return {{}};}});if(!response.ok){{var detail=result.detail;throw new Error(typeof detail==='string'?detail:response.status===401?'다시 로그인해 주세요.':'요청을 처리하지 못했습니다.');}}return result;}}

async function loadSession(){{var g=generation;var value=await request('ui/session');if(g!==generation)throw new Error('로그인 상태가 변경되었습니다.');if(!value||!value.username||typeof value.availablePages!=='object')throw new Error('개인 설정 서버 응답을 확인할 수 없습니다.');session=value;api.role=value.role;api.canManageUsers=!!value.canManageUsers;api.pages=value.pages;api.availablePages=value.availablePages;document.dispatchEvent(new CustomEvent('ws:settings:ready'));return value;}}
var styles=`:host{{font:14px var(--ws-font,system-ui);color:var(--ws-text)}}*{{box-sizing:border-box}}dialog{{width:720px;max-width:calc(100vw - 32px);max-height:calc(100svh - 32px);padding:0;margin:auto;border:1px solid var(--ws-border);border-radius:16px;color:var(--ws-text);background:var(--ws-bg);box-shadow:0 24px 80px #0003}}dialog::backdrop{{background:var(--ws-overlay);backdrop-filter:blur(3px)}}header{{display:flex;align-items:center;justify-content:space-between;padding:24px 28px;border-bottom:1px solid var(--ws-border)}}h2{{font-size:20px;margin:0 0 4px}}h3{{font-size:16px;margin:0 0 16px}}p{{line-height:1.6}}.muted{{color:var(--ws-muted);font-size:13px;margin:0}}nav{{display:flex;padding:12px 28px 0;gap:8px;border-bottom:1px solid var(--ws-border)}}button{{font:600 14px var(--ws-font,system-ui);cursor:pointer;color:var(--ws-text);background:var(--ws-bg);border:1px solid var(--ws-border);border-radius:8px;min-height:40px;padding:8px 14px}}button:disabled{{opacity:.5;cursor:not-allowed}}button:focus-visible,input:focus-visible,select:focus-visible{{outline:2px solid var(--ws-accent);outline-offset:2px}}.tab{{border:0;border-bottom:2px solid transparent;border-radius:0;background:transparent}}.tab[aria-selected=true]{{color:var(--ws-accent);border-bottom-color:var(--ws-accent)}}main{{padding:28px;overflow:auto}}form{{display:grid;gap:16px}}label{{display:block;font-size:13px;font-weight:600}}input:not([type=checkbox]),select{{display:block;width:100%;height:44px;padding:10px 12px;margin-top:6px;border:1px solid var(--ws-border);border-radius:8px;font:14px var(--ws-font,system-ui);color:var(--ws-text);background:var(--ws-bg)}}.actions{{display:flex;gap:8px;justify-content:flex-end;margin-top:8px}}.primary{{background:var(--ws-accent);border-color:var(--ws-accent);color:var(--ws-on-accent)}}.notice{{font-size:13px;min-height:20px;margin:0;color:var(--ws-muted)}}.notice.error{{color:var(--ws-danger)}}.notice.success{{color:var(--ws-success)}}fieldset{{border:1px solid var(--ws-border);border-radius:8px;padding:12px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}legend{{font-size:13px;font-weight:600;padding:0 4px}}.check{{display:flex;align-items:center;gap:8px;font-weight:400}}input[type=checkbox]{{accent-color:var(--ws-accent)}}table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px}}th,td{{padding:10px 8px;border-bottom:1px solid var(--ws-border);text-align:left;vertical-align:top;word-break:break-word}}th{{color:var(--ws-muted)}}.scroll{{overflow-x:auto}}.row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}@media(max-width:600px){{header{{padding:20px}}nav{{padding-inline:20px}}main{{padding:20px}}.row{{grid-template-columns:1fr}}fieldset{{grid-template-columns:1fr}}}}`;
class SettingsPanel extends HTMLElement{{
 constructor(){{super();this.attachShadow({{mode:'open'}});var style=el('style');style.textContent=styles;this.dialog=el('dialog',{{'aria-label':'개인 설정'}});this.head=el('header');var heading=el('div');heading.append(el('h2',{{}},'환경 설정'));this.user=el('p',{{className:'muted'}});heading.append(this.user);var close=el('button',{{type:'button','aria-label':'설정 닫기'}},'닫기');close.addEventListener('click',()=>this.dialog.close());this.head.append(heading,close);this.tabs=el('nav',{{'aria-label':'설정 메뉴'}});this.main=el('main');this.dialog.append(this.head,this.tabs,this.main);this.shadowRoot.append(style,this.dialog);this.dialog.addEventListener('close',()=>{{this.main.replaceChildren();if(this.returnFocus&&this.returnFocus!==document.body&&this.returnFocus!==document.documentElement&&this.returnFocus.isConnected&&!this.returnFocus.closest('[inert]')&&this.returnFocus.getClientRects().length&&getComputedStyle(this.returnFocus).visibility!=='hidden')this.returnFocus.focus();else{{var target=document.querySelector('.ws-shell-header summary');if(target)target.focus();}}}});}}
 async open(view){{this.returnFocus=document.activeElement;if(!this.dialog.open)this.dialog.showModal();this.main.replaceChildren(el('p',{{className:'notice','role':'status'}},'계정 정보를 확인하고 있습니다.'));try{{await loadSession();this.user.textContent=session.username+' · '+(session.role==='admin'?'관리자':'일반 사용자');this.tabs.replaceChildren();var choices=[['personal','개인 설정']];if(session.canManageUsers)choices.push(['users','사용자 관리']);choices.forEach(([key,label])=>{{var b=el('button',{{type:'button',className:'tab',role:'tab','aria-selected':String(view===key)}},label);b.addEventListener('click',()=>this.show(key));b.dataset.key=key;this.tabs.append(b);}});await this.show(view==='users'&&session.canManageUsers?'users':'personal');}}catch(error){{this.main.replaceChildren(el('p',{{className:'notice error',role:'alert'}},error.message));}}}}
 async show(view){{this.tabs.querySelectorAll('button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.key===view)));this.main.replaceChildren();if(view==='users'){{if(!session.canManageUsers){{this.main.append(el('p',{{role:'alert'}},'관리자 권한이 필요합니다.'));return;}}await this.users();}}else this.password();}}
 password(){{this.dialog.setAttribute('aria-label','개인 설정 · 비밀번호 변경');this.main.append(el('h3',{{}},'비밀번호 변경'));var form=el('form');var current=this.field(form,'현재 비밀번호','password','current-password');var next=this.field(form,'새 비밀번호','password','new-password');var confirm=this.field(form,'새 비밀번호 확인','password','new-password');next.minLength=session.passwordMinLength;confirm.minLength=session.passwordMinLength;var note=el('p',{{className:'muted'}},'새 비밀번호를 두 번 동일하게 입력해 주세요.');var status=el('p',{{className:'notice',role:'status'}});var actions=el('div',{{className:'actions'}});var cancel=el('button',{{type:'button'}},'취소');cancel.addEventListener('click',()=>this.dialog.close());var submit=el('button',{{type:'submit',className:'primary'}},'변경 사항 저장');actions.append(cancel,submit);form.append(note,status,actions);form.addEventListener('submit',async event=>{{event.preventDefault();if(next.value!==confirm.value){{status.className='notice error';status.setAttribute('role','alert');status.textContent='새 비밀번호가 일치하지 않습니다.';confirm.focus();return;}}submit.disabled=true;status.textContent='저장 중입니다.';try{{var result=await request('ui/password','POST',{{current_password:current.value,new_password:next.value,confirm_password:confirm.value}});form.reset();status.className='notice success';status.textContent='비밀번호가 변경되었습니다.';if(result.reauthenticate){{if(project==='seongnam')localStorage.removeItem('session_token');if(project==='bali'){{localStorage.removeItem('access_token');localStorage.removeItem('username');}}status.textContent='비밀번호가 변경되었습니다. 다시 로그인해 주세요.';var login=el('button',{{type:'button'}},'로그인 화면으로');login.addEventListener('click',()=>location.reload());form.append(login);submit.disabled=true;submit.dataset.saved='true';}}}}catch(error){{status.className='notice error';status.setAttribute('role','alert');status.textContent=error.message;}}finally{{if(!submit.dataset.saved)submit.disabled=false;}}}});this.main.append(form);if(session.supportsUsernameChange)this.username();current.focus();}}
 username(){{var details=el('details',{{className:'muted'}});details.append(el('summary',{{}},'아이디 변경'));var form=el('form');var current=this.field(form,'현재 비밀번호 (아이디 변경)','password','current-password');var name=this.field(form,'새 아이디','text','username');name.value=session.username;name.maxLength=128;var status=el('p',{{className:'notice',role:'status'}});var button=el('button',{{type:'submit'}},'아이디 변경 저장');form.append(status,button);form.addEventListener('submit',async event=>{{event.preventDefault();button.disabled=true;try{{await request('ui/username','POST',{{current_password:current.value,new_username:name.value}});form.reset();status.className='notice success';status.textContent='아이디가 변경되었습니다. 다시 로그인해 주세요.';var login=el('button',{{type:'button'}},'로그인 화면으로');login.addEventListener('click',()=>location.reload());form.append(login);}}catch(error){{status.className='notice error';status.setAttribute('role','alert');status.textContent=error.message;button.disabled=false;}}}});details.append(form);this.main.append(details);}}
 field(form,label,type,autocomplete){{var group=el('label',{{}},label);var input=el('input',{{type:type,required:true,autocomplete:autocomplete||'off'}});group.append(input);form.append(group);return input;}}
 async users(){{this.dialog.setAttribute('aria-label','사용자 관리 · 페이지 권한');this.main.append(el('h3',{{}},'사용자 관리'));var notice=el('p',{{className:'notice',role:'status'}},'사용자 목록을 불러오고 있습니다.');this.main.append(notice);try{{var users=await request('users');if(!Array.isArray(users))throw new Error('사용자 목록 응답이 올바르지 않습니다.');var wrap=el('div',{{className:'scroll'}});var table=el('table');var head=el('tr');['아이디','역할','페이지 권한',''].forEach(t=>head.append(el('th',{{}},t)));var thead=el('thead');thead.append(head);var body=el('tbody');users.forEach(user=>{{var row=el('tr');row.append(el('td',{{}},user.username),el('td',{{}},user.role==='admin'?'관리자':'일반 사용자'),el('td',{{}},user.role==='admin'?'모든 페이지':user.pages===null?'기존 권한':(user.pages||[]).map(x=>session.availablePages[x]||x).join(', ')||'개인 설정만'));var cell=el('td');var edit=el('button',{{type:'button'}},'수정');edit.addEventListener('click',()=>this.userForm(holder,user));cell.append(edit);row.append(cell);body.append(row);}});table.append(thead,body);wrap.append(table);var holder=el('section');notice.remove();this.main.append(wrap,holder);this.userForm(holder,null);}}catch(error){{notice.className='notice error';notice.setAttribute('role','alert');notice.textContent=error.message;}}}}
 userForm(holder,user){{holder.replaceChildren(el('h3',{{}},user?'계정·페이지 권한 수정':'신규 계정 추가'));var form=el('form');var name=this.field(form,'아이디','text','username');if(user){{name.value=user.username;name.disabled=true;}}var roleLabel=el('label',{{}},'역할');var role=el('select');[['viewer','일반 사용자'],['admin','관리자']].forEach(([value,label])=>role.append(el('option',{{value:value}},label)));role.value=user?user.role:'viewer';roleLabel.append(role);form.append(roleLabel);var password=this.field(form,user?'새 비밀번호 (변경할 때만)':'초기 비밀번호','password','new-password');var confirm=this.field(form,'비밀번호 확인','password','new-password');password.required=!user;confirm.required=!user;password.minLength=session.passwordMinLength;confirm.minLength=session.passwordMinLength;
 var grants=el('fieldset');grants.append(el('legend',{{}},'페이지 조회 권한'));Object.keys(session.availablePages).filter(x=>x!=='users').forEach(key=>{{var label=el('label',{{className:'check'}});var checkbox=el('input',{{type:'checkbox',value:key}});checkbox.checked=key==='settings'||(user?(user.pages===null||((user.pages||[]).includes(key))):false);checkbox.disabled=key==='settings';label.append(checkbox,document.createTextNode(session.availablePages[key]));grants.append(label);}});role.addEventListener('change',()=>{{grants.querySelectorAll('input').forEach(input=>{{input.disabled=role.value==='admin'||input.value==='settings';if(role.value==='admin')input.checked=true;}});}});if(role.value==='admin')role.dispatchEvent(new Event('change'));form.append(grants);var hint=el('p',{{className:'muted'}},'관리자는 모든 페이지와 사용자 관리에 접근합니다. 일반 사용자는 선택한 페이지를 조회할 수 있습니다.');var status=el('p',{{className:'notice',role:'status'}});var actions=el('div',{{className:'actions'}});if(user){{var reset=el('button',{{type:'button'}},'신규 계정 추가로 돌아가기');reset.addEventListener('click',()=>this.userForm(holder,null));actions.append(reset);}}var save=el('button',{{type:'submit',className:'primary'}},user?'변경 사항 저장':'계정 추가');actions.append(save);form.append(hint,status,actions);form.addEventListener('submit',async event=>{{event.preventDefault();if(password.value!==confirm.value){{status.className='notice error';status.textContent='비밀번호가 일치하지 않습니다.';confirm.focus();return;}}save.disabled=true;var pages=Array.from(grants.querySelectorAll('input:checked')).map(x=>x.value);var body={{role:role.value,pages:pages}};if(user){{body.scope_type=user.scope_type;body.scope_ref=user.scope_ref;if(password.value)body.password=password.value;}}else{{body.username=name.value.trim();body.password=password.value;body.scope_type='cluster';body.scope_ref='';}}try{{await request('users'+(user?'/'+encodeURIComponent(user.username):''),user?'PATCH':'POST',body);form.reset();await this.show('users');var success=el('p',{{className:'notice success',role:'status'}},user?'계정 권한이 저장되었습니다.':'새 계정이 추가되었습니다.');this.main.prepend(success);}}catch(error){{status.className='notice error';status.setAttribute('role','alert');status.textContent=error.message;save.disabled=false;}}}});holder.append(form);}}
}}
if(!customElements.get('ws-user-settings'))customElements.define('ws-user-settings',SettingsPanel);
var api={{supported:true,canManageUsers:false,logout:async function(){{await request('logout','POST',{{}});location.assign(prefix+'login');}},open:async function(view){{if(project==='samsung'&&!/\\/settings\\/?$/.test(location.pathname)){{location.assign(prefix+'settings?ws-panel='+encodeURIComponent(view||'personal'));return;}}if(!panel){{panel=document.createElement('ws-user-settings');document.body.append(panel);}}await panel.open(view||'personal');}}}};window.WSSettings=api;
document.addEventListener('ws:shell-ready',function(event){{if(event.detail.project!==project)return;if(!event.detail.ready){{generation++;session=null;api.canManageUsers=false;delete api.pages;delete api.role;if(panel&&panel.dialog.open)panel.dialog.close();}}else loadSession().catch(function(){{}});}});
if(document.body&&document.body.classList.contains('ws-shell-ready'))loadSession().catch(function(){{}});
}}());
</script>
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
        "<em>실제 Pod 간 네트워크 연결</em>입니다(L3/L4 수준). 위 다이어그램에서 "
        "<strong>화살표</strong>는 연결 방향(source → destination)이며 <strong>전기가 흐르듯</strong> "
        "애니메이션됩니다 — 연결이 잦은 경로일수록 <strong>더 빠르게</strong> 흐릅니다(두께는 모두 동일). "
        "화살표 가운데 <strong>숫자</strong>가 실제 관측된 연결 수이고, <strong>색</strong>은 "
        "Cilium의 정책 <strong>판정(verdict)</strong>입니다.</p>"
        "<p>다이어그램은 <strong>멀티홉 흐름 그래프</strong>입니다 — 각 리소스를 한 번만 그리고 "
        "<strong>위에서 아래로 갈수록 다음 단계(hop)</strong>로 이어집니다. 그래서 "
        "<code>A→B</code>와 <code>B→C</code>가 같이 관측되면 <code>A→B→C</code> 사슬로 연결돼 "
        "보입니다(1:1 쌍은 인접한 두 행). 각 <strong>칩</strong>은 흐름에 참여한 리소스이며, "
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
    """칩 폭 추정(글자수 기반, SVG는 실측정이 없으므로). 앞뒤 여백 포함, 120~320px로 클램프(크게)."""
    chars = max(len(label), len(sub))
    return max(120.0, min(320.0, chars * 7.8 + 26))


def _flow_graph_svg(nodes: list, edges: list, limit: int = 10, *, scope: str = "app",
                    namespace: Optional[str] = None) -> str:
    """상위 연결을 계층형(멀티홉) 노드-링크 그래프로 그린다 — '다음 단계로 이어지는' 흐름 시각화.

    각 리소스를 한 번만 그리고, 진입점에서의 홉 수(layer)에 따라 **위→아래 행**으로 쌓는다
    (한 행의 노드는 가로로 편다). 그래서 A→B, B→C 가 있으면 A→B→C 사슬로 아래로 이어져
    보인다(1:1 쌍은 인접 두 행). 세로를 주 축으로 두어 깊은 사슬도 가로 스크롤 없이 아래로만
    길어진다. 각 노드는 리소스 칩(라벨=ns/pod, 아래줄=워크로드/성격)이며 클릭하면 그 리소스에
    focus가 걸린다. 화살표 두께는 균일, 색 = 대표 verdict.

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

    # --- 좌표 배치(세로 흐름). 계층(hop)을 위→아래로 쌓고, 한 계층의 노드는 가로로 편다.
    # 한 계층의 노드가 많아 가로가 컨테이너(TARGET_W)를 넘으면 **여러 줄로 접어** 아래로 쌓는다
    # → 가로 스크롤 없이 아래로만 길어지고, 축소되지 않아 크기도 유지된다. 칩/폰트는 확대값 그대로.
    chip_h, lay_gap, node_hgap, intra_gap, pad_top, pad_x = 46, 96, 30, 18, 50, 22
    TARGET_W = 980.0   # 한 줄 최대 가로 폭(≈컨테이너). 넘으면 다음 줄로 접는다.
    node_cw = {
        n["label"]: _chip_width(_shorten(n["label"], 26), _shorten(_sub(n), 26))
        for L in columns for n in columns[L]
    }

    # 1) 각 계층을 TARGET_W 이내의 여러 '줄'로 접는다(줄바꿈 패킹). 가장 넓은 줄로 전체 폭 결정.
    layers_rows: dict = {}
    max_row_w = 90.0
    for L in range(max_layer + 1):
        rows_l, cur, cur_w = [], [], 0.0
        for n in columns.get(L, []):
            w = node_cw[n["label"]]
            add = w if not cur else node_hgap + w
            if cur and cur_w + add > TARGET_W:
                rows_l.append((cur, cur_w)); cur, cur_w = [], 0.0
                add = w
            cur.append(n); cur_w += add
        if cur:
            rows_l.append((cur, cur_w))
        layers_rows[L] = rows_l or [([], 0.0)]
        for _r, rw in layers_rows[L]:
            max_row_w = max(max_row_w, rw)
    content_width = max_row_w + 2 * pad_x

    # 2) 좌표 배정: 계층을 위→아래로, 계층 안의 여러 줄도 위→아래로. 각 줄은 가운데 정렬.
    node_x, node_y = {}, {}
    y = float(pad_top)
    for L in range(max_layer + 1):
        for row, rw in layers_rows[L]:
            x = (content_width - rw) / 2.0
            for n in row:
                node_x[n["label"]] = x
                node_y[n["label"]] = y
                x += node_cw[n["label"]] + node_hgap
            y += chip_h + intra_gap        # 같은 계층 다음 줄
        y += lay_gap - intra_gap           # 다음 계층까지 간격
    content_bottom = (max(node_y.values()) + chip_h) if node_y else pad_top + chip_h
    content_right = content_width  # 역방향 우회선이 더 오른쪽으로 나가면 아래 루프에서 확장

    # --- 포트 분산: 한 노드에 여러 간선이 붙을 때 연결 지점을 칩 '가로 폭'에 고루 나눠 겹침을
    # 막는다. 나가는/들어오는 간선을 상대편 x로 정렬해 좌→우로 포트를 배정하면 교차도 줄어든다.
    out_e: dict = defaultdict(list)
    in_e: dict = defaultdict(list)
    for i, e in enumerate(edges):
        out_e[e["src"]].append(i)
        in_e[e["dst"]].append(i)
    for lst in out_e.values():
        lst.sort(key=lambda i: node_x[edges[i]["dst"]])
    for lst in in_e.values():
        lst.sort(key=lambda i: node_x[edges[i]["src"]])
    src_px, dst_px = {}, {}
    for lbl, lst in out_e.items():
        k = len(lst)
        for j, i in enumerate(lst):
            src_px[i] = node_x[lbl] + node_cw[lbl] * (j + 1) / (k + 1)
    for lbl, lst in in_e.items():
        k = len(lst)
        for j, i in enumerate(lst):
            dst_px[i] = node_x[lbl] + node_cw[lbl] * (j + 1) / (k + 1)

    max_count = max((e["count"] for e in edges), default=1) or 1
    min_count = min((e["count"] for e in edges), default=1)
    span = (max_count - min_count) or 1
    used_verdicts = {e.get("verdict") or "FORWARDED" for e in edges}
    markers = "".join(
        f'<marker id="arw-{html.escape(v)}" markerWidth="10" markerHeight="10" refX="8" refY="4" '
        f'orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="{_VERDICT_COLOR.get(v, "#4b5563")}"/></marker>'
        for v in used_verdicts
    )

    EDGE_W = 3.2                    # 모든 화살표 두께 균일(연결 수와 무관, 크게)
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
        ARROW_GAP = 11
        # 계층이 여러 줄로 접힐 수 있으므로 계층 기준이 아니라 노드별 실제 y를 쓴다.
        sy_top, dy_top = node_y[e["src"]], node_y[e["dst"]]
        if e["src"] == e["dst"]:
            # 자기 자신으로의 흐름: 칩 아래에 작은 고리로 그린다(드묾).
            x = node_x[e["src"]] + node_cw[e["src"]] / 2
            y = sy_top + chip_h
            content_bottom = max(content_bottom, y + 46)  # 고리가 아래로 잘리지 않게 높이 확장
            p0, c1, c2, p3 = (x, y), (x - 28, y + 44), (x + 28, y + 44), (x, y + 3)
        elif dy_top > sy_top:  # 정방향(위→아래): 세로 중간점을 제어점으로 하는 S커브.
            sx, sy = src_px[i], sy_top + chip_h                  # 출발 = 칩 아래 변
            ex, ey = dst_px[i], dy_top - ARROW_GAP               # 도착 = 다음 칩 위 변
            cy = (sy + ey) / 2
            p0, c1, c2, p3 = (sx, sy), (sx, cy), (ex, cy), (ex, ey)
        else:  # 역방향/동일 높이(순환): 오른쪽으로 우회(위→아래 흐름을 방해하지 않게).
            sx, sy = src_px[i], sy_top                           # 출발 = 칩 위 변
            ex, ey = dst_px[i], dy_top + chip_h + ARROW_GAP      # 도착 = 대상 칩 아래 변
            bow = max(sx, ex, content_width - pad_x) + 48
            content_right = max(content_right, bow + 12)         # 우회선이 잘리지 않게 폭 확장
            p0, c1, c2, p3 = (sx, sy), (bow, sy), (bow, ey), (ex, ey)
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
            f'<text x="{mx:.0f}" y="{my - 4:.0f}" text-anchor="middle" font-size="12" '
            f'font-weight="700" fill="{color}" '
            f'style="paint-order:stroke;stroke:var(--card-bg);stroke-width:3px">{e["count"]}</text>'
        )

    node_svg = []
    for lbl, n in by_label.items():
        x = node_x[lbl]
        y = node_y[lbl]
        cw = node_cw[lbl]
        stroke = _KIND_STROKE.get(n["kind"], "#9ca3af")
        fill = _KIND_FILL.get(n["kind"], "none")
        dash = ' stroke-dasharray="3 2"' if n["kind"] == "reserved" else ""
        sub = _shorten(_sub(n), 26)
        label_disp = _shorten(lbl, 26)
        url = _flows_url(scope=scope, namespace=namespace, focus=lbl)
        sub_svg = (
            f'<text x="{x + 13:.0f}" y="{y + 33:.0f}" font-size="11" fill="currentColor" '
            f'opacity="0.6">{html.escape(sub)}</text>' if sub else ""
        )
        node_svg.append(
            f'<a href="{html.escape(url)}"><title>{html.escape(lbl)} 연결만 보기</title>'
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{cw:.0f}" height="{chip_h}" rx="9" '
            f'fill="{fill}" stroke="{stroke}"{dash} stroke-width="1"/>'
            f'<text x="{x + 13:.0f}" y="{y + 20:.0f}" font-size="14" fill="currentColor">'
            f'{html.escape(label_disp)}</text>{sub_svg}</a>'
        )

    height = content_bottom + 16
    width = content_right + 10

    caption = (
        f'<text x="{pad_x}" y="34" font-size="13" fill="currentColor" opacity="0.6">'
        f'↓ 아래로 갈수록 다음 단계(hop) · 칩=리소스, 아래줄=워크로드</text>'
    )
    # 세로 흐름이라 폭은 컨테이너에 맞추고(가로 스크롤 없음) 높이는 자연히 늘어난다. width="100%"
    # + max-width로 자연 폭까지만 커지므로, 폭이 화면보다 좁으면 큰 크기 그대로, 넓으면 축소만 된다.
    return (
        f'<div class="diagram"><svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
        f'style="max-width:{width:.0f}px;height:auto;display:block" role="img" '
        f'aria-label="멀티홉 Pod 트래픽 흐름 그래프(세로)">'
        f"<defs>{markers}</defs>{caption}{''.join(edge_svg)}{''.join(node_svg)}</svg></div>"
    )


def _scope_toggle_html(summary: FlowSummary) -> str:
    """애플리케이션 전용/전체 트래픽 전환 = 선택 버튼. 현재 namespace/focus 필터는 유지한다."""
    return _seg_control_html("보기", [
        ("내 애플리케이션 트래픽",
         _flows_url(scope="app", namespace=summary.namespace, focus=summary.focus),
         summary.scope == "app"),
        ("전체(인프라 포함)",
         _flows_url(scope="all", namespace=summary.namespace, focus=summary.focus),
         summary.scope == "all"),
    ])


def _namespace_filter_html(summary: FlowSummary) -> str:
    """현재 scope에 등장하는 네임스페이스 선택 링크. '전체'는 ns 해제, 개별 ns는 focus 초기화.

    scope만 적용한 목록(summary.namespaces)을 쓰므로, 어떤 필터가 걸려 있어도 다른
    네임스페이스로 곧장 전환할 수 있다. 다른 ns로 옮기면 이전 리소스 focus는 대개 무의미하므로
    개별 ns 링크는 focus를 넘기지 않아(초기화) 그 네임스페이스 전체를 보여준다.
    """
    if not summary.namespaces:
        return ""
    # '전체'(ns 해제)는 focus는 유지 — "이 리소스가 낀 흐름을 모든 ns에 걸쳐" 보고 싶을 수 있으므로.
    options = [
        ("전체", _flows_url(scope=summary.scope, focus=summary.focus), not summary.namespace)
    ]
    for ns in summary.namespaces:
        name = ns["name"]
        inner = f'{html.escape(name)} <span class="segcount">({ns["count"]})</span>'
        options.append((inner, _flows_url(scope=summary.scope, namespace=name),
                        name == summary.namespace))
    return _seg_control_html("네임스페이스", options)


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
        table = controls + (
            f'<div class="error-row" style="padding:1rem">⚠ Hubble 조회 실패: '
            f'{html.escape(summary.fetch_error)}</div>'
        ) + help_panel
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
        table = controls + empty + help_panel
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
            f"(전체 {len(summary.top_pairs)}개는 아래 표 참고) · 위→아래 = 다음 단계(hop) · "
            "화살표는 전기 흐르듯 애니메이션(연결 잦을수록 빠름), 가운데 숫자 = 실제 연결 수 · "
            "선 위 마우스 = 상세, 칩 클릭 = 그 리소스 연결만 보기.</p>"
        )
        rows = "\n".join(
            _flow_pair_row_html(p, scope=summary.scope, namespace=summary.namespace)
            for p in summary.top_pairs
        )
        table = controls + diagram + diagram_cap + help_panel + f"""<div style="margin-bottom:1rem">{badges}</div>
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
<html lang="ko" class="ws-app ws-project-samsung">
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
<script data-ws-auth>/* 선택적 테마 동작. 업무 데이터/계정 저장소와 별도 키를 사용한다. */
(function () {{
  'use strict';
  var key = 'web-standard:theme';
  var choices = ['system', 'light', 'dark'];
  function apply(value, persist) {{
    if (choices.indexOf(value) < 0) value = 'system';
    var root = document.documentElement;
    root.classList.toggle('ws-theme-light', value === 'light');
    root.classList.toggle('ws-theme-dark', value === 'dark');
    root.setAttribute('data-ws-theme', value);
    if (persist) {{ try {{ localStorage.setItem(key, value); }} catch (_) {{}} }}
    document.querySelectorAll('[data-ws-theme-select]').forEach(function (el) {{ el.value = value; }});
  }}
  var initial = 'system';
  try {{ initial = localStorage.getItem(key) || initial; }} catch (_) {{}}
  apply(initial, false);
  document.addEventListener('DOMContentLoaded', function () {{ apply(document.documentElement.getAttribute('data-ws-theme') || initial, false); }}, {{ once: true }});
  document.addEventListener('change', function (event) {{
    if (event.target.matches('[data-ws-theme-select]')) apply(event.target.value, true);
  }});
  window.addEventListener('storage', function (event) {{ if (event.key === key) apply(event.newValue, false); }});
}}());

/* 인증 요청/토큰/쿠키를 다루지 않는 공통 표현 계층. 기존 노드와 이벤트를 보존한다. */
(function(){{
'use strict';
var project=(document.documentElement.className.match(/ws-project-([\\w-]+)/)||[])[1];
var names={{bali:'메모 앱',canggu:'Kubernetes 자동 운영',pangyo:'MemoNest',samsung:'TrafficPolicy',seongnam:'OS Monitor',seoul:'클러스터 점검'}};
if(project==='pangyo'||!names[project])return;
var pendingPassword=project==='samsung'&&/\\/settings\\/?$/.test(location.pathname);
var passwordDialog=null;
function element(tag,cls,text){{var e=document.createElement(tag);if(cls)e.className=cls;if(text)e.textContent=text;return e;}}
function heading(title,subtitle,mark){{var h=element('div','ws-auth-heading');if(mark){{var icon=element('div','ws-auth-mark');icon.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="2" fill="currentColor" stroke="none"/><rect x="14" y="3" width="7" height="7" rx="2" fill="currentColor" stroke="none"/><rect x="3" y="14" width="7" height="7" rx="2" fill="currentColor" stroke="none"/><rect x="14" y="14" width="7" height="7" rx="2" fill="currentColor" stroke="none" opacity=".55"/></svg>';h.append(icon);}}h.append(element('h2','',title),element('p','',subtitle));return h;}}
function hide(e){{if(e)e.setAttribute('data-ws-auth-hide','');}}
function original(e){{if(e)e.setAttribute('data-ws-original-action','');}}
function available(e){{return !!e&&e.isConnected&&!e.closest('[hidden]');}}
function login(card,screen,form){{if(!card||card.dataset.wsAuthReady)return;card.dataset.wsAuthReady='1';card.classList.add('ws-auth-card');if(screen)screen.classList.add('ws-auth-screen');
 if(project==='bali'){{hide(card.firstElementChild);var buttons=card.querySelectorAll('button');buttons.forEach(function(b){{if(/로그인/.test(b.textContent))b.classList.add('ws-auth-submit');}});hide(card.lastElementChild&&card.lastElementChild.tagName==='P'?card.lastElementChild:null);}}
 else {{Array.from(card.children).forEach(function(c){{if(c.matches('h1,h2,h3,.sub,.muted'))hide(c);}});}}
 card.prepend(heading('로그인',names[project]+' 계정으로 로그인하세요.',true));
 var fields=card.querySelectorAll('input:not([type=hidden]):not([type=checkbox])');fields.forEach(function(input,index){{if(!input.id)input.id='ws-login-'+project+'-'+index;if(!input.closest('label')&&!card.querySelector('label[for="'+input.id+'"]')){{var label=element('label','ws-auth-label',input.type==='password'?'비밀번호':'아이디');label.htmlFor=input.id;input.before(label);}}if(input.type==='password')input.autocomplete='current-password';else if(!input.autocomplete)input.autocomplete='username';}});
 if(form)form.setAttribute('aria-label','로그인');
}}
function stylePassword(card,form,cancel){{card.classList.add('ws-password-card');var old=card.querySelector(':scope > h2,:scope > h3');hide(old);card.prepend(heading('비밀번호 변경','현재 비밀번호를 확인한 뒤 새 비밀번호를 저장합니다.',false));
 var action=form?form.querySelector('button[type=submit]'):card.querySelector('.edit-actions .primary');if(!action&&form)action=form.querySelector('button.btn');
 var actions=element('div','ws-password-actions');if(form){{if(cancel)actions.append(cancel);if(action)actions.append(action);form.append(actions);}}else{{var row=card.querySelector('.edit-actions');if(row)row.classList.add('ws-password-actions');}}
 if(action)action.textContent='변경 사항 저장';
 var inputs=Array.from((form||card).querySelectorAll('input[type=password]'));
 if(inputs.length===2){{var label=element('label','ws-auth-field','새 비밀번호 확인');var confirm=element('input');confirm.type='password';confirm.autocomplete='new-password';confirm.required=true;confirm.dataset.wsConfirm='1';label.append(confirm);if(form)form.insertBefore(label,actions);else{{var grid=card.querySelector('.edit-grid');grid.append(label);}}var status=element('p','ws-auth-feedback');status.setAttribute('role','alert');label.after(status);
 var validate=function(event){{if(confirm.value!==inputs[1].value||!confirm.value){{event.preventDefault();event.stopImmediatePropagation();status.textContent='새 비밀번호가 일치하지 않습니다.';confirm.focus();}}else status.textContent='';}};if(action)action.addEventListener('click',validate,true);if(form)form.addEventListener('submit',validate,true);}}
 if(form){{var user=form.querySelector('input[name=new_username]');if(user){{var field=user.closest('.field');if(field){{var advanced=element('details','ws-auth-advanced');advanced.append(element('summary','','아이디 변경 (선택)'),field);form.insertBefore(advanced,actions);}}}}}}card.setAttribute('aria-label','비밀번호 변경');
}}
function modalize(form){{if(!form||form.dataset.wsPasswordReady)return;form.dataset.wsPasswordReady='1';var parent=form.parentElement;parent.classList.add('ws-security-panel');var dialog=element('dialog','ws-password-card');var open=element('button','ws-security-open','비밀번호 변경');open.type='button';var cancel=element('button','','취소');cancel.type='button';cancel.addEventListener('click',function(){{dialog.close();}});form.before(open,dialog);dialog.append(form);stylePassword(dialog,form,cancel);open.addEventListener('click',function(){{dialog.showModal();}});passwordDialog=dialog;if(pendingPassword){{pendingPassword=false;dialog.showModal();}}}}
function menu(host,logout,password,account){{if(!host||host.querySelector(':scope > .ws-account-menu'))return;var detail=element('details','ws-account-menu');var summary=element('summary','','계정');var panel=element('div','ws-account-dropdown');detail.append(summary,panel);
 function action(text,callback,cls){{var b=element('button',cls,text);b.type='button';b.addEventListener('click',function(){{detail.open=false;callback();}});panel.append(b);}}
 if(account){{action('계정 관리',function(){{account.click();}});original(account);}}
 if(password){{action('비밀번호 변경',function(){{if(typeof password==='function')password();else password.click();}});if(typeof password!=='function')original(password);}}
 if(logout){{action('로그아웃',function(){{logout.click();}},'ws-signout');original(logout);}}
 host.append(detail);
}}
function requestPassword(){{pendingPassword=true;var settings=document.querySelector('[data-tab=settings],[data-hash="#settings"]');if(settings)settings.click();setTimeout(schedule,0);}}
function enhance(){{
 if(project==='bali'){{var input=document.getElementById('username');if(input){{var card=input.closest('div[style]');while(card&&(!card.querySelector('button')||!card.querySelector('input[type=password]')))card=card.parentElement;if(card){{var outer=card.parentElement;if(outer&&outer.querySelectorAll('input').length===2){{login(outer,outer.parentElement,null);}}}}}}var logout=Array.from(document.querySelectorAll('header button')).find(function(b){{return b.textContent==='로그아웃'&&!b.closest('.ws-account-menu');}});if(logout)menu(logout.parentElement,logout,null,null);}}
 if(project==='canggu'){{var form=document.querySelector('form.login');if(form)login(form,form.parentElement,form);var pw=Array.from(document.querySelectorAll('header > button')).find(function(b){{return b.textContent==='비밀번호 변경';}});var lo=Array.from(document.querySelectorAll('header > button')).find(function(b){{return b.textContent==='로그아웃';}});if(lo)menu(lo.parentElement,lo,pw,null);var modal=document.querySelector('.modal-backdrop .modal');if(modal&&modal.querySelector('input[type=password]')&&!modal.dataset.wsPasswordReady){{modal.dataset.wsPasswordReady='1';stylePassword(modal,null,null);modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');}}}}
 if(project==='seoul'||project==='seongnam'){{var overlay=document.getElementById('login-overlay');var form=document.getElementById('login-form');if(form)login(project==='seoul'?form:form.parentElement,overlay,form);var lo=document.getElementById('logout-btn');if(available(lo))menu(lo.parentElement,lo,requestPassword,null);modalize(document.getElementById(project==='seoul'?'password-form':'pw-form'));}}
 if(project==='samsung'){{var form=document.querySelector('form[action$="/login"]');if(form){{login(form.parentElement,form.closest('main'),form);document.body.classList.add('ws-auth-page');}}var settings=document.querySelector('form[action$="/settings"]');if(settings){{if(window.WSSettings&&window.WSSettings.supported){{if(!settings.dataset.wsSdkReady){{settings.dataset.wsSdkReady='1';original(settings);var open=element('button','ws-security-open','개인 설정 열기');open.type='button';open.addEventListener('click',function(){{window.WSSettings.open('personal');}});settings.before(open);}}if(pendingPassword){{pendingPassword=false;setTimeout(function(){{var view=new URLSearchParams(location.search).get('ws-panel');window.WSSettings.open(view==='users'?'users':'personal');}},0);}}}}else modalize(settings);}}var logout=document.querySelector('form[action$="/logout"] button');var link=document.querySelector('.userbox a[href$="/settings"]');if(logout)menu(logout.closest('.userbox'),logout,link,null);}}
 if(project==='pangyo'){{var btn=document.getElementById('account-btn');if(btn)btn.classList.add('ws-account-trigger');var form=document.getElementById('account-login-form');if(form){{var dialog=form.closest('dialog');if(dialog){{login(dialog,null,form);dialog.classList.add('ws-auth-card');}}}}else{{var dialog=document.getElementById('account-dialog');if(dialog&&dialog.dataset.wsAuthReady){{dialog.classList.remove('ws-auth-card');delete dialog.dataset.wsAuthReady;var h=dialog.querySelector(':scope > .ws-auth-heading');if(h)h.remove();}}}}}}
 if(pendingPassword&&passwordDialog&&passwordDialog.isConnected&&passwordDialog.parentElement.getClientRects().length){{pendingPassword=false;if(!passwordDialog.open)passwordDialog.showModal();}}
}}
var scheduled=false;function schedule(){{if(scheduled)return;scheduled=true;requestAnimationFrame(function(){{scheduled=false;enhance();}});}}
document.addEventListener('keydown',function(e){{var modal=document.querySelector('.modal-backdrop .ws-password-card');if(modal&&e.key==='Tab'){{var nodes=Array.from(modal.querySelectorAll('input,button')).filter(function(n){{return !n.disabled&&n.getClientRects().length;}});var first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){{e.preventDefault();last.focus();}}else if(!e.shiftKey&&document.activeElement===last){{e.preventDefault();first.focus();}}}}if(modal&&e.key==='Enter'&&e.target.tagName==='INPUT'){{e.preventDefault();var submit=modal.querySelector('button.primary');if(submit)submit.click();}}if(e.key==='Escape'){{document.querySelectorAll('.ws-account-menu[open]').forEach(function(d){{d.open=false;}});var card=document.querySelector('.modal-backdrop .ws-password-card');if(card){{var cancel=card.querySelector('.edit-actions button');if(cancel)cancel.click();}}}}}});
document.addEventListener('click',function(e){{document.querySelectorAll('.ws-account-menu[open]').forEach(function(d){{if(!d.contains(e.target))d.open=false;}});}});
function start(){{enhance();new MutationObserver(schedule).observe(document.body,{{childList:true,subtree:true,attributes:true,attributeFilter:['hidden']}});}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{{once:true}});else start();
}}());

/* 공통 고정 프레임. 기존 React/Vue 소유 노드와 업무 이벤트를 재배치하지 않는다. */
(function(){{
'use strict';
var root=document.documentElement;
var project=(root.className.match(/ws-project-([\\w-]+)/)||[])[1];
var titles={{bali:'메모',canggu:'클러스터 운영',gimpo:'백업·복구',samsung:'트래픽 운영',seongnam:'OS 모니터링',seoul:'클러스터 점검',tashkent:'모델 팩토리'}};
if(project==='pangyo'||!titles[project])return;
var header,sidebar,nav,footer,account,scrim,toggle,items=[],actions=[],signature='',accountSignature='',queued=false,activePage='overview';
var mobile=window.matchMedia('(max-width:760px)');
function el(tag,cls,text){{var n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n;}}
function text(n,value){{if(n.textContent!==value)n.textContent=value;}}
function hide(n){{if(n&&!n.hasAttribute('data-ws-shell-original'))n.setAttribute('data-ws-shell-original','');}}
function closeMenu(focus){{document.body.classList.remove('ws-nav-open');toggle.setAttribute('aria-expanded','false');sidebar.inert=mobile.matches;if(focus)toggle.focus();}}
function openMenu(){{document.body.classList.add('ws-nav-open');toggle.setAttribute('aria-expanded','true');sidebar.inert=false;var first=nav.querySelector('button');if(first)first.focus();}}
function setup(){{
 header=el('header','ws-shell-header');header.setAttribute('aria-label','공통 상단 메뉴');
 toggle=el('button','ws-shell-menu-toggle','☰');toggle.type='button';toggle.setAttribute('aria-label','서비스 메뉴');toggle.setAttribute('aria-controls','ws-shell-sidebar');toggle.setAttribute('aria-expanded','false');toggle.addEventListener('click',function(){{document.body.classList.contains('ws-nav-open')?closeMenu(true):openMenu();}});
 var brand=el('a','ws-shell-brand');brand.href=location.pathname;brand.setAttribute('aria-label',titles[project]+' 홈');var mark=el('span','ws-ci');mark.setAttribute('aria-hidden','true');mark.innerHTML='<i></i><i></i><i></i><i></i>';brand.append(mark,el('span','ws-shell-title',titles[project]));
 var theme=el('select','ws-shell-theme');theme.setAttribute('aria-label','테마');theme.setAttribute('data-ws-theme-select','');[['system','시스템'],['light','Light'],['dark','Dark']].forEach(function(pair){{var option=el('option','',pair[1]);option.value=pair[0];theme.append(option);}});theme.value=root.getAttribute('data-ws-theme')||'system';account=el('div','ws-shell-header-end');header.append(toggle,brand,el('span','ws-shell-version','WEB STANDARD'),theme,account);
 sidebar=el('aside','ws-shell-sidebar');sidebar.id='ws-shell-sidebar';sidebar.setAttribute('aria-label','서비스 탐색');nav=el('nav','ws-shell-nav');nav.setAttribute('aria-label','주 메뉴');footer=el('div','ws-shell-nav-bottom');footer.append(el('p','','공통 디자인 · 개인 설정'));sidebar.append(el('p','ws-nav-caption','WORKSPACE'),nav,footer);
 scrim=el('button','ws-shell-scrim');scrim.type='button';scrim.setAttribute('aria-label','서비스 메뉴 닫기');scrim.tabIndex=-1;scrim.addEventListener('click',function(){{closeMenu(true);}});document.body.prepend(header,sidebar,scrim);sidebar.inert=mobile.matches;
 mobile.addEventListener('change',function(){{closeMenu(false);}});
}}
function visible(n){{return n&&n.isConnected&&!n.closest('[hidden]')&&n.getClientRects().length>0;}}
function activate(node){{if(node&&node.isConnected){{node.click();closeMenu(false);}}}}
function collect(){{var list=[];
 function add(label,node,key){{if(!label||!node||node.hidden||node.closest('[hidden]'))return;list.push({{label:label,key:key||label,run:function(){{activate(node);}}}});}}
 if(project==='seoul'||project==='seongnam'){{
  document.querySelectorAll('#tabs button').forEach(function(n){{add(n.textContent.trim(),n,n.dataset.hash||n.dataset.tab);}});hide(document.querySelector('body > .topbar'));
 }}else if(project==='gimpo'){{
  document.querySelectorAll('.el-aside .el-menu-item').forEach(function(n){{add(n.textContent.trim(),n,n.dataset.wsPage);}});hide(document.querySelector('.el-aside'));hide(document.querySelector('.el-header'));
 }}else if(project==='samsung'){{
  var sdk=window.WSSettings;var grants=sdk&&sdk.supported?(sdk.pages||[]):null;document.querySelectorAll('body > .topbar .tabs > a').forEach(function(n){{var href=n.getAttribute('href');var page=/\\/policies(?:\\?|$)/.test(href)?'policies':'flows';if(grants===null||grants.includes(page))add(n.textContent.trim(),n,href);}});hide(document.querySelector('body > .topbar'));
 }}else if(project==='tashkent'){{
  var sdk=window.WSSettings;var grants=sdk&&sdk.supported?(sdk.pages||[]):null;document.querySelectorAll('body > header:not(.ws-shell-header) nav > a').forEach(function(n){{var href=n.getAttribute('href');var page=href==='/products/new'?'new':href==='/presets'?'presets':href==='/debug'||href==='/dbcheck'?'debug':'products';if(grants===null||grants.includes(page))add(n.textContent.trim(),n,href);}});hide(document.querySelector('body > header:not(.ws-shell-header)'));
 }}else if(project==='canggu'){{
  var sdk=window.WSSettings||{{}};var grants=Array.isArray(sdk.pages)?sdk.pages:null;
  var labels={{overview:'개요',resources:'리소스',operations:'운영',activity:'활동',backup:'백업·복구',access:'접근 발급'}};
  var categories=function(title){{if(/웹 사용자/.test(title))return 'users';if(/백업|Velero/.test(title))return 'backup';if(/접근 발급|kubeconfig/.test(title))return 'access';if(/피드|이벤트|감사|이력/.test(title))return 'activity';if(/이슈|Operating|규칙/.test(title))return 'operations';return 'resources';}};
  var allowed=function(page){{return grants===null||grants.includes(page);}};
  if(!allowed(activePage))activePage=Object.keys(labels).find(allowed)||'settings';
  Object.keys(labels).filter(allowed).forEach(function(page){{list.push({{label:labels[page],key:page,run:function(){{activePage=page;schedule();window.scrollTo(0,0);closeMenu(false);}}}});}});
  document.querySelectorAll('.app .panel').forEach(function(panel){{var heading=panel.querySelector('h2');if(!heading)return;var page=categories(heading.textContent);var hidden=!allowed(page)||page!==activePage;if(panel.hidden!==hidden)panel.hidden=hidden;}});
  var overview=document.querySelector('.app .overview');if(overview){{var hidden=activePage!=='overview'||!allowed('overview');if(overview.hidden!==hidden)overview.hidden=hidden;}}

  hide(document.querySelector('.app > header .ws-account-menu'));
 }}else if(project==='bali'){{
  var sdk=window.WSSettings;var granted=sdk&&sdk.supported?(sdk.pages||[]):['notes'];if(granted.includes('notes'))list.push({{label:'메모',key:'notes',run:function(){{window.scrollTo(0,0);closeMenu(false);}}}});hide(document.querySelector('#root header'));
 }}
 return list;
}}
function accountActions(){{var list=Array.from(document.querySelectorAll('.ws-account-dropdown button')).filter(function(n){{return !header.contains(n);}}).map(function(n){{return {{label:n.textContent.trim(),run:function(){{activate(n);}}}};}});if(list.length&&window.WSSettings&&window.WSSettings.supported&&!list.some(function(a){{return a.label==='비밀번호 변경';}}))list.unshift({{label:'비밀번호 변경',run:function(){{window.WSSettings.open('personal');}}}});if(!list.length&&window.WSSettings&&window.WSSettings.supported&&(project==='tashkent'||project==='gimpo'))list=[{{label:'비밀번호 변경',run:function(){{window.WSSettings.open('personal');}}}},{{label:'로그아웃',run:function(){{window.WSSettings.logout();}}}}];return list;}}
function refresh(){{
 if(!header)setup();
 var signIn=Array.from(document.querySelectorAll('.ws-auth-card')).some(visible);
 var ready=!signIn&&(project==='gimpo'||project==='tashkent'||project==='samsung'||(project==='bali'&&!!document.querySelector('#root header'))||!!document.querySelector('.app > header,#tabs:not([hidden])'));
 if(document.body.classList.contains('ws-shell-ready')!==ready){{document.body.classList.toggle('ws-shell-ready',ready);document.dispatchEvent(new CustomEvent('ws:shell-ready',{{detail:{{project:project,ready:ready}}}}));}}
 if(!ready){{closeMenu(false);return;}}
 settingsNavigation();items=collect();var next=items.map(function(x){{return x.key+'|'+x.label;}}).join('\\n');
 if(signature!==next){{signature=next;var children=items.map(function(item,index){{var b=el('button','',item.label);b.type='button';b.addEventListener('click',function(){{if(items[index])items[index].run();}});return b;}});nav.replaceChildren.apply(nav,children);}}
 Array.from(nav.children).forEach(function(b,index){{var item=items[index];var current=item&&(item.key===location.hash||item.key===location.pathname||(project==='gimpo'&&item.key&&location.pathname.endsWith(item.key))||(project==='bali'&&index===0)||(project==='canggu'&&item&&item.key===activePage));if(current){{if(b.getAttribute('aria-current')!=='page')b.setAttribute('aria-current','page');}}else if(b.hasAttribute('aria-current'))b.removeAttribute('aria-current');}});
 actions=accountActions();var key=actions.map(function(x){{return x.label;}}).join('|');
 if(key!==accountSignature){{accountSignature=key;account.replaceChildren();if(actions.length){{var d=el('details','ws-account-menu');var sum=el('summary','','계정');var panel=el('div','ws-account-dropdown');actions.forEach(function(a,index){{var b=el('button',a.label==='로그아웃'?'ws-signout':'',a.label);b.type='button';b.addEventListener('click',function(){{d.open=false;if(a.label==='비밀번호 변경'&&window.WSSettings&&window.WSSettings.supported)window.WSSettings.open('personal');else if(actions[index])actions[index].run();}});panel.append(b);}});d.append(sum,panel);account.append(d);}}}}
}}
var footerKey='';function settingsNavigation(){{var sdk=window.WSSettings;var key=sdk&&sdk.supported?'personal'+(sdk.canManageUsers?'|users':''):'';if(key===footerKey)return;footerKey=key;footer.replaceChildren(el('p','','공통 디자인 · 개인 설정'));if(key){{var group=el('nav','ws-shell-nav');[['personal','개인 설정'],['users','사용자 관리']].forEach(function(pair){{if(pair[0]==='users'&&!sdk.canManageUsers)return;var b=el('button','',pair[1]);b.type='button';b.addEventListener('click',function(){{closeMenu(false);sdk.open(pair[0]);}});group.append(b);}});footer.prepend(group);}}}}
function schedule(){{if(queued)return;queued=true;requestAnimationFrame(function(){{queued=false;refresh();}});}}
function start(){{refresh();new MutationObserver(schedule).observe(document.body,{{childList:true,subtree:true,attributes:true,attributeFilter:['hidden']}});document.addEventListener('ws:settings:ready',schedule);window.addEventListener('hashchange',schedule);window.addEventListener('popstate',schedule);document.addEventListener('keydown',function(e){{if(e.key==='Escape'&&document.body.classList.contains('ws-nav-open'))closeMenu(true);if(e.key==='Tab'&&mobile.matches&&document.body.classList.contains('ws-nav-open')){{var focus=Array.from(sidebar.querySelectorAll('button')).filter(visible);var first=focus[0],last=focus[focus.length-1];if(e.shiftKey&&document.activeElement===first){{e.preventDefault();last.focus();}}else if(!e.shiftKey&&document.activeElement===last){{e.preventDefault();first.focus();}}}}}});}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{{once:true}});else start();
}}());

/* 공통 개인 설정/사용자 관리. 실제 권한과 비밀번호 검증은 네이티브 서버가 수행한다. */
(function(){{
'use strict';
var project=(document.documentElement.className.match(/ws-project-([\\w-]+)/)||[])[1];
if(!['bali','canggu','gimpo','samsung','seoul','seongnam','tashkent'].includes(project))return; // 나머지 서버 어댑터는 별도 검증 후 연결한다.
var prefix=location.pathname.indexOf('/operating')===0?'/operating/':'/';
if(project==='gimpo')prefix=document.documentElement.getAttribute('data-ws-base')||'/velero/';
if(project==='bali'){{var mount=location.pathname.match(/^\\/(demo-app|bali)(?:\\/|$)/);prefix=mount?'/'+mount[1]+'/':'/';}}
if(project==='samsung'){{var source=document.querySelector('form[action$="/logout"],form[action$="/login"],form[action$="/settings"]');if(source)prefix=new URL(source.getAttribute('action'),location.href).pathname.replace(/\\/(logout|login|settings)\\/?$/,'')+'/';}}
if(project==='seongnam')prefix=(window.API_BASE_URL||'').replace(/\\/$/,'')+'/';
if(project==='seoul'){{var matched=location.pathname.match(/^\\/(k8stest|seoul)(?:\\/|$)/);prefix=matched?'/'+matched[1]+'/':'/';}}
var generation=0,session=null,panel=null;
function el(tag,props,text){{var n=document.createElement(tag);Object.keys(props||{{}}).forEach(function(k){{if(k==='className')n.className=props[k];else if(k in n)n[k]=props[k];else n.setAttribute(k,props[k]);}});if(text)n.textContent=text;return n;}}
async function request(path,method,body){{var headers={{'Content-Type':'application/json'}};var apiPath='api/auth/';if(project==='bali'){{headers.Authorization='Bearer '+(localStorage.getItem('access_token')||'');}}else if(project==='canggu'){{var token=localStorage.getItem('canggu_token')||'';headers.Authorization='Bearer '+token;}}else if(project==='gimpo'||project==='samsung'){{apiPath='api/auth/';}}else if(project==='tashkent'){{apiPath='api/auth/';}}else if(project==='seongnam'){{apiPath='api/v1/auth/';headers.Authorization='Bearer '+(localStorage.getItem('session_token')||'');if(path==='users'||path.indexOf('users/')===0)path='ui/'+path;}}else{{apiPath='api/';if(path==='users'||path.indexOf('users/')===0)path='ui/'+path;}}var response=await fetch(prefix+apiPath+path,{{method:method||'GET',headers:headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)}});var result=await response.json().catch(function(){{return {{}};}});if(!response.ok){{var detail=result.detail;throw new Error(typeof detail==='string'?detail:response.status===401?'다시 로그인해 주세요.':'요청을 처리하지 못했습니다.');}}return result;}}

async function loadSession(){{var g=generation;var value=await request('ui/session');if(g!==generation)throw new Error('로그인 상태가 변경되었습니다.');if(!value||!value.username||typeof value.availablePages!=='object')throw new Error('개인 설정 서버 응답을 확인할 수 없습니다.');session=value;api.role=value.role;api.canManageUsers=!!value.canManageUsers;api.pages=value.pages;api.availablePages=value.availablePages;document.dispatchEvent(new CustomEvent('ws:settings:ready'));return value;}}
var styles=`:host{{font:14px var(--ws-font,system-ui);color:var(--ws-text)}}*{{box-sizing:border-box}}dialog{{width:720px;max-width:calc(100vw - 32px);max-height:calc(100svh - 32px);padding:0;margin:auto;border:1px solid var(--ws-border);border-radius:16px;color:var(--ws-text);background:var(--ws-bg);box-shadow:0 24px 80px #0003}}dialog::backdrop{{background:var(--ws-overlay);backdrop-filter:blur(3px)}}header{{display:flex;align-items:center;justify-content:space-between;padding:24px 28px;border-bottom:1px solid var(--ws-border)}}h2{{font-size:20px;margin:0 0 4px}}h3{{font-size:16px;margin:0 0 16px}}p{{line-height:1.6}}.muted{{color:var(--ws-muted);font-size:13px;margin:0}}nav{{display:flex;padding:12px 28px 0;gap:8px;border-bottom:1px solid var(--ws-border)}}button{{font:600 14px var(--ws-font,system-ui);cursor:pointer;color:var(--ws-text);background:var(--ws-bg);border:1px solid var(--ws-border);border-radius:8px;min-height:40px;padding:8px 14px}}button:disabled{{opacity:.5;cursor:not-allowed}}button:focus-visible,input:focus-visible,select:focus-visible{{outline:2px solid var(--ws-accent);outline-offset:2px}}.tab{{border:0;border-bottom:2px solid transparent;border-radius:0;background:transparent}}.tab[aria-selected=true]{{color:var(--ws-accent);border-bottom-color:var(--ws-accent)}}main{{padding:28px;overflow:auto}}form{{display:grid;gap:16px}}label{{display:block;font-size:13px;font-weight:600}}input:not([type=checkbox]),select{{display:block;width:100%;height:44px;padding:10px 12px;margin-top:6px;border:1px solid var(--ws-border);border-radius:8px;font:14px var(--ws-font,system-ui);color:var(--ws-text);background:var(--ws-bg)}}.actions{{display:flex;gap:8px;justify-content:flex-end;margin-top:8px}}.primary{{background:var(--ws-accent);border-color:var(--ws-accent);color:var(--ws-on-accent)}}.notice{{font-size:13px;min-height:20px;margin:0;color:var(--ws-muted)}}.notice.error{{color:var(--ws-danger)}}.notice.success{{color:var(--ws-success)}}fieldset{{border:1px solid var(--ws-border);border-radius:8px;padding:12px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}legend{{font-size:13px;font-weight:600;padding:0 4px}}.check{{display:flex;align-items:center;gap:8px;font-weight:400}}input[type=checkbox]{{accent-color:var(--ws-accent)}}table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px}}th,td{{padding:10px 8px;border-bottom:1px solid var(--ws-border);text-align:left;vertical-align:top;word-break:break-word}}th{{color:var(--ws-muted)}}.scroll{{overflow-x:auto}}.row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}@media(max-width:600px){{header{{padding:20px}}nav{{padding-inline:20px}}main{{padding:20px}}.row{{grid-template-columns:1fr}}fieldset{{grid-template-columns:1fr}}}}`;
class SettingsPanel extends HTMLElement{{
 constructor(){{super();this.attachShadow({{mode:'open'}});var style=el('style');style.textContent=styles;this.dialog=el('dialog',{{'aria-label':'개인 설정'}});this.head=el('header');var heading=el('div');heading.append(el('h2',{{}},'환경 설정'));this.user=el('p',{{className:'muted'}});heading.append(this.user);var close=el('button',{{type:'button','aria-label':'설정 닫기'}},'닫기');close.addEventListener('click',()=>this.dialog.close());this.head.append(heading,close);this.tabs=el('nav',{{'aria-label':'설정 메뉴'}});this.main=el('main');this.dialog.append(this.head,this.tabs,this.main);this.shadowRoot.append(style,this.dialog);this.dialog.addEventListener('close',()=>{{this.main.replaceChildren();if(this.returnFocus&&this.returnFocus!==document.body&&this.returnFocus!==document.documentElement&&this.returnFocus.isConnected&&!this.returnFocus.closest('[inert]')&&this.returnFocus.getClientRects().length&&getComputedStyle(this.returnFocus).visibility!=='hidden')this.returnFocus.focus();else{{var target=document.querySelector('.ws-shell-header summary');if(target)target.focus();}}}});}}
 async open(view){{this.returnFocus=document.activeElement;if(!this.dialog.open)this.dialog.showModal();this.main.replaceChildren(el('p',{{className:'notice','role':'status'}},'계정 정보를 확인하고 있습니다.'));try{{await loadSession();this.user.textContent=session.username+' · '+(session.role==='admin'?'관리자':'일반 사용자');this.tabs.replaceChildren();var choices=[['personal','개인 설정']];if(session.canManageUsers)choices.push(['users','사용자 관리']);choices.forEach(([key,label])=>{{var b=el('button',{{type:'button',className:'tab',role:'tab','aria-selected':String(view===key)}},label);b.addEventListener('click',()=>this.show(key));b.dataset.key=key;this.tabs.append(b);}});await this.show(view==='users'&&session.canManageUsers?'users':'personal');}}catch(error){{this.main.replaceChildren(el('p',{{className:'notice error',role:'alert'}},error.message));}}}}
 async show(view){{this.tabs.querySelectorAll('button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.key===view)));this.main.replaceChildren();if(view==='users'){{if(!session.canManageUsers){{this.main.append(el('p',{{role:'alert'}},'관리자 권한이 필요합니다.'));return;}}await this.users();}}else this.password();}}
 password(){{this.dialog.setAttribute('aria-label','개인 설정 · 비밀번호 변경');this.main.append(el('h3',{{}},'비밀번호 변경'));var form=el('form');var current=this.field(form,'현재 비밀번호','password','current-password');var next=this.field(form,'새 비밀번호','password','new-password');var confirm=this.field(form,'새 비밀번호 확인','password','new-password');next.minLength=session.passwordMinLength;confirm.minLength=session.passwordMinLength;var note=el('p',{{className:'muted'}},'새 비밀번호를 두 번 동일하게 입력해 주세요.');var status=el('p',{{className:'notice',role:'status'}});var actions=el('div',{{className:'actions'}});var cancel=el('button',{{type:'button'}},'취소');cancel.addEventListener('click',()=>this.dialog.close());var submit=el('button',{{type:'submit',className:'primary'}},'변경 사항 저장');actions.append(cancel,submit);form.append(note,status,actions);form.addEventListener('submit',async event=>{{event.preventDefault();if(next.value!==confirm.value){{status.className='notice error';status.setAttribute('role','alert');status.textContent='새 비밀번호가 일치하지 않습니다.';confirm.focus();return;}}submit.disabled=true;status.textContent='저장 중입니다.';try{{var result=await request('ui/password','POST',{{current_password:current.value,new_password:next.value,confirm_password:confirm.value}});form.reset();status.className='notice success';status.textContent='비밀번호가 변경되었습니다.';if(result.reauthenticate){{if(project==='seongnam')localStorage.removeItem('session_token');if(project==='bali'){{localStorage.removeItem('access_token');localStorage.removeItem('username');}}status.textContent='비밀번호가 변경되었습니다. 다시 로그인해 주세요.';var login=el('button',{{type:'button'}},'로그인 화면으로');login.addEventListener('click',()=>location.reload());form.append(login);submit.disabled=true;submit.dataset.saved='true';}}}}catch(error){{status.className='notice error';status.setAttribute('role','alert');status.textContent=error.message;}}finally{{if(!submit.dataset.saved)submit.disabled=false;}}}});this.main.append(form);if(session.supportsUsernameChange)this.username();current.focus();}}
 username(){{var details=el('details',{{className:'muted'}});details.append(el('summary',{{}},'아이디 변경'));var form=el('form');var current=this.field(form,'현재 비밀번호 (아이디 변경)','password','current-password');var name=this.field(form,'새 아이디','text','username');name.value=session.username;name.maxLength=128;var status=el('p',{{className:'notice',role:'status'}});var button=el('button',{{type:'submit'}},'아이디 변경 저장');form.append(status,button);form.addEventListener('submit',async event=>{{event.preventDefault();button.disabled=true;try{{await request('ui/username','POST',{{current_password:current.value,new_username:name.value}});form.reset();status.className='notice success';status.textContent='아이디가 변경되었습니다. 다시 로그인해 주세요.';var login=el('button',{{type:'button'}},'로그인 화면으로');login.addEventListener('click',()=>location.reload());form.append(login);}}catch(error){{status.className='notice error';status.setAttribute('role','alert');status.textContent=error.message;button.disabled=false;}}}});details.append(form);this.main.append(details);}}
 field(form,label,type,autocomplete){{var group=el('label',{{}},label);var input=el('input',{{type:type,required:true,autocomplete:autocomplete||'off'}});group.append(input);form.append(group);return input;}}
 async users(){{this.dialog.setAttribute('aria-label','사용자 관리 · 페이지 권한');this.main.append(el('h3',{{}},'사용자 관리'));var notice=el('p',{{className:'notice',role:'status'}},'사용자 목록을 불러오고 있습니다.');this.main.append(notice);try{{var users=await request('users');if(!Array.isArray(users))throw new Error('사용자 목록 응답이 올바르지 않습니다.');var wrap=el('div',{{className:'scroll'}});var table=el('table');var head=el('tr');['아이디','역할','페이지 권한',''].forEach(t=>head.append(el('th',{{}},t)));var thead=el('thead');thead.append(head);var body=el('tbody');users.forEach(user=>{{var row=el('tr');row.append(el('td',{{}},user.username),el('td',{{}},user.role==='admin'?'관리자':'일반 사용자'),el('td',{{}},user.role==='admin'?'모든 페이지':user.pages===null?'기존 권한':(user.pages||[]).map(x=>session.availablePages[x]||x).join(', ')||'개인 설정만'));var cell=el('td');var edit=el('button',{{type:'button'}},'수정');edit.addEventListener('click',()=>this.userForm(holder,user));cell.append(edit);row.append(cell);body.append(row);}});table.append(thead,body);wrap.append(table);var holder=el('section');notice.remove();this.main.append(wrap,holder);this.userForm(holder,null);}}catch(error){{notice.className='notice error';notice.setAttribute('role','alert');notice.textContent=error.message;}}}}
 userForm(holder,user){{holder.replaceChildren(el('h3',{{}},user?'계정·페이지 권한 수정':'신규 계정 추가'));var form=el('form');var name=this.field(form,'아이디','text','username');if(user){{name.value=user.username;name.disabled=true;}}var roleLabel=el('label',{{}},'역할');var role=el('select');[['viewer','일반 사용자'],['admin','관리자']].forEach(([value,label])=>role.append(el('option',{{value:value}},label)));role.value=user?user.role:'viewer';roleLabel.append(role);form.append(roleLabel);var password=this.field(form,user?'새 비밀번호 (변경할 때만)':'초기 비밀번호','password','new-password');var confirm=this.field(form,'비밀번호 확인','password','new-password');password.required=!user;confirm.required=!user;password.minLength=session.passwordMinLength;confirm.minLength=session.passwordMinLength;
 var grants=el('fieldset');grants.append(el('legend',{{}},'페이지 조회 권한'));Object.keys(session.availablePages).filter(x=>x!=='users').forEach(key=>{{var label=el('label',{{className:'check'}});var checkbox=el('input',{{type:'checkbox',value:key}});checkbox.checked=key==='settings'||(user?(user.pages===null||((user.pages||[]).includes(key))):false);checkbox.disabled=key==='settings';label.append(checkbox,document.createTextNode(session.availablePages[key]));grants.append(label);}});role.addEventListener('change',()=>{{grants.querySelectorAll('input').forEach(input=>{{input.disabled=role.value==='admin'||input.value==='settings';if(role.value==='admin')input.checked=true;}});}});if(role.value==='admin')role.dispatchEvent(new Event('change'));form.append(grants);var hint=el('p',{{className:'muted'}},'관리자는 모든 페이지와 사용자 관리에 접근합니다. 일반 사용자는 선택한 페이지를 조회할 수 있습니다.');var status=el('p',{{className:'notice',role:'status'}});var actions=el('div',{{className:'actions'}});if(user){{var reset=el('button',{{type:'button'}},'신규 계정 추가로 돌아가기');reset.addEventListener('click',()=>this.userForm(holder,null));actions.append(reset);}}var save=el('button',{{type:'submit',className:'primary'}},user?'변경 사항 저장':'계정 추가');actions.append(save);form.append(hint,status,actions);form.addEventListener('submit',async event=>{{event.preventDefault();if(password.value!==confirm.value){{status.className='notice error';status.textContent='비밀번호가 일치하지 않습니다.';confirm.focus();return;}}save.disabled=true;var pages=Array.from(grants.querySelectorAll('input:checked')).map(x=>x.value);var body={{role:role.value,pages:pages}};if(user){{body.scope_type=user.scope_type;body.scope_ref=user.scope_ref;if(password.value)body.password=password.value;}}else{{body.username=name.value.trim();body.password=password.value;body.scope_type='cluster';body.scope_ref='';}}try{{await request('users'+(user?'/'+encodeURIComponent(user.username):''),user?'PATCH':'POST',body);form.reset();await this.show('users');var success=el('p',{{className:'notice success',role:'status'}},user?'계정 권한이 저장되었습니다.':'새 계정이 추가되었습니다.');this.main.prepend(success);}}catch(error){{status.className='notice error';status.setAttribute('role','alert');status.textContent=error.message;save.disabled=false;}}}});holder.append(form);}}
}}
if(!customElements.get('ws-user-settings'))customElements.define('ws-user-settings',SettingsPanel);
var api={{supported:true,canManageUsers:false,logout:async function(){{await request('logout','POST',{{}});location.assign(prefix+'login');}},open:async function(view){{if(project==='samsung'&&!/\\/settings\\/?$/.test(location.pathname)){{location.assign(prefix+'settings?ws-panel='+encodeURIComponent(view||'personal'));return;}}if(!panel){{panel=document.createElement('ws-user-settings');document.body.append(panel);}}await panel.open(view||'personal');}}}};window.WSSettings=api;
document.addEventListener('ws:shell-ready',function(event){{if(event.detail.project!==project)return;if(!event.detail.ready){{generation++;session=null;api.canManageUsers=false;delete api.pages;delete api.role;if(panel&&panel.dialog.open)panel.dialog.close();}}else loadSession().catch(function(){{}});}});
if(document.body&&document.body.classList.contains('ws-shell-ready'))loadSession().catch(function(){{}});
}}());
</script>
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
def login_form(request: Request, next: str = ''):
    user=auth.get_store().session_user(request.cookies.get(auth.COOKIE_NAME))
    if user:return RedirectResponse(_authorized_next(user,next),status_code=302)
    return HTMLResponse(_login_page(next_url=next))


@app.post("/login")
def login_submit(request: Request, username: str = Form(''), password: str = Form(''), next: str = Form('')):
    store=auth.get_store();token=store.authenticate_session(username,password)
    if token is None:
        return HTMLResponse(_login_page(next_url=next,error='아이디 또는 비밀번호가 올바르지 않습니다.'),status_code=401)
    response=RedirectResponse(_authorized_next(username,next),status_code=302)
    _set_session_cookie(response,request,token)
    return response


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
def settings_submit(request: Request, current_password: str = Form(''), new_username: str = Form(''), new_password: str = Form(''), confirm_password: str = Form('')):
    store=auth.get_store();user=current_user() or store.username
    try:
        final_user=store.change_credentials(user,current_password,new_username=new_username,new_password=new_password,confirm_password=confirm_password,strict=False)
    except AccountError as error:
        return HTMLResponse(_settings_page(username=user,error=str(error)),status_code=400)
    response=HTMLResponse(_settings_page(username=final_user,message='변경되었습니다. 다른 기기의 세션은 로그아웃됩니다.'))
    _set_session_cookie(response,request,store.issue_session(final_user))
    return response


# 공통 웹 계정 계약. 기존 트래픽 조회/정책/Basic 인증과 데이터 수집은 유지한다.
def _web_page(path):
    if path in ('/','/flows','/api/flows'):return 'flows'
    if path in ('/policies','/api/policies'):return 'policies'
    if path.startswith('/api/auth/users'):return 'users'
    if path=='/settings' or path.startswith('/api/auth/ui/') or path=='/api/auth/logout':return 'settings'
    return None

def _authorized_next(username,value):
    prefix=_url_prefix();access=auth.get_store().access(username)
    candidate=_safe_next(value);decoded=unquote(candidate)
    path=urlsplit(candidate).path
    if prefix and (path==prefix or path.startswith(prefix+'/')):path=path[len(prefix):] or '/'
    page=_web_page(path)
    if not decoded.startswith('//') and chr(92) not in decoded and not any(ord(c)<32 for c in decoded):
        if access['role']=='admin' or (page and page_policy.allowed('samsung',access['role'],access['pages'],page)):return candidate
    for page,path in [('flows','/'),('policies','/policies'),('settings','/settings')]:
        if page_policy.allowed('samsung',access['role'],access['pages'],page):return prefix+path
    return prefix+'/settings'

def _account_action(function,*args,**kwargs):
    try:return function(*args,**kwargs)
    except AccountError as error:raise HTTPException(error.status,str(error)) from error

class UIUserCreate(BaseModel):
    username:str
    password:str
    role:str='viewer'
    pages:list[str]=Field(default_factory=list)
class UIUserEdit(BaseModel):
    role:str|None=None
    pages:list[str]|None=None
    password:str|None=None
class UIPassword(BaseModel):
    current_password:str
    new_password:str
    confirm_password:str
class UIUsername(BaseModel):
    current_password:str
    new_username:str

@app.get('/api/auth/ui/session')
def ui_session():return _account_action(auth.get_store().summary,current_user())

@app.post('/api/auth/ui/password')
def ui_password(body:UIPassword):
    if body.new_password!=body.confirm_password:raise HTTPException(400,'새 비밀번호가 일치하지 않습니다')
    if not body.new_password:raise HTTPException(400,'새 비밀번호를 입력하세요')
    _account_action(auth.get_store().change_credentials,current_user(),body.current_password,new_password=body.new_password,confirm_password=body.confirm_password)
    return {'ok':True,'reauthenticate':True}

@app.post('/api/auth/ui/username')
def ui_username(body:UIUsername):
    _account_action(auth.get_store().change_credentials,current_user(),body.current_password,new_username=body.new_username)
    return {'ok':True,'reauthenticate':True}

@app.get('/api/auth/users')
def ui_users():return _account_action(auth.get_store().list_users)

@app.post('/api/auth/users')
def ui_create_user(body:UIUserCreate):return _account_action(auth.get_store().create_user,body.username,body.password,body.role,body.pages)

@app.patch('/api/auth/users/{username}')
def ui_edit_user(username:str,body:UIUserEdit):
    changes=body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):raise HTTPException(400,'변경 값은 null일 수 없습니다')
    return _account_action(auth.get_store().edit_user,username,changes)

@app.post('/api/auth/logout')
def ui_logout():
    response=JSONResponse({'ok':True});response.delete_cookie(auth.COOKIE_NAME,path=_url_prefix() or '/')
    return response
