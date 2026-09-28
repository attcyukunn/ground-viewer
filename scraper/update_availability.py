"""
球場の空き状況を自動取得して docs/data/availability.json に保存するプログラム
GitHub Actions（クラウド）で毎朝自動実行される想定です。
対象: 厚木市 玉川野球場 / 伊勢原市 いせはらサンシャイン・スタジアム / 大磯町 大磯運動公園 野球場
どれか1つの球場の取得に失敗しても、他の球場の取得は続けます。
失敗した球場は、前回取得したデータをそのまま残します。
"""

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

JST = ZoneInfo("Asia/Tokyo")
WEEKS_AHEAD = 8
DAYS_AHEAD = WEEKS_AHEAD * 7

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "availability.json")


def today_jst():
    return datetime.now(JST).date()


# ---------------------------------------------------------------------------
# 厚木市（玉川野球場）
# ---------------------------------------------------------------------------
ATSUGI_URL = (
    "https://www.shisetsu.net/yoyaku/?2128136331"
    "&__SANE_MI__=%E3%83%88%E3%83%83%E3%83%97%E3%83%9A%E3%83%BC%E3%82%B8"
)
ATSUGI_GROUNDS = [{"name": "玉川野球場", "query": "玉川球場"}]


def atsugi_get_year_month(page):
    for c in page.get_by_text(re.compile(r"\d{4}/\d{2}")).all():
        t = c.inner_text().strip()
        if re.fullmatch(r"\d{4}/\d{2}", t):
            y, m = t.split("/")
            return int(y), int(m)
    raise ValueError("年月の見出しが見つかりませんでした")


def atsugi_scrape_week(page, ground_name, checked_at):
    year, month = atsugi_get_year_month(page)
    rows = page.locator("table").first.locator("tr").all()

    dates = []
    cur_y, cur_m, prev_day = year, month, None
    for c in rows[0].locator("th, td").all():
        day_num = int(c.inner_text().strip().split("\n")[0].split("/")[1])
        if prev_day is not None and day_num < prev_day:
            cur_m += 1
            if cur_m > 12:
                cur_m, cur_y = 1, cur_y + 1
        dates.append(f"{cur_y:04d}-{cur_m:02d}-{day_num:02d}")
        prev_day = day_num

    per_date = {d: {"times": [], "slots": 0} for d in dates}
    for row in rows[1:]:
        for col_idx, cell in enumerate(row.locator("th, td").all()):
            if col_idx >= len(dates):
                continue
            parts = cell.inner_text().strip().split("\n")
            time_label = parts[0] if parts else ""
            status = parts[1] if len(parts) > 1 else ""
            if status in ("―", "保守", ""):
                continue
            info = per_date[dates[col_idx]]
            info["slots"] += 1
            if status == "○":
                t = time_label.rstrip("-")
                info["times"].append(f"{t[:2]}:{t[2:]}" if len(t) == 4 else t)

    results = []
    for d, info in per_date.items():
        if info["slots"] == 0:
            status_label = "対象外"
        elif len(info["times"]) == info["slots"]:
            status_label = "空きあり"
        elif info["times"]:
            status_label = "一部空き"
        else:
            status_label = "満"
        results.append({
            "groundName": ground_name,
            "date": d,
            "status": status_label,
            "times": ", ".join(info["times"]),
            "url": ATSUGI_URL,
            "directLink": False,
            "checkedAt": checked_at,
        })
    return results


def atsugi_next_week(page):
    page.locator("#id7a").click(timeout=15000)
    page.wait_for_timeout(2500)


def scrape_atsugi(browser, checked_at):
    results = []
    page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo", viewport={"width": 1400, "height": 1000})
    page.set_default_timeout(60000)
    page.goto(ATSUGI_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3000)

    for i, ground in enumerate(ATSUGI_GROUNDS):
        print(f"[厚木] {ground['name']}")
        if i > 0:
            page.get_by_role("button", name="施設を再選択").click()
            page.wait_for_timeout(1500)
        page.locator(".bootstrap-tagsinput").click()
        page.get_by_role("textbox", name="施設名・曜日などを入力").fill(ground["query"])
        page.get_by_role("button", name="検索 ").click()
        page.wait_for_timeout(2500)
        page.get_by_role("button", name=ground["name"]).click()
        page.wait_for_timeout(2500)

        for week in range(WEEKS_AHEAD):
            week_results = atsugi_scrape_week(page, ground["name"], checked_at)
            results.extend(week_results)
            print(f"  {week + 1}週目: {len(week_results)}件")
            if week < WEEKS_AHEAD - 1:
                try:
                    atsugi_next_week(page)
                except Exception as e:
                    print(f"  次の週に進めませんでした: {e}")
                    break
    page.close()
    return results


# ---------------------------------------------------------------------------
# e-kanagawa（伊勢原市・大磯町）
# ---------------------------------------------------------------------------
OPEN_LABELS = {"空き状況のみ", "利用可能"}

EKANAGAWA_GROUNDS = [
    {
        "name": "いせはらサンシャイン・スタジアム",
        "url": lambda ds: (
            "https://s-yoyaku.e-kanagawa.lg.jp/isehara/FacilityAvailability/"
            f"Index/142140/0015?ptn=2&d={ds}&sd={ds}&ed={ds}"
        ),
    },
    {
        "name": "大磯運動公園 野球場",
        "url": lambda ds: (
            "https://s-yoyaku.e-kanagawa.lg.jp/oiso/FacilityAvailability/"
            f"Index/143413/0001?ptn=3&d={ds}&rc=005&sd={ds}&rc2w=005&ed={ds}"
        ),
    },
]


def parse_status_row(text):
    slots = []
    for chunk in re.split(r"(?<!\d)(?=\d+時から\d+時)", text):
        m = re.match(r"(\d+)時から(\d+)時\s*(\S+)", chunk.strip())
        if m:
            slots.append((int(m.group(1)), m.group(3)))
    return slots


def ekanagawa_scrape_day(page, ground, d, checked_at, skip_tutorial):
    ds = d.isoformat()
    url = ground["url"](ds)
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    if skip_tutorial:
        try:
            page.get_by_text("スキップ", exact=True).click(timeout=4000)
            page.wait_for_timeout(1000)
        except Exception:
            pass

    entry = {
        "groundName": ground["name"],
        "date": ds,
        "status": "不明",
        "times": "",
        "url": url,
        "directLink": True,
        "checkedAt": checked_at,
    }

    tables = page.locator("table").all()
    if len(tables) < 2:
        return entry
    rows = tables[1].locator("tr").all()
    if len(rows) < 4:
        return entry
    cells = rows[3].locator("th, td").all()
    if len(cells) < 2:
        return entry

    slots = parse_status_row(cells[1].inner_text())
    if not slots:
        entry["status"] = "対象外"
        return entry

    open_slots = [s for s in slots if s[1] in OPEN_LABELS]
    entry["times"] = ", ".join(f"{h:02d}:00" for h, _ in open_slots)
    if len(open_slots) == len(slots):
        entry["status"] = "空きあり"
    elif open_slots:
        entry["status"] = "一部空き"
    else:
        entry["status"] = Counter(s[1] for s in slots).most_common(1)[0][0]
    return entry


def scrape_ekanagawa(browser, ground, checked_at):
    print(f"[e-kanagawa] {ground['name']}")
    results = []
    page = browser.new_page(locale="ja-JP", timezone_id="Asia/Tokyo", viewport={"width": 1400, "height": 1000})
    page.set_default_timeout(60000)
    start = today_jst()
    for i in range(DAYS_AHEAD):
        d = start + timedelta(days=i)
        try:
            entry = ekanagawa_scrape_day(page, ground, d, checked_at, skip_tutorial=(i == 0))
        except Exception as e:
            print(f"  {d} 取得失敗: {e}")
            continue
        results.append(entry)
        page.wait_for_timeout(1200)
    print(f"  {len(results)}件")
    page.close()
    return results


# ---------------------------------------------------------------------------
# まとめて実行
# ---------------------------------------------------------------------------
def load_previous():
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"entries": []}


def main():
    checked_at = datetime.now(JST).isoformat(timespec="minutes")
    previous = load_previous()
    prev_by_ground = {}
    for e in previous.get("entries", []):
        prev_by_ground.setdefault(e["groundName"], []).append(e)

    all_entries = []
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        jobs = [("玉川野球場", lambda: scrape_atsugi(browser, checked_at))]
        for g in EKANAGAWA_GROUNDS:
            jobs.append((g["name"], lambda g=g: scrape_ekanagawa(browser, g, checked_at)))

        for name, job in jobs:
            try:
                entries = job()
                if not entries:
                    raise RuntimeError("データが1件も取れませんでした")
                all_entries.extend(entries)
            except Exception as e:
                print(f"!! {name} の取得に失敗: {e}")
                errors.append({"groundName": name, "message": str(e)[:200]})
                all_entries.extend(prev_by_ground.get(name, []))

        browser.close()

    today_str = today_jst().isoformat()
    dedup = {}
    for e in all_entries:
        if e["date"] >= today_str:
            dedup[f"{e['groundName']}|{e['date']}"] = e
    entries = sorted(dedup.values(), key=lambda e: (e["date"], e["groundName"]))

    output = {"updatedAt": checked_at, "errors": errors, "entries": entries}
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)
    print(f"\n合計{len(entries)}件を保存しました（エラー {len(errors)}件）")


if __name__ == "__main__":
    main()
