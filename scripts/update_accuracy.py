#!/usr/bin/env python3
"""
Build accuracy.html by comparing stored predictions with match results.

Result priority:
1. data/results_overrides.json for verified manual scores.
2. ESPN scoreboard API when a match is marked completed.
3. FixtureDownload CSV as a fallback result feed.
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
OVERRIDES_PATH = ROOT / "data" / "results_overrides.json"
ACCURACY_PATH = ROOT / "accuracy.html"
FIXTURE_CSV_URL = "https://fixturedownload.com/download/fifa-world-cup-2026-GMTStandardTime.csv"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"

TEAM_ALIASES = {
    "usa": "unitedstates",
    "us": "unitedstates",
    "unitedstates": "unitedstates",
    "unitedstatesofamerica": "unitedstates",
    "turkey": "turkiye",
    "turkiye": "turkiye",
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


def parse_iso_to_beijing(value: str) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized).astimezone(BEIJING_TZ)
    except ValueError:
        return None


def fixture(
    *,
    source: str,
    priority: int,
    date_beijing: str,
    time_beijing: str,
    home_team: str,
    away_team: str,
    score: tuple[int, int] | None,
    status: str,
    completed: bool,
    source_url: str,
    note: str = "",
) -> dict[str, Any]:
    return {
        "source": source,
        "priority": priority,
        "date_beijing": date_beijing,
        "time_beijing": time_beijing,
        "home_team": home_team,
        "away_team": away_team,
        "score": score,
        "status": status,
        "completed": completed,
        "source_url": source_url,
        "note": note,
        "pair": team_pair(home_team, away_team),
    }


def load_manual_overrides() -> list[dict[str, Any]]:
    if not OVERRIDES_PATH.exists():
        return []

    data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    rows = []
    for item in data.get("results", []):
        home = str(item.get("home_team", "")).strip()
        away = str(item.get("away_team", "")).strip()
        if not home or not away:
            continue
        score = parse_result(str(item.get("actual_score", "")))
        rows.append(
            fixture(
                source="手动覆盖",
                priority=0,
                date_beijing=str(item.get("date_beijing", "")),
                time_beijing=str(item.get("time_beijing", "")),
                home_team=home,
                away_team=away,
                score=score,
                status="已手动确认" if score else "手动覆盖待比分",
                completed=score is not None,
                source_url=str(item.get("source_url", "")),
                note=str(item.get("note", "")),
            )
        )
    return rows


def load_espn_results(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dates = set()
    for prediction in predictions:
        for date_value in nearby_dates(str(prediction.get("date_beijing", "")), days=1):
            dates.add(date_value.replace("-", ""))

    rows = []
    for date_value in sorted(dates):
        response = requests.get(ESPN_SCOREBOARD_URL, params={"dates": date_value}, timeout=30)
        response.raise_for_status()
        data = response.json()
        for event in data.get("events", []):
            competition = (event.get("competitions") or [{}])[0]
            competitors = competition.get("competitors", [])
            home = next((item for item in competitors if item.get("homeAway") == "home"), None)
            away = next((item for item in competitors if item.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            kickoff_bj = parse_iso_to_beijing(str(event.get("date", "")))
            if not kickoff_bj:
                continue

            status_type = event.get("status", {}).get("type", {})
            completed = bool(status_type.get("completed"))
            status_text = str(status_type.get("description") or status_type.get("shortDetail") or "Unknown")
            home_score = str(home.get("score", "")).strip()
            away_score = str(away.get("score", "")).strip()
            score = None
            if completed and home_score.isdigit() and away_score.isdigit():
                score = (int(home_score), int(away_score))

            rows.append(
                fixture(
                    source="ESPN",
                    priority=1,
                    date_beijing=kickoff_bj.date().isoformat(),
                    time_beijing=kickoff_bj.strftime("%H:%M"),
                    home_team=str(home.get("team", {}).get("displayName", "")),
                    away_team=str(away.get("team", {}).get("displayName", "")),
                    score=score,
                    status=status_text,
                    completed=completed,
                    source_url=(event.get("links") or [{}])[0].get("href", ""),
                    note="ESPN 显示 completed=false 时不会用 0-0 结算。",
                )
            )
    return rows


def load_fixture_download_results() -> list[dict[str, Any]]:
    response = requests.get(FIXTURE_CSV_URL, timeout=30)
    response.raise_for_status()
    rows = []

    for row in csv.DictReader(io.StringIO(response.text)):
        raw_date = (row.get("Date") or "").strip()
        try:
            kickoff_utc = dt.datetime.strptime(raw_date, "%d/%m/%Y %H:%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue

        kickoff_bj = kickoff_utc.astimezone(BEIJING_TZ)
        score = parse_result(row.get("Result", ""))
        rows.append(
            fixture(
                source="FixtureDownload",
                priority=2,
                date_beijing=kickoff_bj.date().isoformat(),
                time_beijing=kickoff_bj.strftime("%H:%M"),
                home_team=(row.get("Home Team") or "").strip(),
                away_team=(row.get("Away Team") or "").strip(),
                score=score,
                status="已给出比分" if score else "赛程已找到，比分为空",
                completed=score is not None,
                source_url=FIXTURE_CSV_URL,
                note="FixtureDownload CSV 更新可能滞后。",
            )
        )
    return rows


def load_results(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    fixtures = []
    fixtures.extend(load_manual_overrides())
    fixtures.extend(load_espn_results(predictions))
    fixtures.extend(load_fixture_download_results())
    return {
        "fetched_at": dt.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M"),
        "fixtures": fixtures,
    }


def find_fixture(prediction: dict[str, Any], fixtures: list[dict[str, Any]]) -> dict[str, Any] | None:
    pred_pair = team_pair(prediction["home_team"], prediction["away_team"])
    allowed_dates = nearby_dates(str(prediction["date_beijing"]), days=1)
    pair_matches = [item for item in fixtures if item["pair"] == pred_pair]

    dated_matches = [item for item in pair_matches if item["date_beijing"] in allowed_dates]
    candidates = dated_matches or pair_matches
    if not candidates:
        return None

    completed = [item for item in candidates if item["completed"] and item["score"]]
    if completed:
        return sorted(completed, key=lambda item: item["priority"])[0]
    return sorted(candidates, key=lambda item: item["priority"])[0]


def evaluate_prediction(prediction: dict[str, Any], fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    result = find_fixture(prediction, fixtures)
    row = dict(prediction)
    row["status"] = "pending"
    row["actual_score"] = ""
    row["actual_1x2"] = ""
    row["hit_1x2"] = None
    row["hit_primary_score"] = None
    row["hit_score_candidate"] = None
    row["source_name"] = ""
    row["source_url"] = ""
    row["source_date"] = ""
    row["source_time_beijing"] = ""

    if not result:
        row["status_note"] = "公开赛果源暂未找到该场"
        return row

    row["source_name"] = result["source"]
    row["source_url"] = result["source_url"]
    row["source_date"] = str(result["date_beijing"])
    row["source_time_beijing"] = str(result["time_beijing"])

    if not result.get("score"):
        row["status_note"] = f"{result['source']}：{result['status']}。{result.get('note', '')}".strip()
        return row

    home_goals, away_goals = result["score"]
    actual_score = f"{home_goals}-{away_goals}"
    actual_1x2 = side_from_score(home_goals, away_goals)
    candidates = [str(item).strip() for item in prediction.get("score_candidates", [])]

    row["status"] = "finished"
    row["status_note"] = f"已结算，来源：{result['source']}"
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


def source_link(row: dict[str, Any]) -> str:
    source_name = row.get("source_name") or "-"
    source_url = row.get("source_url") or ""
    if not source_url:
        return html.escape(source_name)
    return f'<a href="{html.escape(source_url)}">{html.escape(source_name)}</a>'


def build_html(rows: list[dict[str, Any]], fetched_at: str) -> str:
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
        <td>{report_link}<br><span>{source_link(row)}</span></td>
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
.page{{max-width:1180px;margin:0 auto}}
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
.note{{margin-top:14px;color:#666;font-size:12px;line-height:1.7}}
@media(max-width:820px){{.top{{display:block}}.updated{{margin-top:8px}}.cards{{grid-template-columns:1fr 1fr}}.panel{{overflow:auto}}table{{min-width:1060px}}}}
</style>
</head>
<body>
<main class="page">
  <header class="top">
    <div>
      <h1>世界杯预测准确率</h1>
      <div class="sub">对比预测方向、主比分、候选比分池与赛后结果。赛果优先级：手动覆盖 > ESPN 完赛数据 > FixtureDownload。</div>
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
    待赛果比赛：{len(pending)} 场。本页抓取时间：北京时间 {html.escape(fetched_at)}。如果 ESPN 仍显示 Scheduled 或 FixtureDownload 比分为空，本页不会把 0-0 当作完赛比分。
    如果你已经有最终比分，可写入 data/results_overrides.json 后重新运行脚本结算。
  </div>
</main>
</body>
</html>
"""


def main() -> int:
    data = json.loads(PREDICTIONS_PATH.read_text(encoding="utf-8"))
    predictions = data.get("predictions", [])
    result_data = load_results(predictions)
    rows = [evaluate_prediction(item, result_data["fixtures"]) for item in predictions]
    ACCURACY_PATH.write_text(build_html(rows, result_data["fetched_at"]), encoding="utf-8")
    print(f"Wrote {ACCURACY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
