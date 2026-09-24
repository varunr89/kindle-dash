#!/usr/bin/env python3
"""Render an office e-ink panel PNG at the Kindle Paperwhite 5 native size (1236x1648).

Four panels rotate server-side; the Kindle only ever fetches one fixed URL, so every
decision about what is on screen happens here.

    1  snow     SNOW price, day change, 30-day sparkline            (Nasdaq API)
    2  status   Snowflake status page: indicator, incidents, health (status.snowflake.com)
    3  bellevue weather, 4-day forecast, air quality, sun           (open-meteo)
    4  geek     Snowflake blog, top HN stories, today's xkcd        (RSS, Firebase, xkcd)

No API keys, no Pillow: the sparkline is inline SVG and the whole layout is HTML, so
Chrome screenshots it at 1:1. Run:

    python3 render_panels.py --panel snow --out dash.png
    python3 render_panels.py --rotate --out dash.png --publish

`--rotate` picks the slot from the wall clock, so a `*/5` cron needs no arguments.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from pathlib import Path

OUT = Path(__file__).resolve().parent
W, H = 1236, 1648
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Snowflake's Bellevue office is in the Spring District.
LAT, LON, TZ = 47.6214, -122.1852, "America/Los_Angeles"
PLACE = "Spring District, Bellevue"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122 Safari/537.36")

PANELS = ["snow", "status", "bellevue", "menu", "blog", "hn", "xkcd"]
ROTATE_MINUTES = 1

REPO = os.environ.get("GH_REPO", "varunr89/kindle-dash")
BRANCH = os.environ.get("GH_BRANCH", "main")
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"


def frame_name(panel: str) -> str:
    """One file per panel.

    This is what makes a 1-minute cadence possible on raw.githubusercontent.com, whose
    CDN serves `max-age=300` and ignores every client-side cache-buster. Because the
    device fetches each path only once per full cycle, and the cycle
    (len(PANELS) * ROTATE_MINUTES) is longer than that 300s, each fetch finds the cached
    copy already expired -> a cache miss -> current bytes. One URL for all panels could
    never be fresher than ~5 minutes.
    """
    return f"dash-{panel}.png"


def frame_urls() -> list:
    return [f"{RAW_BASE}/{frame_name(p)}" for p in PANELS]


# raw.githubusercontent.com serves `max-age=300` per path and ignores cache-busters, so the
# device's per-path period has to be longer than this. See frame_name() for why.
CDN_TTL = 300


def freshness_warning() -> str:
    """Empty while the rotation is fast enough for every device fetch to be a cache miss."""
    period = len(PANELS) * ROTATE_MINUTES * 60
    if period <= CDN_TTL:
        need = CDN_TTL // (ROTATE_MINUTES * 60) + 1
        return (f"WARNING: {len(PANELS)} panels x {ROTATE_MINUTES}m = {period}s per path, under "
                f"the CDN's {CDN_TTL}s cache - the device will paint stale frames. Keep at least "
                f"{need} panels.")
    return ""


def conf_text() -> str:
    """The device's config, derived from PANELS so the device's wheel and ours cannot drift.

    Generated here rather than in the shell script that publishes it: the device's URL order
    has to match the publisher's slot order, and one generator is one place to get it wrong.
    """
    return (
        "# Device config. Fetched by dashboard.sh every cycle and adopted when it changes, so\n"
        "# changing a value here reaches the panel without touching the device (within the CDN's\n"
        f"# ~{CDN_TTL // 60} min). Only DASH_URLS and DASH_INTERVAL are read; the file is never sourced.\n"
        f'DASH_URLS="{" ".join(frame_urls())}"\n'
        f"DASH_INTERVAL={ROTATE_MINUTES * 60}\n"
    )


def revision() -> str:
    """Short sha of the code doing the rendering.

    Printed with every frame so the log shows which revision drew it - which is also how you
    confirm from here that an edit made on GitHub actually reached this machine.
    """
    try:
        done = subprocess.run(["git", "-C", str(OUT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5)
        return done.stdout.strip() or "nogit"
    except Exception:
        return "nogit"

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm + hail", 99: "severe thunderstorm",
}


# ---------------------------------------------------------------- fetch helpers

def fetch(url: str, headers: dict | None = None, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url: str, headers: dict | None = None, timeout: int = 20,
               attempts: int = 3) -> dict:
    """Retry: a single transient blip should not cost the whole slot's frame."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return json.loads(fetch(url, headers, timeout))
        except Exception as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    assert last is not None
    raise last


def _num(s: str) -> float:
    return float(re.sub(r"[^0-9.\-]", "", s or "0") or 0)


def strip_html(s: str, limit: int = 200) -> str:
    """HN comment bodies are HTML; flatten to one line and cut on a word boundary."""
    txt = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()
    if len(txt) <= limit:
        return txt
    cut = txt[:limit].rsplit(" ", 1)[0]
    return cut + " …"


# ------------------------------------------------------------------ panel data

def data_snow() -> dict:
    info = fetch_json(
        "https://api.nasdaq.com/api/quote/SNOW/info?assetclass=stocks",
        {"Accept": "application/json"},
    )["data"]
    primary = info.get("primaryData") or {}
    start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    hist = fetch_json(
        f"https://api.nasdaq.com/api/quote/SNOW/historical"
        f"?assetclass=stocks&fromdate={start}&limit=30",
        {"Accept": "application/json"},
    )["data"]
    rows = ((hist.get("tradesTable") or {}).get("rows")) or []
    series = [(_num(r.get("close"))) for r in reversed(rows)]  # oldest -> newest
    series = [v for v in series if v > 0]

    price = _num(primary.get("lastSalePrice"))
    change = _num(primary.get("netChange"))
    pct = primary.get("percentageChange") or ""
    return {
        "price": price,
        "change": change,
        "pct": pct,
        "volume": primary.get("volume") or "—",
        "prev_close": _num(primary.get("previousClose")),
        "series": series,
        "hi30": max(series) if series else price,
        "lo30": min(series) if series else price,
        "asof": info.get("lastTradeTimestamp") or primary.get("lastTradeTimestamp") or "",
    }


def data_status() -> dict:
    """All four Statuspage endpoints, kept separate so each pane can be labelled with the
    API path it came from."""
    base = "https://status.snowflake.com/api/v2"
    status = fetch_json(f"{base}/status.json")
    summary = fetch_json(f"{base}/summary.json")
    comps = fetch_json(f"{base}/components.json").get("components", [])
    incidents = fetch_json(f"{base}/incidents/unresolved.json").get("incidents", [])

    leaves = [c for c in comps if not c.get("group")]
    counts: dict[str, int] = {}
    for c in leaves:
        counts[c.get("status", "unknown")] = counts.get(c.get("status", "unknown"), 0) + 1
    bad = [c for c in leaves if c.get("status") != "operational"]
    notable = [c for c in leaves
               if re.search(r"oregon|snowsight|global|us west|us east", c.get("name", ""), re.I)]

    return {
        "indicator": (status.get("status") or {}).get("indicator", "unknown"),
        "description": (status.get("status") or {}).get("description", ""),
        "page_updated": ((status.get("page") or {}).get("updated_at") or "")[:16].replace("T", " "),
        "summary_indicator": (summary.get("status") or {}).get("indicator", ""),
        "n_components": len(summary.get("components", [])),
        "n_incidents": len(summary.get("incidents", [])),
        "n_maint": len(summary.get("scheduled_maintenances", [])),
        "n_leaves": len(leaves),
        "counts": counts,
        "bad": [{"name": c.get("name", ""), "status": c.get("status", "")} for c in bad[:5]],
        "notable": [{"name": c.get("name", ""), "status": c.get("status", "")} for c in notable[:4]],
        "incidents": [
            {
                "name": i.get("name", ""),
                "status": i.get("status", ""),
                "impact": i.get("impact", ""),
                "created": (i.get("created_at") or "")[:16].replace("T", " "),
                "body": strip_html((i.get("incident_updates") or [{}])[0].get("body", ""), 190),
                "n_updates": len(i.get("incident_updates") or []),
            }
            for i in incidents[:2]
        ],
    }


def data_bellevue() -> dict:
    w = fetch_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,sunrise,sunset"
        f"&timezone={TZ}&forecast_days=5&temperature_unit=fahrenheit&wind_speed_unit=mph"
    )
    aq = fetch_json(
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={LAT}&longitude={LON}&current=us_aqi,pm2_5&timezone={TZ}"
    )
    days = w["daily"]
    forecast = []
    for i in range(1, min(5, len(days["time"]))):
        forecast.append({
            "day": datetime.fromisoformat(days["time"][i]).strftime("%a"),
            "cond": WMO.get(days["weather_code"][i], "—"),
            "hi": round(days["temperature_2m_max"][i]),
            "lo": round(days["temperature_2m_min"][i]),
            "pop": days["precipitation_probability_max"][i],
        })
    cur = w["current"]
    aqi = (aq.get("current") or {}).get("us_aqi")
    return {
        "temp": round(cur["temperature_2m"]),
        "feels": round(cur["apparent_temperature"]),
        "cond": WMO.get(cur["weather_code"], "—"),
        "wind": round(cur["wind_speed_10m"]),
        "forecast": forecast,
        "aqi": aqi,
        "pm25": (aq.get("current") or {}).get("pm2_5"),
        "aqi_word": aqi_word(aqi),
        "sunrise": days["sunrise"][0][11:16],
        "sunset": days["sunset"][0][11:16],
    }


def aqi_word(aqi):
    if aqi is None:
        return ""
    if aqi <= 50:
        return "good"
    if aqi <= 100:
        return "moderate"
    if aqi <= 150:
        return "unhealthy (sensitive)"
    if aqi <= 200:
        return "unhealthy"
    return "very unhealthy"


def fmt_date(s: str) -> str:
    """RFC822 pubDate -> 'Sep 22'."""
    try:
        return parsedate_to_datetime(s).strftime("%b %-d")
    except Exception:
        return (s or "")[:16]


def data_blog() -> dict:
    """Snowflake's own feed, on its own panel because those titles are long."""
    posts = []
    try:
        xml = fetch("https://www.snowflake.com/feed/").decode("utf-8", "replace")
        root = ET.fromstring(xml)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            posts.append({
                "title": title,
                "date": fmt_date(item.findtext("pubDate") or ""),
                "desc": strip_html(item.findtext("description") or "", 230),
            })
            if len(posts) >= 5:
                break
    except Exception as exc:
        sys.stderr.write(f"feed: {exc}\n")
    return {"posts": posts}


def data_hn() -> dict:
    """Top 3 stories, each with up to three top-level comments. Given a whole panel so
    the comments have room to actually be worth reading."""
    stories = []
    try:
        ids = fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:3]
        for i in ids:
            it = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
            comments = []
            for kid in (it.get("kids") or [])[:2]:
                try:
                    c = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{kid}.json")
                    txt = strip_html(c.get("text") or "", 240)
                    if txt:
                        comments.append({"by": c.get("by", ""), "text": txt})
                except Exception as exc:
                    sys.stderr.write(f"hn comment {kid}: {exc}\n")
            stories.append({
                "title": (it.get("title") or "").strip(),
                "score": it.get("score", 0),
                "by": it.get("by", ""),
                "descendants": it.get("descendants", 0),
                "comments": comments,
            })
    except Exception as exc:
        sys.stderr.write(f"hn: {exc}\n")
    return {"stories": stories}


def data_xkcd() -> dict:
    try:
        c = fetch_json("https://xkcd.com/info.0.json")
    except Exception as exc:
        sys.stderr.write(f"xkcd: {exc}\n")
        return {}
    img = c.get("img", "")
    # xkcd publishes a 2x asset next to every strip; on a panel this size the 1x
    # original is noticeably soft once scaled to the page width.
    if img.endswith(".png"):
        big = img[:-4] + "_2x.png"
        try:
            if fetch(big) :
                img = big
        except Exception:
            pass
    return {"num": c.get("num"), "title": c.get("title", ""),
            "img": img, "alt": c.get("alt", "")}


# Sifted runs the Bellevue lodge kitchen. The public page is a JS app that renders
# nothing server-side, but it loads a plain JSON API - so read that directly.
SIFTED_MARKET = "https://eat.sifted.co/api/markets/snowflake/bellevue"
SIFTED_MEALS = "https://eat.sifted.co/api/markets/meals"

# The lodge switches from breakfast to lunch at 11:00 local time; the panel follows that
# rather than showing both at once.
MENU_SWITCH_HOUR = 11


def data_menu() -> dict:
    """The lodge menu for right now - breakfast before MENU_SWITCH_HOUR, lunch after.

    Every dish is kept, condiment rows included; the point of this panel is to answer "what
    is there", so nothing is ranked, filtered or capped. Showing one service line at a time
    is what makes that affordable - both together do not fit a page.
    """
    market = (fetch_json(SIFTED_MARKET).get("data") or {})
    mid = market.get("id", "")
    if not mid:
        return {}

    day, raw = None, []
    for offset in range(0, 4):  # weekends/off days return an empty list
        d = (datetime.now() + timedelta(days=offset)).date()
        raw = fetch_json(f"{SIFTED_MEALS}?id={mid}&date={d.isoformat()}").get("data") or []
        if raw:
            day = d
            break
    if not raw:
        return {}

    lines = []
    for sl in sorted(raw, key=lambda s: (s.get("serviceLine") or {}).get("order", 99)):
        name = (sl.get("serviceLine") or {}).get("name", "")
        menus = []
        for m in sl.get("menus") or []:
            items = []
            for e in m.get("scheduledElements") or []:
                tags = [t.lower() for t in (e.get("tags") or [])]
                items.append({
                    "name": (e.get("name") or "").strip(),
                    "veg": "vegan" in tags,
                    "vgt": "vegetarian" in tags,
                })
            if items:
                menus.append({
                    "menu": (m.get("name") or "").strip(),
                    "brand": ((m.get("brand") or {}).get("name") or "").strip(),
                    "items": items,
                })
        if menus:
            lines.append({"line": name, "menus": menus})

    hour = datetime.now().hour
    want = "breakfast" if hour < MENU_SWITCH_HOUR else "lunch"
    target = next((l for l in lines if l["line"].lower() == want), None)
    note = ""
    if target is None:
        target = next((l for l in lines if l["line"].lower() in ("breakfast", "lunch")), None)
        if target is not None:
            note = f"no {want} today - showing {target['line'].lower()}"
    if target is None:
        target = lines[0]
        note = f"showing {target['line'].lower()}"

    return {
        "date": day,
        "hour": hour,
        "want": want,
        "target": target,
        "other": [l for l in lines if l is not target],
        "note": note,
    }


# ------------------------------------------------------------------- rendering

CSS = f"""
  @page {{ size: {W}px {H}px; margin: 0; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: {W}px; height: {H}px; background: #fff; color: #000;
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased; }}
  body {{ padding: 60px 68px; display: flex; flex-direction: column; }}
  .top {{ display: flex; justify-content: space-between; align-items: baseline;
          border-bottom: 4px solid #000; padding-bottom: 22px; }}
  .kicker {{ font-size: 30px; font-weight: 700; letter-spacing: 5px; text-transform: uppercase; }}
  .stamp {{ font-size: 30px; color: #333; text-align: right; line-height: 1.35; }}
  .rule {{ border-top: 3px solid #000; margin: 34px 0 30px; }}
  .bottom {{ margin-top: auto; }}
  .src {{ font-size: 26px; color: #555; text-align: right; }}
  .label {{ font-size: 26px; font-weight: 700; letter-spacing: 3px; text-transform: uppercase; color: #444; }}
  .card {{ border: 3px solid #000; padding: 26px 24px; }}
  .muted {{ color: #555; }}
  .now {{ font-size: 300px; font-weight: 700; line-height: 0.95; letter-spacing: -10px; }}
  .now sup {{ font-size: 104px; letter-spacing: 0; font-weight: 500; }}
  .up {{ color: #000; }}
  .down {{ color: #000; }}
  .big {{ font-size: 84px; font-weight: 700; line-height: 1.05; }}
  .huge {{ font-size: 150px; font-weight: 700; line-height: 1; letter-spacing: -5px; }}
  .sub {{ font-size: 34px; color: #555; line-height: 1.35; }}
  .row {{ display: flex; gap: 26px; }}
  td {{ padding: 16px 0; font-size: 34px; border-bottom: 2px solid #ddd; }}
  .ok::before {{ content: "\\2022"; padding-right: 14px; }}
  .notok {{ font-weight: 700; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }}
  .pane {{ border: 3px solid #000; padding: 20px 22px; }}
  .api {{ font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 22px;
          color: #444; padding-bottom: 8px; border-bottom: 2px solid #000; margin-bottom: 14px; }}
  .val {{ font-weight: 700; line-height: 1.1; }}
  .kv {{ display: flex; justify-content: space-between; gap: 14px; font-size: 27px;
         padding: 7px 0; border-bottom: 1px solid #ddd; }}
  .kv b {{ white-space: nowrap; }}
  .note {{ font-size: 26px; color: #444; line-height: 1.3; }}
  .cmt {{ margin-top: 8px; padding-left: 16px; border-left: 3px solid #999; }}
  .mblock {{ margin-top: 20px; }}
  .mblock:first-child {{ margin-top: 0; }}
  .mname {{ font-size: 34px; font-weight: 700; line-height: 1.15; }}
  .mitem {{ font-size: 30px; line-height: 1.18; padding-left: 12px; }}
  .tag {{ font-size: 21px; color: #555; border: 1px solid #999; padding: 0 5px; vertical-align: 3px; }}
"""


def skeleton(kicker: str, right: str, body: str, source: str) -> str:
    now = datetime.now()
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
  <div class="top">
    <div class="kicker">{escape(kicker)}</div>
    <div class="stamp">{now.strftime('%a %b %-d')}<br>{now.strftime('%H:%M')}</div>
  </div>
  <div style="margin-top:44px; flex:1; display:flex; flex-direction:column;">{body}</div>
  <div class="bottom">
    <div class="rule"></div>
    <div class="src">{escape(source)}</div>
  </div>
</body></html>"""


def sparkline(series, width=1040, height=300) -> str:
    """Inline SVG sparkline — no Pillow, no image files."""
    if len(series) < 2:
        return ""
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1
    pad = 12
    n = len(series)
    pts = []
    for i, v in enumerate(series):
        x = pad + (width - 2 * pad) * i / (n - 1)
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    first_y = pad + (height - 2 * pad) * (1 - (series[0] - lo) / span)
    last_y = pad + (height - 2 * pad) * (1 - (series[-1] - lo) / span)
    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"
     xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="0" width="{width}" height="{height}" fill="#fff" stroke="#000" stroke-width="3"/>
  <line x1="{pad}" y1="{first_y:.1f}" x2="{width-pad}" y2="{first_y:.1f}"
        stroke="#aaa" stroke-width="2" stroke-dasharray="10 8"/>
  <polyline points="{' '.join(pts)}" fill="none" stroke="#000" stroke-width="5"
        stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{pts[-1].split(',')[0]}" cy="{last_y:.1f}" r="9" fill="#000"/>
</svg>"""


def body_snow(d: dict) -> tuple[str, str]:
    arrow = "▲" if d["change"] >= 0 else "▼"
    hi, lo, price = d["hi30"], d["lo30"], d["price"]
    pos = 0 if hi == lo else (price - lo) / (hi - lo) * 100
    body = f"""
    <div class="now">{price:,.2f}</div>
    <div class="big" style="font-size:56px; margin-top:10px;">
      {arrow} {abs(d['change']):.2f} &nbsp;({escape(str(d['pct']))})
    </div>
    <div class="sub" style="margin-top:14px;">
      prev close {d['prev_close']:,.2f} &middot; volume {escape(str(d['volume']))}
    </div>

    <div class="rule"></div>
    <div class="label">last 30 days</div>
    <div style="flex:1; display:flex; align-items:center; margin-top:14px;">
      {sparkline(d['series'], height=440)}
    </div>
    <div class="row" style="margin-top:18px; justify-content:space-between;">
      <div class="sub">30d low <b>{lo:,.2f}</b></div>
      <div class="sub">30d high <b>{hi:,.2f}</b></div>
      <div class="sub">now at <b>{pos:.0f}%</b> of range</div>
    </div>
    """
    return ("snow / nyse", body, "source: Nasdaq quote + historical API · rendered locally")


def body_status(d: dict) -> tuple[str, str]:
    """Four panes, one per Statuspage endpoint, each labelled with the path it came from."""
    word = {"none": "all systems operational", "minor": "partially degraded",
            "major": "major outage", "critical": "critical outage"}.get(d["indicator"], d["indicator"])

    counts_html = "".join(
        f'<div class="kv"><span>{escape(k.replace("_", " "))}</span><b>{v}</b></div>'
        for k, v in sorted(d["counts"].items(), key=lambda kv: (kv[0] != "operational", -kv[1]))
    )

    comp_html = "".join(
        f'<div class="kv"><span>{escape(c["name"][:34])}</span><b>{escape(c["status"].replace("_", " "))}</b></div>'
        for c in (d["bad"] or d["notable"])
    ) or '<div class="note">no non-operational components</div>'

    if d["incidents"]:
        inc_html = "".join(
            f"""<div style="margin-top:12px;">
                  <div style="font-size:28px; font-weight:700;">{escape(i['name'])}</div>
                  <div class="note">{escape(i['impact'] or i['status'])} &middot; since {escape(i['created'])}
                    &middot; {i['n_updates']} updates</div>
                  <div class="note" style="margin-top:6px;">{escape(i['body'])}</div>
                </div>"""
            for i in d["incidents"]
        )
    else:
        inc_html = '<div class="note" style="margin-top:10px;">No unresolved incidents.</div>'

    body = f"""
    <div class="grid2" style="flex:1;">
      <div class="pane">
        <div class="api">/status.json</div>
        <div class="val" style="font-size:46px;">{escape(word)}</div>
        <div class="note" style="margin-top:10px; font-size:30px;">{escape(d['description'])}</div>
        <div class="note" style="margin-top:8px;">page updated {escape(d['page_updated'])}Z</div>
      </div>

      <div class="pane">
        <div class="api">/summary.json</div>
        <div class="kv"><span>indicator</span><b>{escape(d['summary_indicator'] or d['indicator'])}</b></div>
        <div class="kv"><span>components</span><b>{d['n_components']}</b></div>
        <div class="kv"><span>unresolved incidents</span><b>{d['n_incidents']}</b></div>
        <div class="kv"><span>scheduled maintenance</span><b>{d['n_maint']}</b></div>
      </div>

      <div class="pane">
        <div class="api">/components.json</div>
        <div class="note">{d['n_leaves']} leaf components</div>
        {counts_html}
      </div>

      <div class="pane">
        <div class="api">/incidents/unresolved.json</div>
        {inc_html}
      </div>
    </div>
    """
    return ("snowflake status", body, "source: status.snowflake.com Statuspage API · 4 endpoints")


def body_bellevue(d: dict) -> tuple[str, str]:
    fc = "".join(
        f"""<div class="card" style="flex:1;">
              <div style="font-size:40px; font-weight:700;">{f['day']}</div>
              <div class="sub" style="min-height:96px; margin-top:10px;">{escape(f['cond'])}</div>
              <div style="font-size:54px; font-weight:700; margin-top:8px;">{f['hi']}&deg;
                <span class="sub" style="font-size:36px;">{f['lo']}&deg;</span></div>
              <div class="sub" style="min-height:44px;">{'' if not f['pop'] or f['pop'] < 15 else str(f['pop']) + '% rain'}</div>
            </div>"""
        for f in d["forecast"]
    )
    body = f"""
    <div class="now">{d['temp']}<sup>&deg;F</sup></div>
    <div style="font-size:52px; font-weight:500; margin-top:6px;">{escape(d['cond'])}</div>
    <div class="sub" style="margin-top:12px; font-size:36px;">
      feels like {d['feels']}&deg; &middot; wind {d['wind']} mph &middot; {escape(PLACE)}
    </div>
    <div class="rule"></div>
    <div class="row">{fc}</div>
    <div class="rule"></div>
    <div class="row">
      <div class="card" style="flex:1;">
        <div class="label">air quality</div>
        <div class="big" style="margin-top:8px;">{d['aqi'] if d['aqi'] is not None else '—'}
          <span class="sub" style="font-size:34px;">US AQI</span></div>
        <div class="sub">{escape(d['aqi_word'])}
          {'&middot; PM2.5 ' + str(round(d['pm25'], 1)) + ' &micro;g/m&sup3;' if d['pm25'] is not None else ''}</div>
      </div>
      <div class="card" style="flex:1;">
        <div class="label">sun</div>
        <div class="big" style="margin-top:8px;">{escape(d['sunrise'])} &rarr; {escape(d['sunset'])}</div>
        <div class="sub">sunrise &middot; sunset (local)</div>
      </div>
    </div>
    """
    return ("bellevue, wa", body, "source: open-meteo forecast + air quality API · no key")


def body_blog(d: dict) -> tuple[str, str]:
    posts = "".join(
        f"""<div style="margin-top:28px;">
              <div style="font-size:39px; font-weight:700; line-height:1.2;">{escape(p['title'])}</div>
              <div class="note" style="margin-top:6px;">{escape(p['date'])}</div>
              <div class="note" style="margin-top:10px; font-size:29px;">{escape(p['desc'])}</div>
            </div>"""
        for p in d["posts"]
    ) or '<div class="note">feed unavailable</div>'
    body = f"""
    <div class="label">latest from snowflake</div>
    {posts}
    """
    return ("snowflake blog", body, "source: snowflake.com/feed")


def body_hn(d: dict) -> tuple[str, str]:
    def story(s: dict) -> str:
        cmts = "".join(
            f"""<div class="cmt"><span class="note" style="font-size:28px;">
                  <b>{escape(c['by'])}</b> {escape(c['text'])}</span></div>"""
            for c in s["comments"]
        ) or '<div class="note">no comments fetched</div>'
        return f"""<div style="margin-top:26px;">
              <div style="font-size:36px; font-weight:700; line-height:1.2;">{escape(s['title'])}</div>
              <div class="note" style="margin-top:5px;">{s['score']} pts &middot;
                {s.get('descendants') or 0} comments</div>
              {cmts}
            </div>"""

    body = f"""
    <div class="label">hacker news — top 3, with the comments</div>
    {"".join(story(s) for s in d["stories"]) or '<div class="note">HN unavailable</div>'}
    """
    return ("hacker news", body, "source: Hacker News API · topstories + top-level comments")


def body_xkcd(d: dict) -> tuple[str, str]:
    if not d:
        return ("xkcd", '<div class="note">xkcd unavailable</div>', "source: xkcd.com")
    body = f"""
    <div class="label">xkcd #{d['num']} — {escape(d['title'])}</div>
    <div style="flex:1; min-height:0; display:flex; align-items:center; justify-content:center;
                margin:14px -50px 0;">
      <img src="{escape(d['img'])}" style="width:100%; height:100%; object-fit:contain;" alt="">
    </div>
    <div class="note" style="margin-top:14px; font-size:29px;">{escape(d['alt'])}</div>
    """
    return (f"xkcd #{d['num']}", body, "source: xkcd.com")


def body_menu(d: dict) -> tuple[str, str]:
    if not d or not d.get("target"):
        return ("today's menu", '<div class="note">menu unavailable</div>', "source: eat.sifted.co")

    t = d["target"]

    def heading(m: dict) -> str:
        return f"{escape(m['brand'])} · {escape(m['menu'])}" if m["brand"] else escape(m["menu"])

    def rows(m: dict) -> str:
        return "".join(
            f'<div class="mitem">{escape(i["name"])}'
            f'{" <span class=tag>vg</span>" if i["veg"] else (" <span class=tag>v</span>" if i["vgt"] else "")}'
            f"</div>"
            for i in m["items"]
        )

    def block(m: dict) -> str:
        return f'<div class="mblock"><div class="mname">{heading(m)}</div>{rows(m)}</div>'

    def height(m: dict) -> int:
        """Rough line count, enough to balance the two columns without measuring them."""
        return 1 + sum(max(1, -(-len(i["name"]) // 30)) for i in m["items"])

    # Largest station first, each into whichever column is currently shorter: a big one such
    # as the continental spread needs a column to itself, and this stops it being stranded
    # behind small stations. Display order within each column is restored afterwards.
    order = {id(m): k for k, m in enumerate(t["menus"])}
    cols: list = [[], []]
    loads = [0, 0]
    for m in sorted(t["menus"], key=height, reverse=True):
        j = 0 if loads[0] <= loads[1] else 1
        cols[j].append(m)
        loads[j] += height(m)
    for col in cols:
        col.sort(key=lambda m: order[id(m)])

    columns = "".join(
        f'<div style="display:flex; flex-direction:column;">'
        f'{"".join(block(m) for m in col)}</div>'
        for col in cols
    )

    rest = ""
    if d["other"]:
        names = ", ".join(
            escape(m["brand"] or m["menu"]) for l in d["other"] for m in l["menus"]
        )
        rest = (f'<div class="note" style="margin-top:12px; font-size:24px;">'
                f'<b>{escape("/".join(l["line"].lower() for l in d["other"]))}:</b> {names}</div>')

    window = "until 11:00" if d["want"] == "breakfast" else "from 11:00"
    note = f" · {escape(d['note'])}" if d.get("note") else ""
    body = f"""
    <div class="label">{d['date'].strftime('%A %B %-d')} · bellevue lodge · {window}{note}</div>
    <div class="grid2" style="flex:1; margin-top:16px;">{columns}</div>
    {rest}
    """
    return (f"lodge {t['line'].lower()}", body,
            "source: eat.sifted.co market API · all options, every condiment included")


BUILDERS = {
    "snow": (data_snow, body_snow),
    "status": (data_status, body_status),
    "bellevue": (data_bellevue, body_bellevue),
    "menu": (data_menu, body_menu),
    "blog": (data_blog, body_blog),
    "hn": (data_hn, body_hn),
    "xkcd": (data_xkcd, body_xkcd),
}


# --------------------------------------------------------------------- chrome

def render_png(panel: str, out: Path) -> Path:
    fetcher, builder = BUILDERS[panel]
    kicker, body, source = builder(fetcher())
    html = skeleton(kicker, "", body, source)
    html_path = OUT / f"panel-{panel}.html"
    html_path.write_text(html)

    profile = OUT / ".chrome-profile"
    shutil.rmtree(profile, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--disable-extensions", "--force-device-scale-factor=1", f"--window-size={W},{H}",
        f"--user-data-dir={profile}", "--default-background-color=FFFFFF",
        "--virtual-time-budget=6000",
        f"--screenshot={out}", html_path.as_uri(),
    ]
    # Headless Chrome writes the screenshot and then routinely fails to exit, so waiting
    # for the process costs ~2 minutes per frame. Watch for the file instead and kill it.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 90
    last_size, stable_since = -1, time.time()
    while time.time() < deadline:
        if out.exists():
            size = out.stat().st_size
            if size != last_size:
                last_size, stable_since = size, time.time()
            elif size > 0 and time.time() - stable_since > 0.7:
                break  # written and no longer growing
        if proc.poll() is not None and out.exists():
            break
        time.sleep(0.25)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    if not out.exists():
        raise SystemExit("chrome failed to produce a screenshot")
    return out


def slot_panel(now: float | None = None) -> str:
    idx = int(((now or time.time()) // (ROTATE_MINUTES * 60)) % len(PANELS))
    return PANELS[idx]


if __name__ == "__main__":
    args = sys.argv[1:]
    panel = None
    out = OUT / "dash.png"
    publish = False
    publish_all = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--panel":
            panel = args[i + 1]; i += 2
        elif a == "--out":
            out = Path(args[i + 1]); i += 2
        elif a == "--publish":
            publish = True; i += 1
        elif a == "--rotate":
            panel = slot_panel(); i += 1
        elif a == "--urls":
            print(" ".join(frame_urls())); raise SystemExit(0)
        elif a == "--conf":
            # The device config, whole: the shell script that publishes it only forwards this,
            # so there is exactly one definition of the URL list and the interval.
            print(conf_text(), end=""); raise SystemExit(0)
        elif a == "--publish-all":
            publish_all = True; i += 1
        elif a == "--publish-slot":
            # What the scheduler runs: work out the current slot from the clock, render it
            # and publish it to that panel's own path.
            panel = slot_panel(); publish = True; i += 1
            out = OUT / frame_name(panel)
            os.environ["GH_PATH"] = out.name
        else:
            raise SystemExit(f"unknown argument: {a}")

    warn = freshness_warning()
    if warn:
        print(warn)

    if publish_all:
        # Run after the code changes: republish every frame, so a panel that was just added
        # appears now instead of when its slot next comes round (up to a full cycle later),
        # and edits show up on every panel rather than one per minute.
        sys.path.insert(0, str(OUT))
        import publish as pub  # noqa: E402
        mirror = os.environ.pop("GH_MIRROR", None)   # a mirror push per panel is pointless
        try:
            for name in PANELS:
                png = render_png(name, OUT / frame_name(name))
                os.environ["GH_PATH"] = png.name
                print(f"rev={revision()} panel={name} -> {png.name} ({png.stat().st_size} bytes)")
                pub.publish(png)
        finally:
            if mirror:
                os.environ["GH_MIRROR"] = mirror
        raise SystemExit(0)

    if panel is None:
        raise SystemExit(f"usage: render_panels.py --panel {{{'|'.join(PANELS)}}}|--rotate [--out PNG] [--publish]")
    if panel not in BUILDERS:
        raise SystemExit(f"unknown panel {panel!r}")

    started = time.time()
    png = render_png(panel, out)
    print(f"rev={revision()} panel={panel} -> {png} ({png.stat().st_size} bytes) "
          f"in {time.time() - started:.1f}s")

    if publish:
        sys.path.insert(0, str(OUT))
        import publish as pub  # noqa: E402
        url = pub.publish(png)
        print(f"published: {url}")

        # Compatibility mirror. Any device still configured with a single DASH_URL keeps
        # moving (at the CDN's ~5 min, out of order) instead of freezing on a file that
        # nobody updates any more. Set GH_MIRROR=dash.png only while such a device is in
        # the field; unset it once every device uses the per-panel paths.
        mirror = os.environ.get("GH_MIRROR", "").strip().lstrip("/")
        if mirror:
            os.environ["GH_PATH"] = mirror
            print(f"mirrored: {pub.publish(png)}")
