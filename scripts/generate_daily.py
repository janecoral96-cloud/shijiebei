#!/usr/bin/env python3
"""
Generate the latest daily World Cup prediction report for GitHub Pages.

Data pipeline:
1. FixtureDownload CSV is the primary 2026 World Cup schedule source.
2. Kalshi and optional The Odds API data are added when accessible.
3. Google News RSS snippets are collected for each fixture.
4. DeepSeek turns structured data into a styled HTML report matching the
   existing report style. A deterministic fallback report is generated if the
   model is unavailable.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
INDEX_PATH = REPO_ROOT / "index.html"

try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")

UTC = dt.timezone.utc

FIXTURE_CSV_URL = "https://fixturedownload.com/download/fifa-world-cup-2026-GMTStandardTime.csv"
KALSHI_MARKETS_URL = "https://trading-api.kalshi.com/trade-api/v2/markets?event_ticker=WORLDCUP&limit=200"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


@dataclass
class SourceStatus:
    name: str
    ok: bool
    detail: str
    url: str = ""


@dataclass
class Fixture:
    match_number: str
    round_number: str
    date_utc: str
    date_beijing: str
    time_beijing: str
    home_team: str
    away_team: str
    group: str
    venue: str
    result: str


def http_get(url: str, timeout: int = 20) -> requests.Response:
    return requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; WorldCupReportBot/1.0)",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        timeout=timeout,
    )


def parse_fixture_time(raw: str) -> dt.datetime:
    return dt.datetime.strptime(raw.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=UTC)


def load_fixtures(statuses: list[SourceStatus]) -> list[Fixture]:
    try:
        response = http_get(FIXTURE_CSV_URL)
        response.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(response.text)))
    except Exception as exc:
        statuses.append(SourceStatus("FixtureDownload", False, str(exc), FIXTURE_CSV_URL))
        return []

    fixtures: list[Fixture] = []
    for row in rows:
        try:
            kickoff_utc = parse_fixture_time(row["Date"])
        except Exception:
            continue
        kickoff_bj = kickoff_utc.astimezone(BEIJING_TZ)
        home = (row.get("Home Team") or "").strip()
        away = (row.get("Away Team") or "").strip()
        if not home or not away or home.upper() == "TBD" or away.upper() == "TBD":
            continue
        fixtures.append(
            Fixture(
                match_number=(row.get("Match Number") or "").strip(),
                round_number=(row.get("Round Number") or "").strip(),
                date_utc=kickoff_utc.isoformat(),
                date_beijing=kickoff_bj.date().isoformat(),
                time_beijing=kickoff_bj.strftime("%H:%M"),
                home_team=home,
                away_team=away,
                group=(row.get("Group") or "").strip(),
                venue=(row.get("Location") or "").strip(),
                result=(row.get("Result") or "").strip(),
            )
        )

    statuses.append(SourceStatus("FixtureDownload", True, f"{len(fixtures)} fixtures", FIXTURE_CSV_URL))
    return fixtures


def requested_target_date(fixtures: list[Fixture]) -> str:
    raw = (os.environ.get("TARGET_DATE") or "").strip()
    if len(sys.argv) > 1:
        raw = sys.argv[1].strip()
    if raw:
        dt.date.fromisoformat(raw)
        return raw

    today = dt.datetime.now(BEIJING_TZ).date()
    available = sorted({dt.date.fromisoformat(item.date_beijing) for item in fixtures if not item.result})
    for day in available:
        if day >= today:
            return day.isoformat()
    if available:
        return available[-1].isoformat()
    return today.isoformat()


def fixtures_for_date(fixtures: list[Fixture], date_str: str, include_finished: bool = False) -> list[Fixture]:
    selected = [
        item
        for item in fixtures
        if item.date_beijing == date_str and (include_finished or not item.result)
    ]
    selected.sort(key=lambda item: (item.time_beijing, item.match_number))
    return selected


def collect_kalshi(statuses: list[SourceStatus]) -> list[dict[str, Any]]:
    try:
        response = http_get(KALSHI_MARKETS_URL)
        response.raise_for_status()
        data = response.json()
        markets = data.get("markets", [])
    except Exception as exc:
        statuses.append(SourceStatus("Kalshi", False, str(exc), KALSHI_MARKETS_URL))
        return []

    cleaned = [
        {
            "title": market.get("title", ""),
            "ticker": market.get("ticker", ""),
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "last_price": market.get("last_price"),
            "volume": market.get("volume"),
        }
        for market in markets
    ]
    statuses.append(SourceStatus("Kalshi", True, f"{len(cleaned)} markets", KALSHI_MARKETS_URL))
    return cleaned


def collect_odds_api(fixtures: list[Fixture], statuses: list[SourceStatus]) -> list[dict[str, Any]]:
    api_key = (os.environ.get("ODDS_API_KEY") or "").strip()
    if not api_key:
        statuses.append(SourceStatus("The Odds API", False, "ODDS_API_KEY not set"))
        return []

    url = (
        "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/"
        f"?apiKey={urllib.parse.quote(api_key)}&regions=eu,us,uk&markets=h2h,totals,btts&oddsFormat=decimal"
    )
    try:
        response = http_get(url)
        response.raise_for_status()
        raw_events = response.json()
    except Exception as exc:
        statuses.append(SourceStatus("The Odds API", False, str(exc), "https://the-odds-api.com/"))
        return []

    wanted = {(f.home_team.lower(), f.away_team.lower()) for f in fixtures}
    wanted |= {(f.away_team.lower(), f.home_team.lower()) for f in fixtures}
    events: list[dict[str, Any]] = []
    for event in raw_events:
        pair = (str(event.get("home_team", "")).lower(), str(event.get("away_team", "")).lower())
        if pair in wanted:
            events.append(event)

    statuses.append(SourceStatus("The Odds API", True, f"{len(events)} matching events", "https://the-odds-api.com/"))
    return events


def collect_news_for_fixture(fixture: Fixture) -> list[dict[str, str]]:
    query = f'"{fixture.home_team}" "{fixture.away_team}" World Cup 2026'
    encoded = urllib.parse.quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    try:
        response = http_get(url, timeout=12)
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except Exception:
        return []

    items: list[dict[str, str]] = []
    for item in root.findall(".//item")[:5]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "published": published})
    time.sleep(0.2)
    return items


def collect_news(fixtures: list[Fixture], statuses: list[SourceStatus]) -> dict[str, list[dict[str, str]]]:
    news: dict[str, list[dict[str, str]]] = {}
    for fixture in fixtures:
        key = f"{fixture.home_team} vs {fixture.away_team}"
        news[key] = collect_news_for_fixture(fixture)
    total = sum(len(items) for items in news.values())
    statuses.append(SourceStatus("Google News RSS", True, f"{total} articles", "https://news.google.com/rss"))
    return news


def collect_context(date_str: str, include_finished: bool = False) -> tuple[list[Fixture], dict[str, Any], list[SourceStatus]]:
    statuses: list[SourceStatus] = []
    all_fixtures = load_fixtures(statuses)
    target = date_str or requested_target_date(all_fixtures)
    selected = fixtures_for_date(all_fixtures, target, include_finished=include_finished)

    context = {
        "target_date": target,
        "timezone": "Asia/Shanghai",
        "generated_at_beijing": dt.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "kalshi_markets": collect_kalshi(statuses),
        "odds_events": collect_odds_api(selected, statuses),
        "news_by_match": collect_news(selected, statuses),
        "source_status": [],
    }
    context["source_status"] = [asdict(item) for item in statuses]
    return selected, context, statuses


def build_prompt(fixtures: list[Fixture], context: dict[str, Any]) -> str:
    target = context["target_date"]
    date_obj = dt.date.fromisoformat(target)
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][date_obj.weekday()]
    payload = {
        "date": target,
        "weekday": weekday,
        "timezone": "Asia/Shanghai",
        "fixtures": [asdict(item) for item in fixtures],
        "kalshi_markets": context["kalshi_markets"][:40],
        "odds_events": context["odds_events"],
        "news_by_match": context["news_by_match"],
        "source_status": context["source_status"],
    }

    return f"""
You are generating a Chinese static HTML betting-reference report for a 2026 FIFA World Cup website.

Use the same style and information density as the user's existing reports:
- background #f0ede8, white cards, primary blue #1a5fa8
- a schedule box, match tabs, one card per match, and a final selected-plan tab
- compact Chinese copy, no marketing hero, no external CSS or JS dependencies
- include JavaScript function show(i) for tab switching

Important rules:
- Directly output complete HTML only. No Markdown fences and no explanation.
- Use Simplified Chinese in UTF-8.
- Use only the structured facts below as current facts. If a data source is missing, explicitly label it as missing.
- Do not invent real-time injuries, lineups, or odds. If unavailable, write "未抓到可靠实时数据".
- This is a prediction/reference page, not financial advice.

For each match include:
1. Kickoff time in Beijing time, group/stage, venue.
2. Recent/news context from RSS titles when relevant.
3. Market context from Kalshi/Odds API when relevant.
4. 1X2 lean, top 4 exact scores with probabilities, confidence 1-10.
5. Five betting-reference cards: value, risk, total goals, correct score, upset/no-bet.
6. Key risk notes.

Final selected-plan tab:
- 4-5 selected ideas only when justified by the available data.
- If odds are missing, use "观察/不下注" language instead of pretending there is value.
- Include a 100-unit allocation only for ideas with enough support; otherwise allocate most units to "等待盘口".

Structured data:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()


def call_deepseek(prompt: str) -> str | None:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        print("DEEPSEEK_API_KEY not set; using fallback report.")
        return None
    if OpenAI is None:
        print("openai package unavailable; using fallback report.")
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "Output complete HTML only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.35,
            max_tokens=16000,
        )
        content = response.choices[0].message.content or ""
        print(f"DeepSeek returned {len(content)} characters.")
        return content
    except Exception as exc:
        print(f"DeepSeek failed: {exc}; using fallback report.")
        return None


def extract_html(raw: str | None) -> str | None:
    if not raw:
        return None
    content = raw.strip()
    content = re.sub(r"^```(?:html)?\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\s*```$", "", content)
    start = content.lower().find("<!doctype html")
    if start == -1:
        start = content.lower().find("<html")
    if start > 0:
        content = content[start:]
    lower = content.lower()
    if "<html" not in lower or "</html>" not in lower:
        return None
    if "charset" not in lower:
        content = content.replace("<head>", '<head>\n<meta charset="UTF-8">', 1)
    return content


def pseudo_probabilities(fixture: Fixture) -> dict[str, Any]:
    seed = sum(ord(ch) for ch in f"{fixture.home_team}-{fixture.away_team}")
    home = 0.34 + ((seed % 17) - 8) / 200
    away = 0.30 + (((seed // 7) % 17) - 8) / 220
    draw = max(0.18, 1.0 - home - away)
    total = home + draw + away
    home, draw, away = home / total, draw / total, away / total
    scores = [
        ("1-1", 0.118),
        ("1-0", 0.104 if home >= away else 0.081),
        ("0-1", 0.104 if away > home else 0.081),
        ("2-1", 0.086 if home >= away else 0.071),
    ]
    lean = "主胜" if home > away and home > draw else "客胜" if away > home and away > draw else "平局"
    return {
        "home": round(home * 100, 1),
        "draw": round(draw * 100, 1),
        "away": round(away * 100, 1),
        "lean": lean,
        "scores": scores,
    }


def fallback_report(fixtures: list[Fixture], context: dict[str, Any], reason: str) -> str:
    target = context["target_date"]
    date_obj = dt.date.fromisoformat(target)
    title = f"世界杯预测报告 · {date_obj.month}月{date_obj.day}日"

    schedule_rows = []
    tabs = []
    cards = []
    for idx, fixture in enumerate(fixtures):
        active = " active" if idx == 0 else ""
        match_name = f"{fixture.home_team} vs {fixture.away_team}"
        probs = pseudo_probabilities(fixture)
        news = context.get("news_by_match", {}).get(match_name, [])
        news_items = "".join(
            f"<li>{html.escape(item.get('title', ''))}</li>" for item in news[:3]
        ) or "<li>未抓到可靠实时新闻。</li>"
        score_rows = "".join(
            f"<div class=\"score\"><strong>{html.escape(score)}</strong><span>{prob:.1%}</span></div>"
            for score, prob in probs["scores"]
        )
        schedule_rows.append(
            f"<div class=\"sched-row\"><div class=\"sched-time\">{fixture.time_beijing}</div><div>{html.escape(match_name)} · {html.escape(fixture.group)} · {html.escape(fixture.venue)}</div></div>"
        )
        tabs.append(f"<button class=\"tab{active}\" onclick=\"show({idx})\">{html.escape(match_name)}</button>")
        cards.append(
            f"""
    <section class="card{active}">
      <div class="mh">
        <div>
          <h2>{html.escape(match_name)}</h2>
          <div class="meta">北京时间 {fixture.time_beijing} · {html.escape(fixture.group)} · {html.escape(fixture.venue)}</div>
        </div>
        <div class="tag">自动兜底</div>
      </div>
      <div class="grid two">
        <div class="block">
          <div class="sec-title">1X2 倾向</div>
          <div class="big">{probs["lean"]}</div>
          <p>主胜 {probs["home"]}% · 平局 {probs["draw"]}% · 客胜 {probs["away"]}%</p>
        </div>
        <div class="block">
          <div class="sec-title">新闻上下文</div>
          <ul>{news_items}</ul>
        </div>
      </div>
      <div class="sec-title">比分预测</div>
      <div class="scores">{score_rows}</div>
      <div class="bets">
        <div class="bet val"><b>价值观察</b><span>等待临场 1X2 与大小球盘口确认</span></div>
        <div class="bet risk"><b>风险项</b><span>自动任务未确认首发、伤停和盘口变化</span></div>
        <div class="bet cool"><b>冷门处理</b><span>仅小注比分，不建议追高风险串关</span></div>
      </div>
    </section>
            """.strip()
        )

    selected_idx = len(fixtures)
    tabs.append(f"<button class=\"tab\" onclick=\"show({selected_idx})\">精选方案</button>")
    cards.append(
        f"""
    <section class="card">
      <h2>精选方案</h2>
      <div class="status">自动 AI 报告未完成：{html.escape(reason)}。本页保留赛程、新闻和低置信模型参考。</div>
      <div class="alloc">
        <div><strong>70 单位</strong><span>等待盘口/首发确认</span></div>
        <div><strong>20 单位</strong><span>单场低风险方向</span></div>
        <div><strong>10 单位</strong><span>小注比分尝试</span></div>
      </div>
    </section>
        """.strip()
    )

    status_rows = "".join(
        f"<li>{html.escape(item['name'])}: {'OK' if item['ok'] else '缺失'} · {html.escape(item['detail'])}</li>"
        for item in context.get("source_status", [])
    )
    if not fixtures:
        schedule_html = "<div class=\"empty\">当天没有找到未赛世界杯赛程。</div>"
    else:
        schedule_html = "".join(schedule_rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#f0ede8;color:#1a1a1a;padding:20px;line-height:1.6}}
.container{{max-width:960px;margin:0 auto}}
h1{{font-size:24px;margin-bottom:4px}}
.subtitle,.meta{{color:#666;font-size:13px}}
.schedule-box,.card{{background:#fff;border:1px solid #ddd;border-radius:10px;padding:18px;margin:16px 0}}
.sched-row{{display:grid;grid-template-columns:72px 1fr;gap:10px;padding:8px 0;border-bottom:1px solid #f0ede8;font-size:13px}}
.sched-row:last-child{{border-bottom:0}}
.sched-time{{color:#1a5fa8;font-weight:800}}
.tabs{{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0}}
.tab{{border:1px solid #ccc;background:transparent;border-radius:999px;padding:7px 12px;cursor:pointer;font:inherit;font-size:12px;color:#555}}
.tab.active{{background:#1a5fa8;color:#fff;border-color:#1a5fa8}}
.card{{display:none}}
.card.active{{display:block}}
.mh{{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #eee;padding-bottom:12px;margin-bottom:14px}}
.tag{{height:max-content;background:#fff4e6;color:#8a4d00;border-radius:999px;padding:3px 8px;font-size:11px;font-weight:700}}
.grid.two{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.block{{background:#f8f7f4;border-radius:8px;padding:12px}}
.sec-title{{font-size:11px;font-weight:800;color:#888;text-transform:uppercase;letter-spacing:.06em;margin:12px 0 8px}}
.big{{font-size:24px;font-weight:800;color:#1a5fa8}}
ul{{padding-left:18px;font-size:13px;color:#444}}
.scores{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}
.score{{background:#e8f2fc;border-radius:8px;padding:10px;text-align:center}}
.score strong{{display:block;font-size:20px;color:#0d4e9e}}
.score span{{font-size:12px;color:#555}}
.bets,.alloc{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}}
.bet,.alloc div{{background:#f8f7f4;border-radius:8px;padding:10px;border-left:3px solid #1a5fa8}}
.bet.risk{{border-left-color:#c07800}}.bet.cool{{border-left-color:#aa2222}}
.bet span,.alloc span{{display:block;font-size:12px;color:#555;margin-top:4px}}
.status{{background:#fff8e6;border-left:3px solid #c07800;padding:10px;margin:10px 0;color:#5c3a00}}
.empty{{padding:12px;color:#666}}
.sources{{font-size:12px;color:#666;margin:18px 0 30px}}
@media(max-width:720px){{.grid.two,.scores,.bets,.alloc{{grid-template-columns:1fr}}.mh{{display:block}}}}
</style>
</head>
<body>
<main class="container">
  <h1>{html.escape(title)}</h1>
  <div class="subtitle">{target} · 北京时间 · 自动生成</div>
  <section class="schedule-box">
    <h2>赛程总览</h2>
    {schedule_html}
  </section>
  <nav class="tabs">{"".join(tabs)}</nav>
  {"".join(cards)}
  <section class="sources">
    <strong>数据源状态</strong>
    <ul>{status_rows}</ul>
    <p>风险提示：内容仅供参考，不构成投资建议。自动抓取可能遗漏临场伤停、首发和盘口变化。</p>
  </section>
</main>
<script>
function show(i){{
  document.querySelectorAll('.card').forEach((c,j)=>c.classList.toggle('active',j===i));
  document.querySelectorAll('.tab').forEach((b,j)=>b.classList.toggle('active',j===i));
}}
</script>
</body>
</html>
"""


def report_sort_key(path: Path) -> dt.date:
    try:
        return dt.date.fromisoformat(path.stem)
    except ValueError:
        return dt.date.min


def build_index() -> str:
    reports = sorted(REPORTS_DIR.glob("*.html"), key=report_sort_key, reverse=True)
    cards = []
    for path in reports:
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day.weekday()]
        cards.append(
            f"""
    <a class="day-card" href="reports/{html.escape(path.name)}">
      <span class="date-big">{day.day}</span>
      <span class="date-label">{day.year}年{day.month}月 · {weekday}</span>
      <span class="match-count">查看报告</span>
      <span class="arrow">→</span>
    </a>
            """.strip()
        )
    cards_html = "\n".join(cards) or '<div class="empty">还没有报告。</div>'
    updated = dt.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>世界杯每日投注报告</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#f0ede8;color:#1a1a1a;min-height:100vh;padding:36px 18px}}
.page{{width:min(960px,100%);margin:0 auto}}
.header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;border-bottom:1px solid #d8d2c8;padding-bottom:22px;margin-bottom:22px}}
h1{{font-size:30px;line-height:1.2}}
.sub{{margin-top:8px;color:#666;font-size:14px}}
.updated{{color:#888;font-size:12px;white-space:nowrap}}
.calendar-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}}
.day-card{{position:relative;display:flex;flex-direction:column;gap:7px;min-height:132px;padding:18px;border:1px solid #ddd;border-radius:8px;background:#fff;color:inherit;text-decoration:none;transition:transform .15s ease,border-color .15s ease,box-shadow .15s ease}}
.day-card:hover{{transform:translateY(-2px);border-color:#1a5fa8;box-shadow:0 10px 24px rgba(0,0,0,.08)}}
.date-big{{font-size:34px;font-weight:800;line-height:1;color:#1a5fa8}}
.date-label{{font-size:13px;color:#666}}
.match-count{{margin-top:auto;width:max-content;border-radius:999px;background:#e8f2fc;color:#0d4e9e;padding:3px 9px;font-size:12px;font-weight:700}}
.arrow{{position:absolute;right:16px;top:18px;color:#aaa;font-size:20px}}
.empty{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:20px;color:#666}}
.footer{{margin-top:28px;color:#777;font-size:12px;line-height:1.7}}
@media (max-width:640px){{.header{{display:block}}.updated{{margin-top:10px;white-space:normal}}h1{{font-size:25px}}}}
</style>
</head>
<body>
<main class="page">
  <header class="header">
    <div>
      <h1>世界杯每日投注报告</h1>
      <div class="sub">按日期打开对应 HTML 报告。</div>
    </div>
    <div class="updated">北京时间 {updated} 更新</div>
  </header>
  <section class="calendar-grid" aria-label="报告日期列表">
{cards_html}
  </section>
  <footer class="footer">页面为静态 HTML，由 GitHub Actions 自动抓取赛程与上下文并发布。内容仅供参考，不构成投资建议。</footer>
</main>
</body>
</html>
"""


def main() -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / ".nojekyll").touch()

    all_fixtures = load_fixtures([])
    target_date = requested_target_date(all_fixtures)
    explicit = (os.environ.get("TARGET_DATE") or "").strip() or (sys.argv[1].strip() if len(sys.argv) > 1 else "")
    if explicit:
        target_date = explicit

    fixtures, context, statuses = collect_context(target_date, include_finished=bool(explicit))
    context["target_date"] = target_date
    print(f"Target date: {target_date}")
    print(f"Fixtures selected: {len(fixtures)}")
    for status in statuses:
        print(f"{status.name}: {'OK' if status.ok else 'MISS'} - {status.detail}")

    prompt = build_prompt(fixtures, context)
    report_html = extract_html(call_deepseek(prompt))
    if not report_html:
        report_html = fallback_report(fixtures, context, "DeepSeek missing or invalid HTML")

    report_path = REPORTS_DIR / f"{target_date}.html"
    report_path.write_text(report_html, encoding="utf-8")
    INDEX_PATH.write_text(build_index(), encoding="utf-8")
    print(f"Wrote {report_path}")
    print(f"Wrote {INDEX_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
