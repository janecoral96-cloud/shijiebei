#!/usr/bin/env python3
"""
Build accuracy.html by comparing stored predictions with FixtureDownload results.

The workflow is intentionally manual: keep predictions in data/predictions.json,
run this script after match results are available, then commit accuracy.html.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_PATH = ROOT / "data" / "predictions.json"
ACCURACY_PATH = ROOT / "accuracy.html"
FIXTURE_CSV_URL = "https://fixturedownload.com/download/fifa-world-cup-2026-GMTStandardTime.csv"

try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


def normalize(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def parse_result(raw: str) -> tuple[int, int] | None:
    raw = (raw or "").strip()
    if not raw or "-" not in raw:
        return None
    left, right = [part.strip() for part in raw.split("-", 1)]
    if not left.isdigit() or not right.isdigit():
        return None
    return int(left), int(right)


def side_from_score(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if away_goals > home_goals:
        return "away"
    return "draw"


def load_results() -> dict[tuple[str, str, str], dict[str, Any]]:
    response = requests.get(FIXTURE_CSV_URL, timeout=30)
    response.raise_for_status()
    rows = csv.DictReader(io.StringIO(response.text))
    results: dict[tuple[str, str, str], dict[str, Any]] = {}

    for row in rows:
        raw_date = (row.get("Date") or "").strip()
        try:
            kickoff_utc = dt.datetime.strptime(raw_date, "%d/%m/%Y %H:%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        kickoff_bj = kickoff_utc.astimezone(BEIJING_TZ)
        home = (row.get("Home Team") or "").strip()
        away = (row.get("Away Team") or "").strip()
        key = (kickoff_bj.date().isoformat(), normalize(home), normalize(away))
        score = parse_result(row.get("Result", ""))
        results[key] = {
            "date_beijing": kickoff_bj.date().isoformat(),
            "time_beijing": kickoff_bj.strftime("%H:%M"),
            "home_team": home,
            "away_team": away,
            "result_raw": (row.get("Result") or "").strip(),
            "score": score,
        }
    return results


def evaluate_prediction(prediction: dict[str, Any], results: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, Any]:
    key = (
        prediction["date_beijing"],
        normalize(prediction["home_team"]),
        normalize(prediction["away_team"]),
    )
    result = results.get(key)
    row = dict(prediction)
    row["status"] = "pending"
    row["actual_score"] = ""
    row["actual_1x2"] = ""
    row["hit_1x2"] = None
    row["hit_primary_score"] = None
    row["hit_score_candidate"] = None

    if not result or not result.get("score"):
        return row

    home_goals, away_goals = result["score"]
    actual_score = f"{home_goals}-{away_goals}"
    actual_1x2 = side_from_score(home_goals, away_goals)
    candidates = [str(item).strip() for item in prediction.get("score_candidates", [])]

    row["status"] = "finished"
    row["actual_score"] = actual_score
    row["actual_1x2"] = actual_1x2
    row["hit_1x2"] = prediction.get("pick_1x2") == actual_1x2
    row["hit_primary_score"] = prediction.get("primary_score") == actual_score
    row["hit_score_candidate"] = actual_score in candidates
    return row


def pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "-"
    return f"{numerator / denominator * 100:.1f}%"


def label_1x2(value: str) -> str:
    return {"home": "主胜", "draw": "平局", "away": "客胜"}.get(value, value or "-")


def build_html(rows: list[dict[str, Any]]) -> str:
    finished = [row for row in rows if row["status"] == "finished"]
    pending = [row for row in rows if row["status"] == "pending"]
    total = len(finished)
    hit_1x2 = sum(1 for row in finished if row["hit_1x2"])
    hit_primary = sum(1 for row in finished if row["hit_primary_score"])
    hit_candidate = sum(1 for row in finished if row["hit_score_candidate"])
    updated = dt.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    table_rows = []
    for row in rows:
        status_class = "pending" if row["status"] == "pending" else "done"
        one_x_two_hit = "-" if row["hit_1x2"] is None else ("命中" if row["hit_1x2"] else "未中")
        primary_hit = "-" if row["hit_primary_score"] is None else ("命中" if row["hit_primary_score"] else "未中")
        candidate_hit = "-" if row["hit_score_candidate"] is None else ("命中" if row["hit_score_candidate"] else "未中")
        report = row.get("report", "")
        report_link = f'<a href="{html.escape(report)}">报告</a>' if report else "-"
        table_rows.append(
            f"""
      <tr class="{status_class}">
        <td>{html.escape(row["date_beijing"])}<br><span>{html.escape(row.get("time_beijing", ""))}</span></td>
        <td><strong>{html.escape(row["home_team"])} vs {html.escape(row["away_team"])}</strong><br><span>{html.escape(row.get("group", ""))}</span></td>
        <td>{label_1x2(str(row.get("pick_1x2", "")))}</td>
        <td>{html.escape(str(row.get("primary_score", "")))}</td>
        <td>{html.escape(", ".join(row.get("score_candidates", [])))}</td>
        <td>{html.escape(row["actual_score"] or "待赛果")}</td>
        <td>{one_x_two_hit}</td>
        <td>{primary_hit}</td>
        <td>{candidate_hit}</td>
        <td>{report_link}</td>
      </tr>
            """.strip()
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>世界杯预测准确率</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#f0ede8;color:#1a1a1a;padding:28px 18px;line-height:1.6}}
.page{{max-width:1120px;margin:0 auto}}
.top{{display:flex;justify-content:space-between;gap:18px;align-items:flex-end;border-bottom:1px solid #d8d2c8;padding-bottom:20px;margin-bottom:20px}}
h1{{font-size:28px;line-height:1.2}}
.sub,.updated,td span{{font-size:12px;color:#666}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}}
.metric{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px}}
.metric .label{{font-size:12px;color:#777;margin-bottom:4px}}
.metric .value{{font-size:26px;font-weight:800;color:#1a5fa8}}
.panel{{background:#fff;border:1px solid #ddd;border-radius:8px;overflow:hidden}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:11px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#777;background:#f8f7f4}}
tr.done td:nth-child(7),tr.done td:nth-child(8),tr.done td:nth-child(9){{font-weight:700}}
tr.pending{{color:#666}}
a{{color:#1a5fa8;text-decoration:none;font-weight:700}}
.note{{margin-top:14px;color:#666;font-size:12px}}
@media(max-width:820px){{.top{{display:block}}.updated{{margin-top:8px}}.cards{{grid-template-columns:1fr 1fr}}.panel{{overflow:auto}}table{{min-width:900px}}}}
</style>
</head>
<body>
<main class="page">
  <header class="top">
    <div>
      <h1>世界杯预测准确率</h1>
      <div class="sub">对比预测方向、主比分、比分候选池与赛后结果。</div>
    </div>
    <div class="updated">北京时间 {updated} 更新</div>
  </header>
  <section class="cards">
    <div class="metric"><div class="label">已结算比赛</div><div class="value">{total}</div></div>
    <div class="metric"><div class="label">1X2 命中率</div><div class="value">{pct(hit_1x2, total)}</div></div>
    <div class="metric"><div class="label">主比分命中率</div><div class="value">{pct(hit_primary, total)}</div></div>
    <div class="metric"><div class="label">候选比分命中率</div><div class="value">{pct(hit_candidate, total)}</div></div>
  </section>
  <section class="panel">
    <table>
      <thead>
        <tr>
          <th>日期</th>
          <th>比赛</th>
          <th>预测方向</th>
          <th>主比分</th>
          <th>候选比分</th>
          <th>实际比分</th>
          <th>1X2</th>
          <th>主比分</th>
          <th>候选比分</th>
          <th>来源</th>
        </tr>
      </thead>
      <tbody>
        {"".join(table_rows)}
      </tbody>
    </table>
  </section>
  <div class="note">待赛果比赛：{len(pending)} 场。赛果来自 FixtureDownload 赛程 CSV；若源数据暂未更新，该场保持待赛果。</div>
</main>
</body>
</html>
"""


def main() -> int:
    data = json.loads(PREDICTIONS_PATH.read_text(encoding="utf-8"))
    predictions = data.get("predictions", [])
    results = load_results()
    rows = [evaluate_prediction(item, results) for item in predictions]
    ACCURACY_PATH.write_text(build_html(rows), encoding="utf-8")
    print(f"Wrote {ACCURACY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
