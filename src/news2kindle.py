#!/usr/bin/env python
# encoding: utf-8

# Daily EPUB generator with ChatGPT-written summary (weather + agenda + top UK headlines),
# Kindle-safe HTML, and email delivery via Gmail SMTP (STARTTLS).

from email.utils import COMMASPACE, formatdate, formataddr
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import smtplib
import pypandoc
import pytz
import time
import logging
import subprocess
from datetime import datetime, timedelta, date
import os
import re
import tempfile
import html
import json
from pathlib import Path
from shutil import which
import requests
from openai import OpenAI
from icalendar import Calendar
from dateutil.tz import gettz
from FeedparserThread import FeedparserThread

logging.basicConfig(level=logging.INFO)

# Environment
EMAIL_SMTP = os.getenv("EMAIL_SMTP", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWD = os.getenv("EMAIL_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER)
KINDLE_EMAIL = os.getenv("KINDLE_EMAIL")
PANDOC = os.getenv("PANDOC_PATH", "/usr/bin/pandoc")
PERIOD = int(os.getenv("UPDATE_PERIOD", 12))  # minutes between runs (12 => 12 minutes). Adjust if you intend hours.

DOC_TITLE = os.getenv("DOC_TITLE", "Daily News")
DOC_AUTHOR = os.getenv("DOC_AUTHOR", "News2Kindle")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Paths
CONFIG_PATH = Path("/app/config")
FEED_FILE = CONFIG_PATH / "feeds.txt"
CAL_FILE = CONFIG_PATH / "calendars.txt"  # list of secret iCal URLs, one per line

# Timezone
LONDON_TZ = gettz("Europe/London")

# HTML templates
HTML_HEAD_TEMPLATE = u"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{DOC_TITLE}</title>
  <style>
    body { font-family: serif; line-height: 1.4; }
    h1,h2,h3 { margin-top: 1.2em; }
    .k-card { padding: 0.6em 0.8em; border: 1px solid #ddd; border-radius: 4px; }
    .muted { color: #555; }
    ol.headlines { padding-left: 1.2em; }
    ol.headlines li { margin: 0.4em 0; }
    /* Article layout */
    article { margin: 1em 0; }
  </style>
</head>
<body>
"""
HTML_HEAD = HTML_HEAD_TEMPLATE.replace("{DOC_TITLE}", html.escape(DOC_TITLE))

HTML_TAIL = u"""
</body>
</html>
"""

HTML_PER_POST = u"""
<article id="post-{idx}">
  <h2><a href="{link}">{title}</a></h2>
  <p class="muted"><small>By {author} for <i>{blog}</i>, on {nicedate} at {nicetime}.</small></p>
  {body}
</article>
"""

# Sanitisation regexes for feed fragments only
BAD_TAGS_RE = re.compile(r"</?(script|style|iframe|svg|object|embed|noscript|video|audio)[^>]*>", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")

# Weather (Open-Meteo)
LAT, LON = 51.4816, -3.1791
OPEN_METEO = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max"
    "&timezone=Europe%2FLondon"
)

WEATHERCODE = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight showers",
    81: "Moderate showers",
    82: "Violent showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


# ----------------------------
# Feeds
# ----------------------------

def load_feeds():
    if not FEED_FILE.exists():
        return []
    with open(FEED_FILE, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def update_start(now):
    new_now = time.mktime(now.timetuple())
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FEED_FILE, "a", encoding="utf-8"):
        os.utime(FEED_FILE, (new_now, new_now))


def get_start(fname: Path):
    if not fname.exists():
        # default to 24h ago if no file yet
        return pytz.utc.localize(datetime.utcnow() - timedelta(hours=24))
    return pytz.utc.localize(
        datetime.fromtimestamp(os.path.getmtime(fname)) - timedelta(hours=24)
    )


def get_posts_list(feed_list, start_dt):
    posts = []
    ths = []
    for url in feed_list:
        th = FeedparserThread(url, start_dt, posts)
        ths.append(th)
        th.start()
    for th in ths:
        th.join()
    return posts


def nicedate(dt):
    return dt.strftime("%d %B %Y").strip("0")


def nicehour(dt):
    return dt.strftime("%I:%M %p").strip("0").lower()


def sanitise_fragment(html_text: str) -> str:
    """Clean feed content fragments; safe to inject inside <body>. Do not use on full document."""
    html_text = html_text.replace("&thinsp;", " ")
    html_text = BAD_TAGS_RE.sub("", html_text)
    html_text = IMG_TAG_RE.sub("", html_text)
    return html_text


def nicepost(post, idx):
    d = post._asdict()
    d["nicedate"] = nicedate(d["time"])
    d["nicetime"] = nicehour(d["time"])
    d["idx"] = idx
    d["title"] = d.get("title") or "Untitled"
    d["author"] = d.get("author") or "Unknown"
    d["blog"] = d.get("blog") or "Source"
    d["body"] = sanitise_fragment(d.get("body") or "")
    return d


def html_to_text_one_sentence(html_text: str, max_chars: int = 220) -> str:
    t = TAG_RE.sub(" ", html_text)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    m = re.search(r"(.+?[\.!?])(\s|$)", t)
    s = m.group(1) if m else t
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s


# ----------------------------
# Calendar (ICS without OAuth)
# ----------------------------

def load_calendar_urls():
    if not CAL_FILE.exists():
        return []
    with open(CAL_FILE, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def _to_dt_local(v):
    if hasattr(v, "hour"):
        dt = v
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt.astimezone(LONDON_TZ)
    return datetime(v.year, v.month, v.day, 0, 0, tzinfo=LONDON_TZ)


def _is_today_local(dt):
    return dt.date() == datetime.now(LONDON_TZ).date()


def fetch_todays_events_struct():
    urls = load_calendar_urls()
    events = []
    for url in urls:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            cal = Calendar.from_ical(r.content)
            for comp in cal.walk("VEVENT"):
                start = comp.decoded("DTSTART")
                end = comp.decoded("DTEND", None)
                dt_start = _to_dt_local(start)
                if not _is_today_local(dt_start):
                    continue
                dt_end = _to_dt_local(end) if end is not None else None
                title = str(comp.get("SUMMARY", "Untitled"))
                if (title == "Calendar block"):
                    continue
                loc = str(comp.get("LOCATION", "")).strip()
                all_day = not hasattr(start, "hour")
                events.append({
                    "start": "All day" if all_day else dt_start.strftime("%H:%M"),
                    "end": (dt_end.strftime("%H:%M") if (dt_end and not all_day and dt_end.date()==dt_start.date()) else None),
                    "title": title,
                    "location": loc or None,
                    "all_day": all_day,
                })
        except Exception:
            continue
    # Order: all-day first, then by time
    def sort_key(e):
        if e["all_day"]:
            return ("", "")  # all-day first
        return (e["start"], e["end"] or "")
    events.sort(key=sort_key)
    return events


# ----------------------------
# Weather (data for model)
# ----------------------------

def fetch_cardiff_weather_data():
    url = OPEN_METEO.format(lat=LAT, lon=LON)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        d = data["daily"]
        today_str = date.today().isoformat()
        idx = d["time"].index(today_str)
        code = int(d["weathercode"][idx])
        return {
            "description": WEATHERCODE.get(code, "Weather"),
            "code": code,
            "tmax_c": round(d["temperature_2m_max"][idx]),
            "tmin_c": round(d["temperature_2m_min"][idx]),
            "rain_mm": round(d["precipitation_sum"][idx], 1),
            "wind_kmh": (round(d.get("windspeed_10m_max", [None])[idx])
                         if d.get("windspeed_10m_max") else None),
        }
    except Exception:
        return None


# ----------------------------
# ChatGPT daily summary (weather + agenda + top 3 UK headlines)
# ----------------------------

def build_chatgpt_summary_html(weather, events):
    """
    Conversational two-paragraph summary fragment (no headings/lists).
    Kindle-safe: only <p> tags.
    """
    if not OPENAI_API_KEY:
        # Minimal fallback
        parts = []
        if weather:
            w = weather
            rain = f"{w['rain_mm']} mm" if w.get("rain_mm") is not None else "—"
            wind = f"{w['wind_kmh']} km/h" if w.get("wind_kmh") is not None else "—"
            parts.append(f"Cardiff: {w['description']}. Max {w['tmax_c']}°C, min {w['tmin_c']}°C. Rain {rain}. Wind {wind}.")
        if events:
            agenda_bits = []
            for e in events[:5]:
                when = e["start"] if e["all_day"] else (e["start"] + (f"–{e['end']}" if e["end"] else ""))
                loc = f" · {e['location']}" if e.get("location") else ""
                agenda_bits.append(f"{when} — {e['title']}{loc}")
            parts.append("Today: " + "; ".join(html.escape(x) for x in agenda_bits) + ".")
        # Headlines: uncertainty placeholder
        return "<p>" + " ".join(parts) + "</p><p>Top stories: Uncertain · BBC/Guardian/The Times; Uncertain · BBC/Guardian/The Times; Uncertain · BBC/Guardian/The Times.</p>"

    client = OpenAI(api_key=OPENAI_API_KEY)

    payload = {
        "date_local": datetime.now(LONDON_TZ).strftime("%A %d %B %Y"),
        "location": "Cardiff, UK",
        "weather": weather,
        "agenda": events[:6],  # keep short
    }

    system_msg = (
        "You are a concise British daily-brief writer. "
        "Return exactly TWO HTML <p> paragraphs, no other tags. "
        "Paragraph 1: a warm, direct opener that weaves in Cardiff weather (description, max/min °C, rain mm, wind km/h) "
        "and a compact view of the day’s agenda (time ranges and titles; at most ~6 items, separated by semicolons). "
        "Paragraph 2: the top three UK national headlines as short clauses, each with '· Source' (BBC News, The Times, or The Guardian). "
        "If uncertain about exact titles, write 'Uncertain · BBC/Guardian/The Times' rather than guessing. "
        "No emojis. British spelling. ~120–180 words total."
    )

    user_msg = f"DATA (JSON): {json.dumps(payload, ensure_ascii=False)}"

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": system_msg},
                  {"role": "user", "content": user_msg}],
        temperature=0.5,
        top_p=0.9,
        presence_penalty=0.2,
        frequency_penalty=0.2,
        max_tokens=600,
    )
    frag = resp.choices[0].message.content.strip()
    logging.info("GPT response: %s", frag)
    # Guard: if model returned extra tags, strip to inner <p>…</p>
    if "<html" in frag.lower() or "<body" in frag.lower():
        frag = re.sub(r"(?is).*<body[^>]*>(.*)</body>.*", r"\1", frag)
    # Ensure only <p> tags remain
    frag = re.sub(r"(?is)\s*(?!<p>)(?!</p>)[^<]+", lambda m: html.escape(m.group(0)), frag)
    return frag




# ----------------------------
# EPUB build and email
# ----------------------------

def build_epub_kindlesafe(html_text: str, out_path: Path) -> Path:
    # Prefer calibre's ebook-convert to produce EPUB2
    if which("ebook-convert"):
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp_html:
            tmp_html.write(html_text)
            tmp_html_path = tmp_html.name
        cmd = [
            "ebook-convert",
            tmp_html_path,
            str(out_path),
            "--input-encoding", "utf-8",
            "--epub-version", "2",
            "--no-default-epub-cover",
            "--title", DOC_TITLE,
            "--authors", DOC_AUTHOR,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if which("ebook-meta"):
                subprocess.run(
                    ["ebook-meta", str(out_path), "--title", DOC_TITLE, "--authors", DOC_AUTHOR],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            return out_path
        finally:
            try:
                os.remove(tmp_html_path)
            except OSError:
                pass

    # Fallback to pandoc minimal if calibre missing
    os.environ["PYPANDOC_PANDOC"] = PANDOC
    pypandoc.convert_text(
        html_text,
        to="epub",
        format="html",
        outputfile=str(out_path),
        extra_args=[
            "--standalone",
            "--toc",
            f"--metadata=title:{DOC_TITLE}",
            f"--metadata=author:{DOC_AUTHOR}",
            "--metadata=language:en-GB",
        ],
    )
    return out_path


def send_mail(send_from, send_to, subject, text, files):
    msg = MIMEMultipart()
    msg["From"] = formataddr((str(Header("", "utf-8")), send_from))
    msg["To"] = COMMASPACE.join(send_to)
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain", "utf-8"))

    for f in files or []:
        fpath = Path(f)
        with open(fpath, "rb") as fil:
            data = fil.read()
        if fpath.suffix.lower() == ".epub":
            part = MIMEApplication(data, _subtype="epub+zip", Name=fpath.name)
        else:
            part = MIMEApplication(data, Name=fpath.name)
        part.add_header("Content-Disposition", f'attachment; filename="{fpath.name}"')
        msg.attach(part)

    smtp = smtplib.SMTP(EMAIL_SMTP, EMAIL_SMTP_PORT)
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    smtp.login(EMAIL_USER, EMAIL_PASSWD)
    smtp.sendmail(send_from, send_to, msg.as_string())
    smtp.quit()


# ----------------------------
# Main loop
# ----------------------------

def do_one_round():
    now = pytz.utc.localize(datetime.utcnow())
    start = get_start(FEED_FILE)

    # Pull posts (still used for the Articles section)
    feeds = load_feeds()
    posts = get_posts_list(feeds, start) if feeds else []
    posts.sort()

    # Build ChatGPT summary (weather + agenda + top UK headlines)
    weather_data = fetch_cardiff_weather_data()
    events = fetch_todays_events_struct()
    summary_html = build_chatgpt_summary_html(weather_data, events)

    # Build articles HTML from feeds (optional; skip if no posts)
    if posts:
        articles_html = "\n".join(
            [HTML_PER_POST.format(**nicepost(p, i)) for i, p in enumerate(posts, start=1)]
        )
        body_html = (
            HTML_HEAD
            + summary_html
            + "\n<h1>Articles</h1>\n"
            + articles_html
            + HTML_TAIL
        )
    else:
        body_html = HTML_HEAD + summary_html + HTML_TAIL

    # Create EPUB
    stamp = datetime.now(LONDON_TZ).strftime("%Y-%m-%d")
    out_name = f"{DOC_TITLE.lower().replace(' ', '')}-{stamp}.epub"
    raw_epub = Path(out_name)
    final_epub = build_epub_kindlesafe(body_html, raw_epub)

    size = final_epub.stat().st_size
    if not size:
        logging.error("EPUB is empty; aborting send")
    elif size > 50 * 1024 * 1024:
        logging.error("EPUB exceeds 50 MB; aborting send")
    else:
        logging.info("Sending to Kindle")
        send_mail(
            send_from=EMAIL_FROM,
            send_to=[KINDLE_EMAIL],
            subject=DOC_TITLE,
            text="Your daily news.",
            files=[str(final_epub)],
        )
        logging.info("Sent to Kindle")

    # Cleanup
    for p in {raw_epub, final_epub}:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    # Mark timestamp
    update_start(now)


if __name__ == "__main__":
    while True:
        do_one_round()
        # Note: PERIOD was previously hours; here it's minutes for faster iteration.
        # If you want hours, change to: time.sleep(PERIOD * 60 * 60)
        time.sleep(PERIOD * 60)
