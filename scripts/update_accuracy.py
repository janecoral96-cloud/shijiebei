#!/usr/bin/env python3
"""
Build accuracy.html by comparing stored predictions and market picks with results.

Result priority:
1. data/results_overrides.json for verified manual scores.
2. ESPN scoreboard/summary API when a match is marked completed.
3. FixtureDownload CSV as a fallback score feed.
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
from requests import RequestException


ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_PATH = ROOT / "data" / "predictions.json"
OVERRIDES_PATH = ROOT / "data" / "results_overrides.json"
ACCURACY_PATH = ROOT / "accuracy.html"
REPORTS_DIR = ROOT / "reports"
FIXTURE_CSV_URL = "https://fixturedownload.com/download/fifa-world-cup-2026-GMTStandardTime.csv"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 accuracy-builder/1.0"}

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
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BEIJING_TZ)
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
    halftime_score: tuple[int, int] | None = None,
    event_id: str = "",
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
        "halftime_score": halftime_score,
        "status": status,
        "completed": completed,
        "source_url": source_url,
        "event_id": event_id,
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
        halftime_score = parse_result(str(item.get("halftime_score", "")))
        rows.append(
            fixture(
                source="手动覆盖",
                priority=0,
                date_beijing=str(item.get("date_beijing", "")),
                time_beijing=str(item.get("time_beijing", "")),
                home_team=home,
                away_team=away,
                score=score,
                halftime_score=halftime_score,
                status="已手动确认" if score else "手动覆盖待比分",
                completed=score is not None,
                source_url=str(item.get("source_url", "")),
                note=str(item.get("note", "")),
            )
        )
    return rows


def halftime_from_summary(event_id: str) -> tuple[int, int] | None:
    if not event_id:
        return None
    response = requests.get(ESPN_SUMMARY_URL, params={"event": event_id}, headers=REQUEST_HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()
    competition = (data.get("header", {}).get("competitions") or [{}])[0]
    competitors = competition.get("competitors", [])
    home = next((item for item in competitors if item.get("homeAway") == "home"), None)
    away = next((item for item in competitors if item.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    try:
        return (
            int(home.get("linescores", [])[0]["displayValue"]),
            int(away.get("linescores", [])[0]["displayValue"]),
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def safe_halftime_from_summary(event_id: str) -> tuple[int, int] | None:
    try:
        return halftime_from_summary(event_id)
    except RequestException as exc:
        print(f"Warning: ESPN summary fetch failed for {event_id}: {exc}")
        return None


def load_espn_results(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dates = set()
    for prediction in predictions:
        for date_value in nearby_dates(str(prediction.get("date_beijing", "")), days=1):
            dates.add(date_value.replace("-", ""))

    rows = []
    for date_value in sorted(dates):
        try:
            response = requests.get(
                ESPN_SCOREBOARD_URL,
                params={"dates": date_value},
                headers=REQUEST_HEADERS,
                timeout=30,
            )
            response.raise_for_status()
        except RequestException as exc:
            print(f"Warning: ESPN scoreboard fetch failed for {date_value}: {exc}")
            continue
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
            event_id = str(event.get("id", ""))
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
                    halftime_score=safe_halftime_from_summary(event_id) if completed else None,
                    status=status_text,
                    completed=completed,
                    source_url=(event.get("links") or [{}])[0].get("href", ""),
                    event_id=event_id,
                    note="ESPN 显示 completed=false 时不会用 0-0 结算。",
                )
            )
    return rows


def load_fixture_download_results() -> list[dict[str, Any]]:
    response = requests.get(FIXTURE_CSV_URL, headers=REQUEST_HEADERS, timeout=30)
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


def actual_asian_result(score: tuple[int, int], team: str, line: float) -> str:
    home_goals, away_goals = score
    margin = home_goals - away_goals if team == "home" else away_goals - home_goals
    adjusted = margin + line

    if line % 0.5 == 0:
        if adjusted > 0:
            return "hit"
        if adjusted == 0:
            return "push"
        return "miss"

    lower = line - 0.25 if line > 0 else line - 0.25
    upper = line + 0.25 if line > 0 else line + 0.25
    first = actual_asian_result(score, team, lower)
    second = actual_asian_result(score, team, upper)
    if first == second:
        return first
    if "hit" in (first, second) and "push" in (first, second):
        return "half_hit"
    if "miss" in (first, second) and "push" in (first, second):
        return "half_miss"
    return "push"


def evaluate_market(market: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(market)
    row["result"] = "pending"
    row["actual"] = "待赛果"

    if not result or not result.get("score"):
        return row

    score = result["score"]
    home_goals, away_goals = score
    total_goals = home_goals + away_goals
    row["result"] = "miss"

    kind = market.get("market")
    if kind == "1x2":
        actual = side_from_score(home_goals, away_goals)
        row["actual"] = label_1x2(actual)
        row["result"] = "hit" if market.get("pick") == actual else "miss"
    elif kind == "total":
        line = float(market.get("line", 0))
        row["actual"] = f"总进球 {total_goals}"
        if total_goals == line:
            row["result"] = "push"
        elif market.get("pick") == "over":
            row["result"] = "hit" if total_goals > line else "miss"
        else:
            row["result"] = "hit" if total_goals < line else "miss"
    elif kind == "btts":
        actual_yes = home_goals > 0 and away_goals > 0
        row["actual"] = "是" if actual_yes else "否"
        row["result"] = "hit" if (market.get("pick") == "yes") == actual_yes else "miss"
    elif kind == "asian_handicap":
        line = float(market.get("line", 0))
        team = str(market.get("team", "home"))
        row["actual"] = f"{market.get('team', '')} {line:+g} 后比分差"
        row["result"] = actual_asian_result(score, team, line)
    elif kind == "half_1x2":
        halftime = result.get("halftime_score")
        if halftime is None:
            row["actual"] = "缺半场数据"
            row["result"] = "pending"
        else:
            actual = side_from_score(*halftime)
            row["actual"] = f"半场 {halftime[0]}-{halftime[1]}，{label_1x2(actual)}"
            row["result"] = "hit" if market.get("pick") == actual else "miss"
    elif kind == "half_full":
        halftime = result.get("halftime_score")
        if halftime is None:
            row["actual"] = "缺半场数据"
            row["result"] = "pending"
        else:
            half = side_from_score(*halftime)
            full = side_from_score(home_goals, away_goals)
            row["actual"] = f"{label_1x2(half)} / {label_1x2(full)}"
            row["result"] = "hit" if market.get("half") == half and market.get("full") == full else "miss"
    elif kind == "second_half_1x2":
        halftime = result.get("halftime_score")
        if halftime is None:
            row["actual"] = "缺半场数据"
            row["result"] = "pending"
        else:
            second_half = (home_goals - halftime[0], away_goals - halftime[1])
            actual = side_from_score(*second_half)
            row["actual"] = f"下半场 {second_half[0]}-{second_half[1]}，{label_1x2(actual)}"
            row["result"] = "hit" if market.get("pick") == actual else "miss"
    elif kind == "win_to_nil":
        team = str(market.get("team", "home"))
        actual = side_from_score(home_goals, away_goals)
        opponent_goals = away_goals if team == "home" else home_goals
        row["actual"] = f"{label_1x2(actual)}，对手进球 {opponent_goals}"
        row["result"] = "hit" if actual == team and opponent_goals == 0 else "miss"
    else:
        row["actual"] = market.get("note", "缺少可自动核对的数据")
        row["result"] = "pending"

    return row


def evaluate_prediction(prediction: dict[str, Any], fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    result = find_fixture(prediction, fixtures)
    row = dict(prediction)
    row["status"] = "pending"
    row["actual_score"] = ""
    row["actual_1x2"] = ""
    row["actual_total"] = None
    row["actual_btts"] = ""
    row["halftime_score"] = ""
    row["hit_1x2"] = None
    row["hit_primary_score"] = None
    row["hit_score_candidate"] = None
    row["source_name"] = ""
    row["source_url"] = ""
    row["source_date"] = ""
    row["source_time_beijing"] = ""

    if not result:
        row["status_note"] = "公开赛果源暂未找到该场"
        row["markets_evaluated"] = [evaluate_market(item, None) for item in row.get("markets", [])]
        return row

    row["source_name"] = result["source"]
    row["source_url"] = result["source_url"]
    row["source_date"] = str(result["date_beijing"])
    row["source_time_beijing"] = str(result["time_beijing"])

    if not result.get("score"):
        row["status_note"] = f"{result['source']}：{result['status']}。{result.get('note', '')}".strip()
        row["markets_evaluated"] = [evaluate_market(item, result) for item in row.get("markets", [])]
        return row

    home_goals, away_goals = result["score"]
    actual_score = f"{home_goals}-{away_goals}"
    actual_1x2 = side_from_score(home_goals, away_goals)
    candidates = [str(item).strip() for item in prediction.get("score_candidates", [])]
    halftime = result.get("halftime_score")

    row["status"] = "finished"
    row["status_note"] = f"已结算，来源：{result['source']}"
    row["actual_score"] = actual_score
    row["actual_1x2"] = actual_1x2
    row["actual_total"] = home_goals + away_goals
    row["actual_btts"] = "yes" if home_goals > 0 and away_goals > 0 else "no"
    row["halftime_score"] = "" if halftime is None else f"{halftime[0]}-{halftime[1]}"
    row["hit_1x2"] = prediction.get("pick_1x2") == actual_1x2
    row["hit_primary_score"] = prediction.get("primary_score") == actual_score
    row["hit_score_candidate"] = actual_score in candidates
    row["markets_evaluated"] = [evaluate_market(item, result) for item in row.get("markets", [])]
    return row


def pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "-"
    return f"{numerator / denominator * 100:.1f}%"


def label_1x2(value: str) -> str:
    return {"home": "主胜", "draw": "平局", "away": "客胜"}.get(value, value or "-")


def result_label(value: str | None) -> str:
    return {
        "hit": "命中",
        "miss": "未中",
        "push": "走水",
        "half_hit": "半赢",
        "half_miss": "半输",
        "pending": "-",
    }.get(value or "pending", "-")


def result_class(value: str | None) -> str:
    return {
        "hit": "hit",
        "miss": "miss",
        "push": "push",
        "half_hit": "hit",
        "half_miss": "miss",
        "pending": "muted",
    }.get(value or "pending", "muted")


def source_link(row: dict[str, Any]) -> str:
    source_name = row.get("source_name") or "-"
    source_url = row.get("source_url") or ""
    if not source_url:
        return html.escape(source_name)
    return f'<a href="{html.escape(source_url)}">{html.escape(source_name)}</a>'


def market_stats(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    settled = 0
    hits = 0
    pushes = 0
    for row in rows:
        for market in row.get("markets_evaluated", []):
            result = market.get("result")
            if result in {"hit", "miss", "push", "half_hit", "half_miss"}:
                settled += 1
            if result in {"hit", "half_hit"}:
                hits += 1
            if result == "push":
                pushes += 1
    return settled, hits, pushes


def build_market_grid(row: dict[str, Any]) -> str:
    markets = row.get("markets_evaluated", [])
    if not markets:
        return '<div class="market-empty">暂无盘口建议</div>'
    cards = []
    for market in markets:
        cards.append(
            f"""
        <div class="market-card {result_class(market.get("result"))}">
          <div class="market-top">
            <span>{html.escape(str(market.get("label", market.get("market", ""))))}</span>
            <strong>{result_label(market.get("result"))}</strong>
          </div>
          <div class="market-pick">{html.escape(str(market.get("display", "")))}</div>
          <div class="market-actual">实际：{html.escape(str(market.get("actual", "-")))}</div>
        </div>
            """.strip()
        )
    return "\n".join(cards)


def report_id(report_path: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in report_path.lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return f"report-{cleaned.strip('-')}"


def report_source_label(report_path: str) -> str:
    name = Path(report_path).stem.lower()
    if "claude" in name:
        return "Claude生成"
    if name >= "2026-06-19":
        return "Codex生成"
    return "历史报告"


def report_date_label(report_path: str) -> tuple[str, str]:
    stem = Path(report_path).stem
    raw_date = stem[:10]
    try:
        date_value = dt.date.fromisoformat(raw_date)
    except ValueError:
        return stem, ""
    weekdays = "周一 周二 周三 周四 周五 周六 周日".split()
    return f"{date_value.month}.{date_value.day}", f"{date_value.year}年{date_value.month}月{date_value.day}日 · {weekdays[date_value.weekday()]}"


def report_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [row for row in rows if row["status"] == "finished"]
    total = len(finished)
    hit_1x2 = sum(1 for row in finished if row["hit_1x2"])
    hit_primary = sum(1 for row in finished if row["hit_primary_score"])
    hit_candidate = sum(1 for row in finished if row["hit_score_candidate"])
    settled_markets, hit_markets, push_markets = market_stats(rows)
    return {
        "finished": total,
        "pending": sum(1 for row in rows if row["status"] == "pending"),
        "hit_1x2": hit_1x2,
        "hit_primary": hit_primary,
        "hit_candidate": hit_candidate,
        "settled_markets": settled_markets,
        "hit_markets": hit_markets,
        "push_markets": push_markets,
        "pct_1x2": pct(hit_1x2, total),
        "pct_primary": pct(hit_primary, total),
        "pct_candidate": pct(hit_candidate, total),
        "pct_market": pct(hit_markets, settled_markets),
    }


def build_report_infos(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_report: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        report_path = str(row.get("report", "")).replace("\\", "/")
        if report_path:
            by_report.setdefault(report_path, []).append(row)

    report_paths = set(by_report)
    if REPORTS_DIR.exists():
        for path in REPORTS_DIR.glob("*.html"):
            report_paths.add(f"reports/{path.name}")

    infos = []
    for report_path in report_paths:
        rows_for_report = by_report.get(report_path, [])
        short_date, long_date = report_date_label(report_path)
        source_label = report_source_label(report_path)
        stats = report_stats(rows_for_report)
        infos.append(
            {
                "id": report_id(report_path),
                "path": report_path,
                "short_date": short_date,
                "long_date": long_date,
                "source_label": source_label,
                "title": f"{long_date or short_date} · {source_label}",
                "rows": rows_for_report,
                "stats": stats,
                "source_order": 1 if source_label.startswith("Claude") else 0,
                "date_sort": Path(report_path).stem[:10],
            }
        )

    infos.sort(key=lambda item: (item["date_sort"], -item["source_order"]), reverse=True)
    return infos


def build_html(rows: list[dict[str, Any]], fetched_at: str) -> str:
    finished = [row for row in rows if row["status"] == "finished"]
    pending = [row for row in rows if row["status"] == "pending"]
    total = len(finished)
    hit_1x2 = sum(1 for row in finished if row["hit_1x2"])
    hit_primary = sum(1 for row in finished if row["hit_primary_score"])
    hit_candidate = sum(1 for row in finished if row["hit_score_candidate"])
    settled_markets, hit_markets, push_markets = market_stats(rows)
    updated = dt.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    report_infos = build_report_infos(rows)

    def build_match_card(row: dict[str, Any]) -> str:
        actual_score = row["actual_score"] or "待赛果"
        source_time = ""
        if row.get("source_date"):
            source_time = f'{html.escape(row["source_date"])} {html.escape(row.get("source_time_beijing", ""))}'
        return f"""
    <article class="match-card {row["status"]}">
      <div class="match-head">
        <div>
          <div class="match-date">{html.escape(row["date_beijing"])} {html.escape(row.get("time_beijing", ""))} · {html.escape(row.get("group", ""))}</div>
          <h2>{html.escape(row["home_team"])} vs {html.escape(row["away_team"])}</h2>
        </div>
        <div class="score-box">
          <span>实际比分</span>
          <strong>{html.escape(actual_score)}</strong>
        </div>
      </div>
      <div class="quick-row">
        <div><span>预测方向</span><strong>{label_1x2(str(row.get("pick_1x2", "")))}</strong><em class="{result_class("hit" if row["hit_1x2"] else "miss" if row["hit_1x2"] is not None else "pending")}">{result_label("hit" if row["hit_1x2"] else "miss" if row["hit_1x2"] is not None else "pending")}</em></div>
        <div><span>主比分</span><strong>{html.escape(str(row.get("primary_score", "")))}</strong><em class="{result_class("hit" if row["hit_primary_score"] else "miss" if row["hit_primary_score"] is not None else "pending")}">{result_label("hit" if row["hit_primary_score"] else "miss" if row["hit_primary_score"] is not None else "pending")}</em></div>
        <div><span>候选比分</span><strong>{html.escape(", ".join(row.get("score_candidates", [])))}</strong><em class="{result_class("hit" if row["hit_score_candidate"] else "miss" if row["hit_score_candidate"] is not None else "pending")}">{result_label("hit" if row["hit_score_candidate"] else "miss" if row["hit_score_candidate"] is not None else "pending")}</em></div>
      </div>
      <div class="facts">
        <span>半场：{html.escape(row.get("halftime_score") or "-")}</span>
        <span>总进球：{html.escape(str(row.get("actual_total") if row.get("actual_total") is not None else "-"))}</span>
        <span>BTTS：{html.escape({"yes": "是", "no": "否"}.get(row.get("actual_btts", ""), "-"))}</span>
        <span>来源：{source_link(row)}</span>
        <span>源时间：{source_time or "-"}</span>
        <a href="{html.escape(row.get("report", ""))}">报告</a>
      </div>
      <p class="status-note">{html.escape(row.get("status_note", ""))}</p>
      <div class="markets">
        {build_market_grid(row)}
      </div>
    </article>
            """.strip()

    report_cards = []
    report_panels = []
    for index, info in enumerate(report_infos):
        stats = info["stats"]
        active = " active" if index == 0 else ""
        data_state = "empty" if not info["rows"] else "ready"
        report_cards.append(
            f"""
    <a class="report-card{active}" href="#{html.escape(info["id"])}" data-target="{html.escape(info["id"])}">
      <span class="report-date">{html.escape(info["short_date"])}</span>
      <span class="report-meta">{html.escape(info["source_label"])}</span>
      <strong>{html.escape(info["long_date"] or info["short_date"])}</strong>
      <span class="report-line">{len(info["rows"])} 场预测 · 已结算 {stats["finished"]} 场</span>
      <span class="report-line">1X2 {stats["pct_1x2"]} · 盘口 {stats["pct_market"]}</span>
    </a>
            """.strip()
        )

        if info["rows"]:
            panel_body = "\n".join(build_match_card(row) for row in info["rows"])
        else:
            panel_body = f"""
      <div class="empty-state">
        <strong>这个 HTML 报告还没有结构化预测记录。</strong>
        <span>可以先打开原报告查看内容；后续把该报告的胜平负、比分、大小球、让球、BTTS 等录入 data/predictions.json 后，这里会自动结算。</span>
      </div>
            """.strip()

        report_panels.append(
            f"""
  <section class="report-panel{active}" id="{html.escape(info["id"])}" data-state="{data_state}">
    <div class="report-head">
      <div>
        <div class="match-date">{html.escape(info["title"])}</div>
        <h2>{html.escape(info["title"])}</h2>
      </div>
      <a class="open-report" href="{html.escape(info["path"])}">打开原报告</a>
    </div>
    <div class="report-metrics">
      <div><span>预测场次</span><strong>{len(info["rows"])}</strong></div>
      <div><span>已结算</span><strong>{stats["finished"]}</strong></div>
      <div><span>1X2</span><strong>{stats["pct_1x2"]}</strong></div>
      <div><span>主比分</span><strong>{stats["pct_primary"]}</strong></div>
      <div><span>候选比分</span><strong>{stats["pct_candidate"]}</strong></div>
      <div><span>盘口</span><strong>{stats["pct_market"]}</strong></div>
    </div>
    <div class="match-list">
      {panel_body}
    </div>
  </section>
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
.sub,.updated,.match-date,.facts,.status-note,.market-actual{{font-size:12px;color:#666}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:18px}}
.metric{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px}}
.metric .label{{font-size:12px;color:#777;margin-bottom:4px}}
.metric .value{{font-size:25px;font-weight:800;color:#1a5fa8}}
.report-picker{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:16px 0 18px}}
.report-card{{display:flex;flex-direction:column;gap:5px;min-height:132px;background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px;color:inherit;text-decoration:none;transition:border-color .15s ease,box-shadow .15s ease,transform .15s ease}}
.report-card:hover,.report-card.active{{border-color:#1a5fa8;box-shadow:0 10px 22px rgba(0,0,0,.08);transform:translateY(-1px)}}
.report-date{{font-size:28px;font-weight:900;color:#1a5fa8;line-height:1}}
.report-meta{{width:max-content;border-radius:999px;background:#e8f2fc;color:#0d4e9e;font-size:11px;font-weight:800;padding:2px 8px}}
.report-line{{font-size:12px;color:#666}}
.report-panel{{display:none}}
.report-panel.active{{display:block}}
.report-head{{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;margin-bottom:12px}}
.open-report{{white-space:nowrap}}
.report-metrics{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:12px}}
.report-metrics div{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px}}
.report-metrics span{{display:block;font-size:11px;color:#777}}
.report-metrics strong{{display:block;font-size:20px;color:#1a5fa8;margin-top:3px}}
.empty-state{{background:#fff;border:1px dashed #ccc;border-radius:8px;padding:18px;color:#666;display:flex;flex-direction:column;gap:6px}}
.match-list{{display:flex;flex-direction:column;gap:14px}}
.match-card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px}}
.match-card.pending{{color:#666}}
.match-head{{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;border-bottom:1px solid #eee;padding-bottom:12px;margin-bottom:12px}}
h2{{font-size:18px;line-height:1.25;margin-top:2px}}
.score-box{{min-width:96px;text-align:right}}
.score-box span{{display:block;font-size:11px;color:#777}}
.score-box strong{{font-size:24px;color:#111}}
.quick-row{{display:grid;grid-template-columns:1fr 1fr 1.4fr;gap:10px;margin-bottom:10px}}
.quick-row div{{background:#f8f7f4;border-radius:8px;padding:10px}}
.quick-row span{{display:block;font-size:11px;color:#777}}
.quick-row strong{{display:block;font-size:13px;margin:2px 0}}
em{{font-style:normal;font-size:12px;font-weight:800}}
.facts{{display:flex;flex-wrap:wrap;gap:8px 14px;margin:8px 0}}
a{{color:#1a5fa8;text-decoration:none;font-weight:700}}
.markets{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-top:12px}}
.market-card{{border:1px solid #e3e0d8;border-radius:8px;padding:10px;background:#fbfaf7}}
.market-top{{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:#666;margin-bottom:5px}}
.market-top strong{{font-size:12px}}
.market-pick{{font-size:13px;font-weight:750;margin-bottom:4px}}
.hit{{color:#137333}}
.miss{{color:#b3261e}}
.push{{color:#8a5a00}}
.muted{{color:#777}}
.market-card.hit{{border-color:#b8d8bd;background:#f4fbf5}}
.market-card.miss{{border-color:#edc0bd;background:#fff7f6}}
.market-card.push{{border-color:#ead29a;background:#fffaf0}}
.note{{margin-top:14px;color:#666;font-size:12px;line-height:1.7}}
@media(max-width:900px){{.top,.match-head,.report-head{{display:block}}.updated,.score-box{{margin-top:8px;text-align:left}}.cards{{grid-template-columns:1fr 1fr}}.quick-row,.report-metrics{{grid-template-columns:1fr}}.open-report{{display:inline-block;margin-top:8px}}}}
</style>
</head>
<body>
<main class="page">
  <header class="top">
    <div>
      <h1>世界杯预测准确率</h1>
      <div class="sub">复盘胜平负、比分、大小球、双方进球、亚洲让球、半场/半全场等推荐。赛果优先级：手动覆盖 > ESPN 完赛数据 > FixtureDownload。</div>
    </div>
    <div class="updated">北京时间 {updated} 更新</div>
  </header>
  <section class="cards">
    <div class="metric"><div class="label">已结算比赛</div><div class="value">{total}</div></div>
    <div class="metric"><div class="label">1X2 命中率</div><div class="value">{pct(hit_1x2, total)}</div></div>
    <div class="metric"><div class="label">主比分命中率</div><div class="value">{pct(hit_primary, total)}</div></div>
    <div class="metric"><div class="label">候选比分命中率</div><div class="value">{pct(hit_candidate, total)}</div></div>
    <div class="metric"><div class="label">盘口建议命中率</div><div class="value">{pct(hit_markets, settled_markets)}</div></div>
  </section>

  <section class="report-picker" aria-label="选择报告">
    {"".join(report_cards)}
  </section>

  {"".join(report_panels)}

  <div class="note">
    全站已结算盘口建议：{settled_markets} 条；命中：{hit_markets} 条；走水：{push_markets} 条；待赛果比赛：{len(pending)} 场。本页抓取时间：北京时间 {html.escape(fetched_at)}。
    如果 ESPN 仍显示 Scheduled 或 FixtureDownload 比分为空，本页不会把 0-0 当作完赛比分。
  </div>
</main>
<script>
const cards = Array.from(document.querySelectorAll('.report-card'));
const panels = Array.from(document.querySelectorAll('.report-panel'));
function showReport(id) {{
  cards.forEach(card => card.classList.toggle('active', card.dataset.target === id));
  panels.forEach(panel => panel.classList.toggle('active', panel.id === id));
}}
cards.forEach(card => {{
  card.addEventListener('click', event => {{
    event.preventDefault();
    const id = card.dataset.target;
    showReport(id);
    history.replaceState(null, '', '#' + id);
  }});
}});
if (location.hash) {{
  const id = location.hash.slice(1);
  if (document.getElementById(id)) showReport(id);
}}
</script>
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
