"""
Lebanese news bulletin: finds new stories on Elnashra (elnashra.com) and the National
News Agency (nna-leb.gov.lb), reads them aloud in Arabic as MP3 (with a chime +
"الخبر الأول/الثاني..." between items) and sends the MP3 to a Telegram chat. The stories
already sent are remembered in state.json, so each one is sent once even when a run
starts late. Long bulletins are split into several files to fit Telegram's size limit.

Usage:
    python news_bulletin.py              # send stories that have not been sent yet
    python news_bulletin.py --no-send    # only build the MP3 locally
    python news_bulletin.py --minutes 60 # use the stories from the last 60 minutes instead
"""

import argparse
import asyncio
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import edge_tts
import imageio_ffmpeg
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
HEADERS = {"User-Agent": "personal-news-bulletin/1.0"}
ELNASHRA_HOME = "https://www.elnashra.com/"
ELNASHRA_SITEMAP = "https://www.elnashra.com/sitemap.xml"
NNA_SITEMAP = "https://nna-leb.gov.lb/ar/sitemap/news.xml"
REQUEST_DELAY = 1.0  # seconds between requests to a site
STATE_SIZE = 1500  # story ids to remember per source; must be more than a sitemap lists
TELEGRAM_MAX_BYTES = 45 * 1024 * 1024  # Telegram bots can send up to 50 MB per file

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "minutes": 60,  # how far back to look on the very first run
    "voice": "ar-SA-HamedNeural",
    "max_items": 60,  # stories per source per run; any extra ones go in the next bulletin
    "max_chars_per_item": 700,
    "send_when_empty": False,
    "output_dir": "output",
}

ORDINALS = ["الأول", "الثاني", "الثالث", "الرابع", "الخامس", "السادس", "السابع", "الثامن",
            "التاسع", "العاشر", "الحادي عشر", "الثاني عشر", "الثالث عشر", "الرابع عشر",
            "الخامس عشر", "السادس عشر", "السابع عشر", "الثامن عشر", "التاسع عشر", "العشرون"]


@dataclass
class NewsItem:
    id: int
    url: str
    title: str
    source: str
    body: str = ""


@dataclass
class Source:
    name: str  # as read out in the bulletin
    find: Callable[[], dict[int, tuple[str, float | None]]]  # {id: (url, unix time if known)}
    read: Callable[[str, int], tuple[str, str]]  # (page html, max chars) -> (headline, text)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    cfg["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", cfg["telegram_bot_token"])
    cfg["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", cfg["telegram_chat_id"])
    return cfg


def load_state() -> dict[str, set[int]]:
    """The story ids already sent, per source."""
    if not STATE_PATH.exists():
        return {}
    return {name: set(ids) for name, ids in json.loads(STATE_PATH.read_text(encoding="utf-8")).items()}


def save_state(state: dict[str, set[int]]) -> None:
    newest = {name: sorted(ids, reverse=True)[:STATE_SIZE] for name, ids in sorted(state.items())}
    STATE_PATH.write_text(json.dumps(newest) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- sites

def fetch(url: str) -> str:
    """One request at a time, spaced out, waiting longer if the site asks us to slow down."""
    for attempt in range(4):
        time.sleep(REQUEST_DELAY)
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code not in (429, 503):
            break
        wait = r.headers.get("Retry-After", "")
        time.sleep(int(wait) if wait.isdigit() else 15 * (attempt + 1))
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def elnashra_stories() -> dict[int, tuple[str, float | None]]:
    """The homepage is always up to date. The sitemap can lag behind it, but reaches far
    enough back to catch up after missed runs."""
    soup = BeautifulSoup(fetch(ELNASHRA_HOME), "html.parser")
    home = {}
    for story in soup.select("li.newsfeed-main[data-id][data-timestamp]"):
        link = story.select_one("a[href*='/news/show/']")
        if link:
            home[int(story["data-id"])] = (link["href"], float(story["data-timestamp"]))
    if not home:
        raise RuntimeError("no stories found on the Elnashra homepage; the layout may have changed")
    try:
        xml = fetch(ELNASHRA_SITEMAP)
    except requests.RequestException as e:
        log(f"could not read the Elnashra sitemap, using the homepage only: {e}")
        return home
    older = {int(story_id): (url, None) for url, story_id in
             re.findall(r"<loc>(https://www\.elnashra\.com/news/show/(\d+)/[^<]*)</loc>", xml)}
    return {**older, **home}


def nna_stories() -> dict[int, tuple[str, float | None]]:
    """The sitemap has one list per category and per day. Its feed at /ar/rss has stopped
    updating, and its /api/ path is off limits in robots.txt."""
    entry = re.compile(r"<loc>([^<]+)</loc>\s*<lastmod>(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
    now = datetime.now(timezone.utc)

    def unix_time(lastmod: str) -> float:
        # the site labels these times +02:00, but they are really UTC
        return datetime.fromisoformat(lastmod).replace(tzinfo=timezone.utc).timestamp()

    # daily lists are named by Lebanese date, which is ahead of UTC
    today = now + timedelta(hours=3)
    days = [f"{today:%Y-%m-%d}", f"{today - timedelta(days=1):%Y-%m-%d}"]
    stories = {}
    categories = entry.findall(fetch(NNA_SITEMAP))
    if not categories:
        raise RuntimeError("no categories found in the NNA sitemap; the layout may have changed")
    for url, lastmod in categories:
        if unix_time(lastmod) < now.timestamp() - 3 * 86400:
            continue  # nothing new in this category, so skip its requests
        category = url.rsplit("/", 1)[-1]
        for day in days:
            xml = fetch(f"https://nna-leb.gov.lb/ar/sitemap/n/{category}?date={day}")
            for story_url, story_time in entry.findall(xml):
                m = re.search(r"/news/(\d+)", story_url)
                if m:
                    stories[int(m.group(1))] = (story_url, unix_time(story_time))
    return stories


def clean_text(text: str) -> str:
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", html.unescape(text))  # invisible characters
    text = re.sub(r"\.{3,}", ".", text)
    return re.sub(r"\s+", " ", text).strip()


def trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # end at the last sentence boundary if there is a reasonable one
    pos = max(cut.rfind("."), cut.rfind("،"), cut.rfind("؟"), cut.rfind("!"))
    return cut[: pos + 1] if pos > limit * 0.5 else cut.rsplit(" ", 1)[0] + "."


def meta_text(soup: BeautifulSoup, name: str) -> str:
    meta = soup.find("meta", property=name)
    return clean_text(meta["content"]) if meta and meta.get("content") else ""


def read_nna(page: str, max_chars: int) -> tuple[str, str]:
    """NNA pages only hold a summary cut off at about 100 characters; the full text is
    loaded from its API. The summary is read only when it is a complete sentence."""
    soup = BeautifulSoup(page, "html.parser")
    if not soup.find("meta", property="article:published_time"):
        return "", ""  # a missing story shows the homepage instead of an error
    summary = re.sub(r"^وطنية\s*-\s*", "", meta_text(soup, "og:description"))
    return meta_text(soup, "og:title"), trim(summary, max_chars) if summary.endswith((".", "؟", "!")) else ""


def read_elnashra(page: str, max_chars: int) -> tuple[str, str]:
    soup = BeautifulSoup(page, "html.parser")
    title = meta_text(soup, "og:title")
    content = soup.select_one(".articleBody")
    if not content:
        return title, ""
    for junk in content.find_all(["script", "style", "div"]):  # adverts sit in divs
        junk.decompose()
    for br in content.find_all("br"):
        br.replace_with(" ")
    for paragraph in content.find_all("p"):
        paragraph.append(" ")
    # no separator inside a paragraph: links can split a single word into several tags
    return title, trim(clean_text(content.get_text("")), max_chars)


SOURCES = {
    "elnashra": Source("النشرة", elnashra_stories, read_elnashra),
    "nna": Source("الوكالة الوطنية للإعلام", nna_stories, read_nna),
}


def open_stories(source: Source, urls: dict[int, str], max_chars: int) -> tuple[list[NewsItem], set[int]]:
    """Read each article. Returns the stories, and the ids of deleted ones to stop trying."""
    items, gone = [], set()
    for story_id, url in sorted(urls.items()):
        try:
            title, body = source.read(fetch(url), max_chars)
        except requests.RequestException as e:
            if getattr(e.response, "status_code", None) == 404:
                gone.add(story_id)
            # any other failure is tried again on the next run
            log(f"  could not open story {story_id}: {e}")
            continue
        if title:
            items.append(NewsItem(id=story_id, url=url, title=title, source=source.name, body=body))
        else:
            log(f"  story {story_id} has no headline, skipped")
            gone.add(story_id)
    return items, gone


def collect(source: Source, sent: set[int] | None, cfg: dict, minutes: int | None) -> tuple[list[NewsItem], set[int]]:
    """Return the new stories of one source, and the ids to remember once they are sent."""
    stories = source.find()
    window = minutes or (cfg["minutes"] if sent is None else None)
    if window:
        cutoff = time.time() - window * 60
        wanted = {i: url for i, (url, when) in stories.items() if when is not None and when >= cutoff}
    else:
        wanted = {i: url for i, (url, _) in stories.items() if i not in sent}
    if sent is None:  # everything older than the first bulletin counts as already sent
        sent = set(stories) - set(wanted)
    if cfg["max_items"] and len(wanted) > cfg["max_items"]:
        # oldest first; the rest stay unsent and go in the next bulletin
        wanted = dict(sorted(wanted.items())[: cfg["max_items"]])
    log(f"{source.name}: {len(wanted)} new stories")
    items, gone = open_stories(source, wanted, cfg["max_chars_per_item"])
    return items, sent | gone | {it.id for it in items}


# ---------------------------------------------------------------- audio

def ordinal(n: int) -> str:
    return f"الخبر {ORDINALS[n - 1]}" if n <= len(ORDINALS) else f"الخبر رقم {n}"


def item_script(n: int, item: NewsItem) -> str:
    title = re.sub(r"^\s*عاجل\s*[:\-|]?\s*|\s*[-|]?\s*عاجل\s*$", "", item.title).strip()
    return " ".join(p for p in [f"{ordinal(n)}.", f"من {item.source}.", title + ".", item.body] if p)


async def tts(text: str, voice: str, path: Path) -> None:
    await edge_tts.Communicate(text, voice).save(str(path))


async def tts_all(texts: list[str], voice: str, workdir: Path) -> list[Path]:
    sem = asyncio.Semaphore(4)
    paths = [workdir / f"seg_{i:04d}.mp3" for i in range(len(texts))]

    async def one(text, path):
        async with sem:
            for attempt in range(4):
                try:
                    return await tts(text, voice, path)
                except Exception as e:  # network hiccups from the TTS service
                    log(f"  TTS retry ({type(e).__name__})")
                    await asyncio.sleep(2 * (attempt + 1))
            # one bad story must not lose the whole bulletin; its number is simply skipped
            log(f"  SKIPPED, could not generate speech: {text[:80]}")
            path.unlink(missing_ok=True)

    await asyncio.gather(*(one(t, p) for t, p in zip(texts, paths)))
    return [p for p in paths if p.exists()]


def ffmpeg(*args: str) -> None:
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *args], check=True)


def make_chime(path: Path) -> None:
    # two soft rising beeps followed by a short silence
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=0.18",
           "-f", "lavfi", "-i", "sine=frequency=1320:duration=0.25",
           "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono:d=0.35",
           "-filter_complex",
           "[0]volume=0.25,afade=t=out:st=0.12:d=0.06,aresample=24000[a];"
           "[1]volume=0.25,afade=t=out:st=0.15:d=0.1,aresample=24000[b];"
           "[a][b][2]concat=n=3:v=0:a=1",
           "-ac", "1", "-ar", "24000", str(path))


def split_parts(segs: list[Path], chime: Path) -> list[list[Path]]:
    """Group segments into files that each fit Telegram's size limit."""
    parts, current, size = [], [], 0
    for seg in segs:
        seg_size = seg.stat().st_size + chime.stat().st_size
        if current and size + seg_size > TELEGRAM_MAX_BYTES:
            parts.append(current)
            current, size = [], 0
        current += [chime, seg] if current else [seg]
        size += seg_size
    parts.append(current)
    return parts


def build_mp3(items: list[NewsItem], cfg: dict, out_path: Path) -> list[Path]:
    intro = f"نشرة الأخبار، الساعة {datetime.now():%H:%M}. عدد الأخبار {len(items)}."
    texts = [intro] + [item_script(n, it) for n, it in enumerate(items, 1)] + ["انتهت النشرة. شكراً للاستماع."]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log(f"generating speech for {len(texts)} segments...")
        segs = asyncio.run(tts_all(texts, cfg["voice"], tmp))
        chime = tmp / "chime.mp3"
        make_chime(chime)

        parts = split_parts(segs, chime)
        outputs = []
        for n, sequence in enumerate(parts, 1):
            target = out_path if len(parts) == 1 else out_path.with_stem(f"{out_path.stem}_part{n}")
            # a list file rather than arguments: hundreds of inputs overflow the command line
            listing = tmp / f"part{n}.txt"
            listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in sequence), encoding="utf-8")
            ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing),
                   "-codec:a", "libmp3lame", "-b:a", "48k", str(target))
            outputs.append(target)
    return outputs


# ---------------------------------------------------------------- telegram

def telegram(cfg: dict, method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/{method}"
    r = requests.post(url, timeout=900, **kwargs)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data


def send_to_telegram(cfg: dict, mp3s: list[Path], items: list[NewsItem]) -> None:
    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
        raise RuntimeError("telegram_bot_token / telegram_chat_id missing (config.json or environment)")
    if not mp3s:
        telegram(cfg, "sendMessage", data={"chat_id": cfg["telegram_chat_id"],
                                           "text": "لا توجد أخبار جديدة."})
        return
    lines = [f"{n}. {it.title}" for n, it in enumerate(items, 1)]
    header = f"📰 نشرة الأخبار ({datetime.now():%H:%M}) – {len(items)} خبر"
    for n, mp3 in enumerate(mp3s, 1):
        label = f"الجزء {n} من {len(mp3s)}" if len(mp3s) > 1 else ""
        caption = f"{header}\n{label}".strip()
        if n == 1:
            caption += "\n\n" + "\n".join(lines)
        if len(caption) > 1024:
            caption = caption[:1020] + "…"
        with mp3.open("rb") as f:
            telegram(cfg, "sendAudio",
                     data={"chat_id": cfg["telegram_chat_id"], "caption": caption,
                           "title": f"نشرة الأخبار {datetime.now():%Y-%m-%d %H:%M} {label}".strip(),
                           "performer": "News bulletin"},
                     files={"audio": (mp3.name, f, "audio/mpeg")})


# ---------------------------------------------------------------- main

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, help="use this time window instead of 'stories not sent yet'")
    ap.add_argument("--no-send", action="store_true", help="build the MP3 but do not send it")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()
    items, failed = [], []
    for key, source in SOURCES.items():
        # one site being down must not stop the other one's stories
        try:
            found, remember = collect(source, state.get(key), cfg, args.minutes)
        except (requests.RequestException, RuntimeError) as e:
            log(f"{source.name}: FAILED, will try again next run: {e}")
            failed.append(key)
            continue
        items += found
        state[key] = remember
    for n, it in enumerate(items, 1):
        log(f"  {n:2d}. [{it.source}] {it.title}")

    mp3s = []
    if items:
        out_dir = BASE_DIR / cfg["output_dir"]
        out_dir.mkdir(exist_ok=True)
        mp3s = build_mp3(items, cfg, out_dir / f"bulletin_{datetime.now():%Y%m%d_%H%M}.mp3")
        log(f"MP3 saved: {', '.join(str(m) for m in mp3s)}")

    if args.no_send:
        return 1 if failed else 0
    if mp3s or cfg["send_when_empty"]:
        send_to_telegram(cfg, mp3s, items)
        log("sent to Telegram")

    # only after a successful send, so a failed run is retried by the next one
    save_state(state)
    return 1 if failed else 0  # a failed run shows up in red on GitHub's Actions tab


if __name__ == "__main__":
    sys.exit(main())
