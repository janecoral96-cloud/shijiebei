#!/usr/bin/env python3
"""
Generate a static daily World Cup report and rebuild the date index.

The script is intentionally defensive:
- target dates are based on Asia/Shanghai, not the GitHub runner timezone;
- the homepage is rebuilt from reports/*.html every run;
- if DeepSeek is unavailable or returns invalid HTML, a valid fallback page is
  still produced so the Pages deployment can complete.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled at runtime in GitHub Actions.
    OpenAI = None


REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
INDEX_PATH = REPO_ROOT / "index.html"

try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"


def get_target_date() -> str:
    raw = (os.environ.get("TARGET_DATE") or "").strip()
    if len(sys.argv) > 1:
        raw = sys.argv[1].strip()

    if raw:
        dt.date.fromisoformat(raw)
        return raw

    today_beijing = dt.datetime.now(BEIJING_TZ).date()
    return (today_beijing + dt.timedelta(days=1)).isoformat()


def request_json(url: str, timeout: int = 15) -> dict[str, Any] | None:
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"Fetch failed: {url} ({exc})")
        return None


def first_description(value: Any) -> str:
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return str(first.get("Description") or first.get("Name") or "")
    if isinstance(value, dict):
        return str(value.get("Description") or value.get("Name") or "")
    return ""


def scrape_fifa_matches(date_str: str) -> list[dict[str, Any]]:
    url = (
        "https://api.fifa.com/api/v3/calendar/matches"
        f"?from={date_str}T00:00:00Z"
        f"&to={date_str}T23:59:59Z"
        "&competitionId=17"
        "&count=50"
    )
    data = request_json(url)
    if not data:
        return []

    matches: list[dict[str, Any]] = []
    for item in data.get("Results", []):
        home = item.get("HomeTeam") or {}
        away = item.get("AwayTeam") or {}
        stadium = item.get("Stadium") or {}
        matches.append(
            {
                "home_team": first_description(home.get("TeamName")) or "TBD",
                "away_team": first_description(away.get("TeamName")) or "TBD",
                "kickoff_time": item.get("Date", ""),
                "venue": first_description(stadium.get("Name")),
                "city": first_description(stadium.get("CityName")),
                "stage": first_description(item.get("StageName")),
                "source": "FIFA",
            }
        )
    print(f"FIFA matches: {len(matches)}")
    return matches


def scrape_kalshi_markets() -> list[dict[str, Any]]:
    data = request_json(
        "https://trading-api.kalshi.com/trade-api/v2/markets"
        "?event_ticker=WORLDCUP&limit=50"
    )
    if not data:
        return []

    markets = [
        {
            "title": market.get("title", ""),
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "volume": market.get("volume"),
        }
        for market in data.get("markets", [])
    ]
    print(f"Kalshi markets: {len(markets)}")
    return markets


def collect_data(date_str: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches = scrape_fifa_matches(date_str)
    markets = scrape_kalshi_markets()
    return matches, markets


def build_prompt(date_str: str, matches: list[dict[str, Any]], markets: list[dict[str, Any]]) -> str:
    date_obj = dt.date.fromisoformat(date_str)
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][date_obj.weekday()]
    match_json = json.dumps(matches, ensure_ascii=False, indent=2) if matches else "未抓取到官方赛程，请明确标注数据缺口。"
    market_json = json.dumps(markets[:20], ensure_ascii=False, indent=2) if markets else "暂无 Kalshi 数据。"

    return f"""
你是世界杯足球分析师。请为 {date_str}（{date_obj.month}月{date_obj.day}日 {weekday}）生成一份完整静态 HTML 报告。

数据：
赛程：
{match_json}

Kalshi 市场：
{market_json}

要求：
1. 直接输出完整 HTML，不要 Markdown，不要解释。
2. 使用简体中文，UTF-8，页面标题包含日期。
3. 内嵌 CSS 和少量原生 JavaScript，不依赖外部资源。
4. 页面包含：赛程总览、每场比赛分析、比分预测、投注参考、风险提示。
5. 每场比赛给出 1X2 倾向、前 3 个比分、信心 1-10、主要风险。
6. 如果数据不足，必须明确写“数据不足，以下为低置信参考”，不要编造实时伤停。
7. 风险提示必须写明：内容仅供参考，不构成投资建议。
""".strip()


def call_deepseek(prompt: str) -> str | None:
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        print("DEEPSEEK_API_KEY is not set; using fallback HTML.")
        return None
    if OpenAI is None:
        print("openai package is unavailable; using fallback HTML.")
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "你只输出完整 HTML。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.45,
            max_tokens=12000,
        )
        content = response.choices[0].message.content or ""
        print(f"DeepSeek response length: {len(content)}")
        return content
    except Exception as exc:
        print(f"DeepSeek failed: {exc}; using fallback HTML.")
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
    return content


def format_kickoff(value: str) -> str:
    if not value:
        return "时间待定"
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(BEIJING_TZ).strftime("%H:%M")
    except ValueError:
        return value


def fallback_report(date_str: str, matches: list[dict[str, Any]], reason: str) -> str:
    date_obj = dt.date.fromisoformat(date_str)
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][date_obj.weekday()]
    title = f"世界杯每日报告 · {date_obj.month}月{date_obj.day}日"

    if matches:
        rows = "\n".join(
            f"""
            <article class="match">
              <div class="time">{html.escape(format_kickoff(str(match.get("kickoff_time", ""))))}</div>
              <h2>{html.escape(str(match.get("home_team", "TBD")))} vs {html.escape(str(match.get("away_team", "TBD")))}</h2>
              <p>{html.escape(str(match.get("stage") or "世界杯"))} · {html.escape(str(match.get("venue") or "场地待定"))}</p>
              <div class="note">自动分析暂不可用，本场仅展示抓取到的赛程信息。投注判断请等待人工复核。</div>
            </article>
            """.strip()
            for match in matches
        )
    else:
        rows = """
        <article class="match">
          <div class="time">暂无官方赛程</div>
          <h2>数据不足，以下为低置信参考</h2>
          <p>自动任务没有抓取到当天赛程，也没有成功生成 AI 分析。</p>
          <div class="note">请手动补充当天比赛后再发布正式投注报告。</div>
        </article>
        """.strip()

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#f0ede8;color:#1a1a1a;padding:24px;line-height:1.65}}
.container{{max-width:920px;margin:0 auto}}
.top{{padding:22px 0 18px;border-bottom:1px solid #d8d2c8;margin-bottom:18px}}
h1{{font-size:26px;line-height:1.2;margin-bottom:8px}}
.sub{{color:#666;font-size:14px}}
.status{{margin:18px 0;padding:12px 14px;border-left:4px solid #c07800;background:#fff8e6;color:#5c3a00;border-radius:0 8px 8px 0}}
.grid{{display:grid;gap:12px}}
.match{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px}}
.time{{font-size:13px;color:#1a5fa8;font-weight:700;margin-bottom:4px}}
h2{{font-size:18px;margin-bottom:6px}}
p{{font-size:13px;color:#666}}
.note{{margin-top:10px;font-size:13px;color:#444;background:#f8f7f4;border-radius:6px;padding:10px}}
.back{{display:inline-block;margin-top:20px;color:#1a5fa8;text-decoration:none;font-weight:700}}
.risk{{margin-top:24px;font-size:12px;color:#777}}
</style>
</head>
<body>
<main class="container">
  <header class="top">
    <h1>{html.escape(title)}</h1>
    <div class="sub">{date_str} · {weekday} · 北京时间</div>
  </header>
  <div class="status">自动生成未完成：{html.escape(reason)}。本页为兜底页面，保证网站可以继续部署访问。</div>
  <section class="grid">
    {rows}
  </section>
  <a class="back" href="../index.html">返回日期首页</a>
  <div class="risk">风险提示：内容仅供参考，不构成投资建议。请理性判断，量力而行。</div>
</main>
</body>
</html>
"""


def report_sort_key(path: Path) -> dt.date:
    try:
        return dt.date.fromisoformat(path.stem)
    except ValueError:
        return dt.date.min


def build_index(current_date: str | None = None, current_match_count: int | None = None) -> str:
    report_files = sorted(REPORTS_DIR.glob("*.html"), key=report_sort_key, reverse=True)
    cards: list[str] = []

    for path in report_files:
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day.weekday()]
        if current_date == path.stem and current_match_count is not None:
            count_text = f"{current_match_count} 场"
        else:
            count_text = "查看报告"
        cards.append(
            f"""
      <a class="day-card" href="reports/{path.name}">
        <span class="date-big">{day.day}</span>
        <span class="date-label">{day.year}年{day.month}月 · {weekday}</span>
        <span class="match-count">{html.escape(count_text)}</span>
        <span class="arrow">→</span>
      </a>
            """.strip()
        )

    if not cards:
        cards.append('<div class="empty">还没有报告。</div>')

    cards_html = "\n".join(cards)
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
  <footer class="footer">页面为静态 HTML，可通过 GitHub Pages 直接访问。内容仅供参考，不构成投资建议。</footer>
</main>
</body>
</html>
"""


def main() -> int:
    target_date = get_target_date()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / ".nojekyll").touch()

    print(f"Target date: {target_date}")
    matches, markets = collect_data(target_date)
    prompt = build_prompt(target_date, matches, markets)
    generated = extract_html(call_deepseek(prompt))

    if generated:
        report_html = generated
    else:
        report_html = fallback_report(target_date, matches, "DeepSeek 不可用或返回内容不是完整 HTML")

    report_path = REPORTS_DIR / f"{target_date}.html"
    report_path.write_text(report_html, encoding="utf-8")
    print(f"Wrote {report_path}")

    INDEX_PATH.write_text(build_index(target_date, len(matches)), encoding="utf-8")
    print(f"Wrote {INDEX_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
