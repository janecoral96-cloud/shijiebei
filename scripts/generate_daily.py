#!/usr/bin/env python3
"""
世界杯每日投注报告自动生成脚本
1. 收集赛程和赔率数据（多数据源）
2. 调用 DeepSeek API 生成 HTML 分析报告
3. 自动更新首页 index.html
"""

import os, sys, json, re, datetime, requests
from pathlib import Path

# ============================================================
# 配置
# ============================================================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"
INDEX_PATH = REPO_ROOT / "index.html"

# 目标日期
if os.environ.get("TARGET_DATE"):
    target_date = os.environ["TARGET_DATE"]
elif len(sys.argv) > 1:
    target_date = sys.argv[1]
else:
    target_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

print(f"Target date: {target_date}")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Step 1: Data Collection
# ============================================================

def scrape_fifa_api(date_str):
    """Try FIFA official API"""
    matches = []
    try:
        url = (
            f"https://api.fifa.com/api/v3/calendar/matches"
            f"?from={date_str}T00:00:00Z&to={date_str}T23:59:59Z"
            f"&competitionId=17&count=20"
        )
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("Results", []):
                home = m.get("HomeTeam", {}).get("TeamName", [{}])[0].get("Description", "?")
                away = m.get("AwayTeam", {}).get("TeamName", [{}])[0].get("Description", "?")
                matches.append({
                    "home_team": home,
                    "away_team": away,
                    "kickoff_time": m.get("Date", ""),
                    "venue": m.get("Stadium", {}).get("Name", [{}])[0].get("Description", ""),
                    "city": m.get("Stadium", {}).get("CityName", [{}])[0].get("Description", ""),
                    "group": "",
                    "stage": m.get("StageName", [{}])[0].get("Description", ""),
                })
            if matches:
                print(f"FIFA API: {len(matches)} matches")
    except Exception as e:
        print(f"FIFA API failed: {e}")
    return matches


def scrape_flashscore(date_str):
    """Try Flashscore scraping"""
    matches = []
    try:
        from bs4 import BeautifulSoup
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        url = "https://www.flashscore.com/football/world/world-cup-2026/fixtures/"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for row in soup.select('[class*="event__match"]'):
                home = row.select_one('[class*="event__participant--home"]')
                away = row.select_one('[class*="event__participant--away"]')
                time_el = row.select_one('[class*="event__time"]')
                if home and away:
                    matches.append({
                        "home_team": home.get_text(strip=True),
                        "away_team": away.get_text(strip=True),
                        "kickoff_time": time_el.get_text(strip=True) if time_el else "",
                        "venue": "", "city": "", "group": "", "stage": "",
                    })
        if matches:
            print(f"Flashscore: {len(matches)} matches")
    except ImportError:
        print("BeautifulSoup not installed, skipping web scrape")
    except Exception as e:
        print(f"Flashscore failed: {e}")
    return matches


def scrape_kalshi():
    """Try Kalshi prediction market data"""
    markets = []
    try:
        url = "https://trading-api.kalshi.com/trade-api/v2/markets?event_ticker=WORLDCUP&limit=50"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("markets", []):
                markets.append({
                    "title": m.get("title", ""),
                    "yes_bid": m.get("yes_bid", 0),
                    "yes_ask": m.get("yes_ask", 0),
                    "volume": m.get("volume", 0),
                })
            print(f"Kalshi: {len(markets)} markets")
    except Exception as e:
        print(f"Kalshi failed: {e}")
    return markets


def collect_all_data(date_str):
    """Collect match data from all sources"""
    matches = scrape_fifa_api(date_str)
    if not matches:
        matches = scrape_flashscore(date_str)

    kalshi_data = scrape_kalshi()

    # Add default odds structure
    for m in matches:
        if "odds" not in m:
            m["odds"] = {
                "home_win": None, "draw": None, "away_win": None,
                "over_2_5": None, "under_2_5": None,
                "asian_handicap": None,
            }

    return matches, kalshi_data


# ============================================================
# Step 2: DeepSeek Report Generation
# ============================================================

def build_prompt(matches, kalshi_data, date_str):
    """Build the prompt for DeepSeek"""
    date_obj = datetime.date.fromisoformat(date_str)
    date_cn = f"{date_obj.month}月{date_obj.day}日"
    weekdays = ["周一","周二","周三","周四","周五","周六","周日"]
    weekday_cn = weekdays[date_obj.weekday()]

    match_str = json.dumps(matches, ensure_ascii=False, indent=2) if matches else \
        "（暂无抓取到的赛程数据，请根据你对2026世界杯赛程的了解推断当日比赛）"
    kalshi_str = json.dumps(kalshi_data[:20], ensure_ascii=False, indent=2) if kalshi_data else "（无Kalshi数据）"

    return f"""你是专业世界杯足球分析师。请为 {date_str}（{date_cn} {weekday_cn}）生成一份完整的HTML投注分析报告。

## 赛程数据
{match_str}

## Kalshi预测市场数据
{kalshi_str}

## 输出要求
生成完整HTML页面（包含嵌入式CSS），具体要求：

### 样式
- 背景 #f0ede8，主色 #1a5fa8，白色卡片
- 响应式布局，移动端友好
- tab切换（每场比赛一个tab + 精选方案tab）
- JavaScript show() 函数实现切换
- 字体 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif

### 每场比赛必须包含（10个板块 + 预测 + 投注建议）
1. 最近5场战绩（两队，含具体比分和日期）
2. 世界排名变化（FIFA排名，Kalshi胜率）
3. 近10场进球/失球数
4. 历史交锋记录
5. 核心球员伤停情况（标注 ⚠）
6. 战术风格对位分析
7. 小组赛形势/战意分析
8. 博彩公司初盘 & 即时盘变化（表格样式）
9. 市场投注热度分析
10. 爆冷因素评估（⚠ 爆冷可能性：低/中/高）

### 每场比赛的预测
- 4个比分预测（主选+次选+3rd+4th），每个带概率
- 信心指数 1-10（信心点图）
- 详细预测理由

### 每场比赛的投注建议
- 5个投注建议卡片（不同颜色边框：蓝=价值，橙=风险，红=冷门）
- 包含：bet type标签、推荐投注、赔率、三依据

### 精选方案板块
- 4-5个精选（稳胆/Banker、博高赔、冷门尝试）
- 100单位资金分配（四格网格）
- 今日价值盘高亮

### HTML结构
- <div class="container"> 包含所有内容
- <h1> 标题 + <div class="subtitle"> 副标题
- <div class="schedule-box"> 赛程总览
- <div class="tabs"> tab按钮
- <div class="card"> 每场比赛（id=m0, m1, m2...）
- 最后一个card是精选方案
- <script>function show(i){{...}}</script>

### 底部
- 风险提示：数据仅供参考，不构成投资建议

直接输出完整HTML，不要markdown标记，不要解释。"""


def call_deepseek(prompt):
    """Call DeepSeek API"""
    if not DEEPSEEK_API_KEY:
        print("ERROR: DEEPSEEK_API_KEY not set")
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    except ImportError:
        print("ERROR: openai package not installed")
        return None

    print(f"Calling DeepSeek ({DEEPSEEK_MODEL})...")
    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "你是世界杯分析师。直接输出完整HTML，不要markdown标记。"
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=16000,
        )
        content = response.choices[0].message.content
        print(f"DeepSeek returned {len(content)} chars")
        return content
    except Exception as e:
        print(f"DeepSeek API error: {e}")
        return None


def extract_html(raw):
    """Extract pure HTML from DeepSeek response"""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```html"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    return raw.strip()


# ============================================================
# Step 3: Update index.html
# ============================================================

def update_index(date_str, matches_count):
    """Add new date card to index.html"""
    if not INDEX_PATH.exists():
        print("index.html not found, skipping")
        return

    content = INDEX_PATH.read_text(encoding="utf-8")
    date_obj = datetime.date.fromisoformat(date_str)
    day_num = date_obj.day
    year_month = f"{date_obj.year}年{date_obj.month}月"
    weekdays = ["周一","周二","周三","周四","周五","周六","周日"]
    weekday_cn = weekdays[date_obj.weekday()]

    new_card = (
        f'  <a href="reports/{date_str}.html" class="day-card">\n'
        f'    <div class="date-big">{day_num}</div>\n'
        f'    <div class="date-label">{year_month} \u00b7 {weekday_cn}</div>\n'
        f'    <div class="match-count">\n'
        f'      <span class="badge">{matches_count}\u573a</span> \u5df2\u53d1\u5e03\n'
        f'    </div>\n'
        f'    <div class="arrow">\u2192</div>\n'
        f'  </a>'
    )

    # Find and replace disabled placeholder
    pattern = (
        r'(<div class="day-card disabled">\s*'
        r'<div class="date-big">' + str(day_num) + r'</div>.*?</div>\s*</div>)'
    )
    match_obj = re.search(pattern, content, re.DOTALL)
    if match_obj:
        content = content.replace(match_obj.group(1), new_card, 1)
        print(f"index.html: replaced disabled card for day {day_num}")
    elif f'reports/{date_str}.html' not in content:
        # Insert before footer
        marker = '<div class="footer">'
        if marker in content:
            content = content.replace(marker, new_card + "\n\n" + marker, 1)
            print(f"index.html: inserted new card for day {day_num}")
    else:
        print(f"index.html: already has {date_str}")

    INDEX_PATH.write_text(content, encoding="utf-8")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print(f"World Cup Report Generator - {target_date}")
    print("=" * 50)

    # Step 1: Collect data
    print("\n[1/3] Collecting match data...")
    match_data, kalshi_data = collect_all_data(target_date)
    print(f"      Matches: {len(match_data)}, Kalshi markets: {len(kalshi_data)}")

    # Step 2: Generate report
    print("\n[2/3] Generating report via DeepSeek...")
    prompt = build_prompt(match_data, kalshi_data, target_date)
    html_raw = call_deepseek(prompt)
    html_content = extract_html(html_raw)

    if html_content and html_content.startswith("<!DOCTYPE html>"):
        report_path = REPORTS_DIR / f"{target_date}.html"
        report_path.write_text(html_content, encoding="utf-8")
        print(f"      Report saved: {report_path}")
    else:
        print("ERROR: Invalid HTML generated")
        if html_content:
            preview = html_content[:300]
            print(f"      Preview: {preview}")
        sys.exit(1)

    # Step 3: Update index
    print("\n[3/3] Updating index.html...")
    match_count = len(match_data) if match_data else 4
    update_index(target_date, match_count)

    print("\n" + "=" * 50)
    print("DONE! Report generation complete.")
    print("=" * 50)
