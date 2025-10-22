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
from datetime import datetime, timedelta
import os
import re
from pathlib import Path
from shutil import which
import tempfile
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

# Paths
CONFIG_PATH = Path("/app/config")
FEED_FILE = CONFIG_PATH / "feeds.txt"
COVER_FILE = CONFIG_PATH / "cover.png"  # intentionally unused until EPUB delivery succeeds

# Templates
HTML_HEAD = u"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>THE DAILY NEWS</title>
</head>
<body>
"""
HTML_TAIL = u"""
</body>
</html>
"""
HTML_PER_POST = u"""
<article>
  <h1><a href="{link}">{title}</a></h1>
  <p><small>By {author} for <i>{blog}</i>, on {nicedate} at {nicetime}.</small></p>
  {body}
</article>
"""

# Sanitisation regexes
BAD_TAGS_RE = re.compile(r"</?(script|style|iframe|svg|object|embed)[^>]*>", re.IGNORECASE)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


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
    # Avoid HTML entities that can confuse Kindle’s parser
    return dt.strftime("%I:%M %p").strip("0").lower()


def nicepost(post):
    d = post._asdict()
    d["nicedate"] = nicedate(d["time"])
    d["nicetime"] = nicehour(d["time"])
    return d


def sanitise_html(html: str) -> str:
    html = html.replace("&thinsp;", " ")
    html = BAD_TAGS_RE.sub("", html)
    html = IMG_TAG_RE.sub("", html)
    return html


def build_epub_kindlesafe(html_text: str, out_path: Path) -> Path:
    """
    Build a Kindle-safe EPUB using Calibre's ebook-convert (preferred).
    Falls back to a minimal pandoc conversion if Calibre is unavailable.
    """
    safe_html = sanitise_html(html_text)

    # Preferred path: Calibre
    if which("ebook-convert"):
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp_html:
            tmp_html.write(safe_html)
            tmp_html_path = tmp_html.name

        # Target EPUB2 packaging; avoid auto-cover; be strict about encoding
        cmd = [
            "ebook-convert",
            tmp_html_path,
            str(out_path),
            "--input-encoding", "utf-8",
            "--epub-version", "2",
            "--no-default-epub-cover",
        ]
        # Quietly run; we don't need verbose output
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return out_path
        finally:
            try:
                os.remove(tmp_html_path)
            except OSError:
                pass

    # Fallback: minimal pandoc (no unsupported flags)
    os.environ["PYPANDOC_PANDOC"] = PANDOC
    pypandoc.convert_text(
        safe_html,
        to="epub",
        format="html",
        outputfile=str(out_path),
        extra_args=[
            "--standalone",
            "--toc",
            "--metadata=title:THE DAILY NEWS",
            "--metadata=language:en-GB",
        ],
    )
    return out_path


def send_mail(send_from, send_to, subject, text, files):
    msg = MIMEMultipart()
    # Ensure simple ASCII-encoded headers
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
        logging.info("Compiling newspaper")
        html = HTML_HEAD + "\n".join(
            [HTML_PER_POST.format(**nicepost(p)) for p in posts]
        ) + HTML_TAIL

        logging.info("Creating epub")
        raw_epub = Path("dailynews.epub")
        final_epub = build_epub_kindlesafe(html, raw_epub)

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
                subject="Daily News",
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
