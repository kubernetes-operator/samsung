"""읽기 전용 웹 대시보드 - TrafficPolicy 운영 현황.

실행:
    uvicorn k8s_traffic_operator.dashboard.app:app --host 0.0.0.0 --port 8080

- `GET /`             : HTML 대시보드 (5초마다 자동 새로고침, JS로 /api/policies 폴링)
- `GET /api/policies` : 동일 데이터의 JSON (프로그램/모니터링 연동용)
- `GET /healthz`      : liveness/readiness 프로브용

이 앱은 클러스터에 아무것도 쓰지 않는다. `data.fetch_policies()`가 유일한 클러스터
접근 지점이며 get/list/watch만 수행한다.
"""

from __future__ import annotations

import html
import time
from typing import List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import data
from .data import PolicySummary

app = FastAPI(title="TrafficPolicy Dashboard", docs_url=None, redoc_url=None)

_SEVERITY_COLOR = {"none": "#4b5563", "warning": "#b45309", "critical": "#b91c1c"}
_PHASE_COLOR = {
    "Reconciled": "#15803d", "ok": "#15803d",
    "no_data": "#6b7280", "collection_failed": "#b45309",
    "Registered": "#2563eb", "Pending": "#6b7280", "Error": "#b91c1c",
}


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


def _row_html(p: PolicySummary) -> str:
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


_PAGE_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrafficPolicy Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 0; padding: 2rem;
         background: #0b0d12; color: #e5e7eb; }}
  @media (prefers-color-scheme: light) {{ body {{ background: #f8fafc; color: #111827; }} }}
  h1 {{ font-size: 1.25rem; margin-bottom: .25rem; }}
  .meta {{ color: #9ca3af; font-size: .85rem; margin-bottom: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th {{ text-align: left; padding: .5rem .75rem; border-bottom: 2px solid #374151; color: #9ca3af;
        font-weight: 600; text-transform: uppercase; font-size: .75rem; }}
  td {{ padding: .6rem .75rem; border-bottom: 1px solid #1f2937; vertical-align: top; }}
  @media (prefers-color-scheme: light) {{
    th {{ border-bottom-color: #e5e7eb; }} td {{ border-bottom-color: #f1f5f9; }}
  }}
  .sub {{ color: #9ca3af; font-size: .78rem; margin-top: .15rem; }}
  .badge {{ display: inline-block; padding: .15rem .5rem; border-radius: .35rem; color: white;
            font-size: .78rem; font-weight: 600; }}
  .error-row td {{ color: #b91c1c; }}
  .empty {{ color: #9ca3af; padding: 2rem 0; text-align: center; }}
</style>
</head>
<body>
<h1>TrafficPolicy Dashboard</h1>
<div class="meta">트래픽 기반 자동운영 오퍼레이터 · 읽기 전용 · 10초마다 새로고침 · {count}개 정책</div>
{table}
</body>
</html>"""


def _render(policies: List[PolicySummary]) -> str:
    if not policies:
        table = '<div class="empty">TrafficPolicy가 없습니다.</div>'
    else:
        rows = "\n".join(_row_html(p) for p in policies)
        table = f"""<table>
<thead><tr>
  <th>Namespace</th><th>Name</th><th>Phase</th><th>Snapshot</th>
  <th>Last Decision</th><th>Applied</th><th>Reconciled</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
    return _PAGE_TEMPLATE.format(count=len(policies), table=table)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return _render(data.fetch_policies())


@app.get("/api/policies", response_class=JSONResponse)
def api_policies():
    policies = data.fetch_policies()
    return {
        "generatedAt": time.time(),
        "count": len(policies),
        "policies": [p.__dict__ for p in policies],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
