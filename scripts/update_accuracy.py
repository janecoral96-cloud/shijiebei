#!/usr/bin/env python3
"""
Build accuracy.html by comparing stored predictions with online match results.

Keep predictions in data/predictions.json, run this script after match results
are available, then commit the regenerated accuracy.html.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
import unicodedata
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_PATH = ROOT / "data" / "predictions.json"
ACCURACY_PATH = ROOT / "accuracy.html"
FIXTURE_CSV_URL = "https://fixturedownload.com/download/fifa-world-cup-2026-GMTStandardTime.csv"

TEAM_ALIASES = {
    "usa": "unitedstates",
    "us": "unitedstates",
    "unitedstates": "unitedstates",
    "unitedstatesofamerica": "unitedstates",
    "turkey": "turkiye",
    "turkiye": "turkiye",
    "türkiye": "turkiye",
}

try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


def normalize(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    key = "".join(ch.lower() for ch in ascii_value if ch.isalnum())
    return TEAM_ALIASES.get(key, key)


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


def team_pair(home: str, away: str) -> tuple[str, str]:
    return normalize(home), normalize(away)


def nearby_dates(date_value: str, days: int = 1) -> set[str]:
    try:
        base = dt.date.fromisoformat(date_value)
    except ValueError:
        return {date_value}
    return {
        (base + dt.timedelta(days=offset)).isoformat()
        for offset in range(-days, days + 1)
    }


def load_results() -> dict[str, Any]:
    response = requests.get(FIXTURE_CSV_URL, timeout=30)
    response.raise_for_status()
    rows = csv.DictReader(io.StringIO(response.text))
    fixtures: list[dict[str, Any]] = []

    for row in rows:
        raw_date = (row.get("Date") or "").strip()
        try:
            kickoff_utc = dt.datetime.strptime(raw_date, "%d/%m/%Y %H:%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue

        kickoff_bj = kickoff_utc.astimezone(BEIJING_TZ)
        home = (row.get("Home Team") or "").strip()
        away = (row.get("Away Team") or "").strip()
        fixtures.append(
            {
                "date_beijing": kickoff_bj.date().isoformat(),
                "time_beijing": kickoff_bj.strftime("%H:%M"),
                "source_date": raw_date,
                "home_team": home,
                "away_team": away,
                "group": (row.get("Group") or "").strip(),
                "location": (row.get("Location") or "").strip(),
                "result_raw": (row.get("Result") or "").strip(),
                "score": parse_result(row.get("Result", "")),
                "pair": team_pair(home, away),
            }
        )

    return {
        "source_url": FIXTURE_CSV_URL,
        "fetched_at": dt.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M"),
        "fixtures": fixtures,
    }


def find_fixture(prediction: dict[str, Any], fixtures: list[dict[str, Any]]) -> dict[str, Any] | None:
    pred_pair = team_pair(prediction["home_team"], prediction["away_team"])
    allowed_dates = nearby_dates(prediction["date_beijing"], days=1)
    pair_matches = [item for item in fixtures if item["pair"] == pred_pair]

    dated_matches = [item for item in pair_matches if item["date_beijing"] in allowed_dates]
    if dated_matches:
        return dated_matches[0]
    if len(pair_matches) == 1:
        return pair_matches[0]
    return None


def evaluate_prediction(prediction: dict[str, Any], fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    result = find_fixture(prediction, fixtures)
    row = dict(prediction)
    row["status"] = "pending"
    row["actual_score"] = ""
    row["actual_1x2"] = ""
    row["hit_1x2"] = None
    row["hit_primary_score"] = None
    row["hit_score_candidate"] = None
    row["source_date"] = ""
    row["source_time_beijing"] = ""
    row["source_result_raw"] = ""

    if not result:
        row["status_note"] = "赛果源暂未找到该场"
        return row

    row["source_date"] = str(result["date_beijing"])
    row["source_time_beijing"] = str(result["time_beijing"])
    row["source_result_raw"] = str(result["result_raw"])

    if not result.get("score"):
        row["status_note"] = "赛果源已找到赛程，但比分尚未更新"
        return row

    home_goals, away_goals = result["score"]
    actual_score = f"{home_goals}-{away_goals}"
    actual_1x2 = side_from_score(home_goals, away_goals)
    candidates = [str(item).strip() for item in prediction.get("score_candidates", [])]

    row["status"] = "finished"
    row["status_note"] = "已结算"
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


def hit_label(value: bool | None) -> str:
    if value is None:
        return "-"
    return "命中" if value else "未中"


def hit_class(value: bool | None) -> str:
    if value is None:
        return "muted"
    return "hit" if value else "miss"


def build_html(rows: list[dict[str, Any]], source_url: str, fetched_at: str) -> str:
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
        actual_score = row["actual_score"] or "待赛果"
        source_time = ""
        if row.get("source_date"):
            source_time = f'源时间：{html.escape(row["source_date"])} {html.escape(row.get("source_time_beijing", ""))}'
        report = row.get("report", "")
        report_link = f'<a href="{html.escape(report)}">报告</a>' if report else "-"
        table_rows.append(
            f"""
      <tr class="{status_class}">
        <td>{html.escape(row["date_beijing"])}<br><span>{html.escape(row.get("time_beijing", ""))}</span></td>
        <td>
          <strong>{html.escape(row["home_team"])} vs {html.escape(row["away_team"])}</strong>
          <br><span>{html.escape(row.get("group", ""))}</span>
        </td>
        <td>{label_1x2(str(row.get("pick_1x2", "")))}</td>
        <td>{html.escape(str(row.get("primary_score", "")))}</td>
        <td>{html.escape(", ".join(row.get("score_candidates", [])))}</td>
        <td>
          <strong>{html.escape(actual_score)}</strong>
          <br><span>{html.escape(row.get("status_note", ""))}</span>
          <br><span>{source_time}</span>
        </td>
        <td class="{hit_class(row["hit_1x2"])}">{hit_label(row["hit_1x2"])}</td>
        <td class="{hit_class(row["hit_primary_score"])}">{hit_label(row["hit_primary_score"])}</td>
        <td class="{hit_class(row["hit_score_candidate"])}">{hit_label(row["hit_score_candidate"])}</td>
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
.hit{{color:#137333;font-weight:800}}
.miss{{color:#b3261e;font-weight:800}}
.muted{{color:#777}}
a{{color:#1a5fa8;text-decoration:none;font-weight:700}}
.note{{margin-top:14px;color:#666;font-size:12px}}
@media(max-width:820px){{.top{{display:block}}.updated{{margin-top:8px}}.cards{{grid-template-columns:1fr 1fr}}.panel{{overflow:auto}}table{{min-width:980px}}}}
</style>
</head>
<body>
<main class="page">
  <header class="top">
    <div>
      <h1>世界杯预测准确率</h1>
      <div class="sub">对比预测方向、主比分、候选比分池与赛后结果。</div>
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
          <th>报告日期</th>
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
  <div class="note">
    待赛果比赛：{len(pending)} 场。赛果来自 <a href="{html.escape(source_url)}">FixtureDownload 2026 World Cup CSV</a>，
    本页抓取时间：北京时间 {html.escape(fetched_at)}。如果源数据比分为空，本页会先显示“待赛果”，下次运行脚本后自动结算。
  </div>
</main>
</body>
</html>
"""


def main() -> int:
    data = json.loads(PREDICTIONS_PATH.read_text(encoding="utf-8"))
    predictions = data.get("predictions", [])
    result_data = load_results()
    rows = [evaluate_prediction(item, result_data["fixtures"]) for item in predictions]
    ACCURACY_PATH.write_text(
        build_html(rows, result_data["source_url"], result_data["fetched_at"]),
        encoding="utf-8",
    )
    print(f"Wrote {ACCURACY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
