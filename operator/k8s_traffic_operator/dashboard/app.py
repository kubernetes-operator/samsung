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
"""

from __future__ import annotations

import html
import time
from typing import List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import data, hubble_flows
from .data import PolicySummary
from .hubble_flows import FlowSummary

app = FastAPI(title="TrafficPolicy Dashboard", docs_url=None, redoc_url=None)

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
  nav a.active { color: inherit; font-weight: 600; text-decoration: underline; }
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
  <a href="/" class="{nav_policies}">TrafficPolicy 현황</a>
  <a href="/flows" class="{nav_flows}">실시간 Pod 트래픽 흐름 (Hubble)</a>
</nav>
<h1>{heading}</h1>
<div class="meta">{meta}</div>
{table}
</body>
</html>"""


def _page(*, active: str, title: str, heading: str, meta: str, table: str) -> str:
    return _PAGE_TEMPLATE.format(
        title=title, style=_STYLE, heading=heading, meta=meta, table=table,
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


def _render_flows(summary: FlowSummary) -> str:
    if summary.fetch_error:
        table = (
            f'<div class="error-row" style="padding:1rem">⚠ Hubble 조회 실패: '
            f'{html.escape(summary.fetch_error)}</div>'
        )
        meta = "Cilium Hubble 기반 실제 Pod 트래픽 흐름 · 조회 실패"
    elif summary.total == 0:
        table = '<div class="empty">관측된 흐름이 없습니다.</div>'
        meta = "Cilium Hubble 기반 실제 Pod 트래픽 흐름 · 0건"
    else:
        badges = " ".join(
            f'<span class="badge" style="background:{_VERDICT_COLOR.get(v, "#4b5563")}">{html.escape(v)} {c}</span>'
            for v, c in summary.verdicts.items()
        )
        rows = "\n".join(_flow_pair_row_html(p) for p in summary.top_pairs)
        table = f"""<div style="margin-bottom:1rem">{badges}</div>
<table>
<thead><tr>
  <th>Source</th><th></th><th>Destination</th><th>Proto</th><th>Count</th><th>Last Seen (UTC)</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
        meta = (
            f"Cilium Hubble 기반 실제 Pod 트래픽 흐름(L3/L4, HTTP RPS 아님) · "
            f"최근 {summary.total}건 중 상위 {len(summary.top_pairs)}개 연결 쌍"
        )
    return _page(
        active="flows", title="Pod Traffic Flows (Hubble)", heading="실시간 Pod 트래픽 흐름",
        meta=meta, table=table,
    )


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
def flows() -> str:
    return _render_flows(hubble_flows.fetch_summary())


@app.get("/api/flows", response_class=JSONResponse)
def api_flows():
    summary = hubble_flows.fetch_summary()
    return {
        "generatedAt": time.time(),
        "total": summary.total,
        "verdicts": summary.verdicts,
        "topPairs": summary.top_pairs,
        "fetchError": summary.fetch_error,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
