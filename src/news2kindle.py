#!/usr/bin/env python
# encoding: utf-8

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
from pathlib import Path
from shutil import which
import requests
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
PERIOD = int(os.getenv("UPDATE_PERIOD", 12))  # hours between RSS pulls

DOC_TITLE = "Today's headlines"
DOC_AUTHOR = "22nd October 2025"

# Paths
CONFIG_PATH = Path("/app/config")
FEED_FILE = CONFIG_PATH / "feeds.txt"
COVER_FILE = CONFIG_PATH / "cover.png"  # optional; currently unused

# HTML templates
HTML_HEAD = u"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{doc_title}</title>
  <style>
    body { font-family: serif; line-height: 1.4; }
    h1,h2,h3 { margin-top: 1.2em; }
    .k-card { padding: 0.6em 0.8em; border: 1px solid #ddd; border-radius: 4px; }
    .muted { color: #555; }
    ol.headlines { padding-left: 1.2em; }
    ol.headlines li { margin: 0.4em 0; }
    /* Weather card */
    .weather-card { display: table; width: 100%; }
    .weather-left { display: table-cell; width: 3.5em; vertical-align: middle; text-align: center; }
    .weather-right { display: table-cell; vertical-align: middle; padding-left: 0.8em; }
    .wx-icon { font-size: 300%; line-height: 1; }
    .wx-summary { font-weight: bold; margin: 0 0 0.2em 0; }
    .wx-meta { margin: 0.1em 0; }
  </style>
</head>
<body>
""".format(doc_title=html.escape(DOC_TITLE))

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

# Cardiff, UK constants (Europe/London)
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

def weather_icon(code: int) -> str:
    # Plain Unicode glyphs that render on Kindle
    if code in (0,):
        return "☀︎"
    if code in (1, 2):
        return "☀︎"  # avoid emoji fonts on some Kindles
    if code in (3,):
        return "☁︎"
    if code in (45, 48):
        return "〰"
    if code in (51, 53, 55, 56, 57):
        return "☂︎"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "☂︎"
    if code in (71, 73, 75, 77, 85, 86):
        return "❄︎"
    if code in (95, 96, 99):
        return "⚡︎"
    return "☁︎"

def load_feeds():
    with open(FEED_FILE, "r", encoding="utf-8") as f:
        return list(f)

def update_start(now):
    new_now = time.mktime(now.timetuple())
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FEED_FILE, "a", encoding="utf-8"):
        os.utime(FEED_FILE, (new_now, new_now))

def get_start(fname: Path):
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

def build_headlines_section(posts):
    items = []
    for i, post in enumerate(posts, start=1):
        p = post._asdict()
        title = html.escape(p.get("title") or "Untitled")
        excerpt = html_to_text_one_sentence(p.get("body") or "")
        items.append(
            f'<li><a href="#post-{i}">{title}</a><br>'
            f'<span class="muted">{html.escape(excerpt)}</span></li>'
        )
    return (
        "<h1>Today’s headlines</h1>"
        '<div class="k-card"><ol class="headlines">'
        + "\n".join(items)
        + "</ol></div>"
    )

def fetch_cardiff_weather_html() -> str:
    url = OPEN_METEO.format(lat=LAT, lon=LON)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        d = data["daily"]
        today_str = date.today().isoformat()
        idx = d["time"].index(today_str)

        code = int(d["weathercode"][idx])
        icon = weather_icon(code)
        desc = WEATHERCODE.get(code, "Weather")

        tmax = round(d["temperature_2m_max"][idx])
        tmin = round(d["temperature_2m_min"][idx])
        rain = round(d["precipitation_sum"][idx], 1)
        wind = d.get("windspeed_10m_max", [None])[idx]
        wind_txt = f"{round(wind)} km/h" if wind is not None else "—"

        summary = f"{desc} · max {tmax}°C · min {tmin}°C"
        meta1 = f"Rain {rain} mm"
        meta2 = f"Wind {wind_txt}"

        return (
            "<h1>Personal summary</h1>"
            '<div class="k-card weather-card">'
            f'  <div class="weather-left"><span class="wx-icon">{icon}</span></div>'
            '  <div class="weather-right">'
            '    <p class="wx-summary">Cardiff today</p>'
            f'    <p class="wx-meta">{html.escape(summary)}</p>'
            f'    <p class="wx-meta">{html.escape(meta1)} · {html.escape(meta2)}</p>'
            '  </div>'
            '</div>'
        )
    except Exception:
        return (
            "<h1>Personal summary</h1>"
            '<div class="k-card"><p>Weather unavailable.</p></div>'
        )

def build_epub_kindlesafe(html_text: str, out_path: Path) -> Path:
    # Do not sanitise the full document here; we need <style> to remain in <head>.
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
            # Enforce metadata with calibre if available
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

def do_one_round():
    now = pytz.utc.localize(datetime.now())
    start = get_start(FEED_FILE)

    logging.info(f"Collecting posts since {start}")
    posts = get_posts_list(load_feeds(), start)
    posts.sort()
    logging.info(f"Downloaded {len(posts)} posts")

    if posts:
        # Build summaries
        headlines_html = build_headlines_section(posts)
        weather_html = fetch_cardiff_weather_html()

        # Build articles
        articles_html = "\n".join(
            [HTML_PER_POST.format(**nicepost(p, i)) for i, p in enumerate(posts, start=1)]
        )

        # Assemble document
        html_doc = (
            HTML_HEAD
            + weather_html
            + "\n"
            + headlines_html
            + "\n"
            + "<h1>Articles</h1>\n"
            + articles_html
            + HTML_TAIL
        )

        logging.info("Creating epub")
        stamp = datetime.now().strftime("%Y-%m-%d")
        raw_epub = Path(f"{DOC_TITLE.lower().replace(' ', '')}-{stamp}.epub")
        final_epub = build_epub_kindlesafe(html_doc, raw_epub)

        size = final_epub.stat().st_size
        if not size:
            logging.error("EPUB is empty; aborting send")
        elif size > 50 * 1024 * 1024:
            logging.error("EPUB exceeds 50 MB; aborting send")
        else:
            logging.info("Sending to kindle email")
            send_mail(
                send_from=EMAIL_FROM,
                send_to=[KINDLE_EMAIL],
                subject=DOC_TITLE,
                text="Your daily news.",
                files=[str(final_epub)],
            )

        logging.info("Cleaning up...")
        for p in {raw_epub, final_epub}:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    logging.info("Finished.")
    update_start(now)

if __name__ == "__main__":
    while True:
        do_one_round()
        time.sleep(PERIOD * 60)
