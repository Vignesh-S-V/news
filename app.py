import os
import time
import threading
from email.utils import parsedate_to_datetime
from datetime import datetime as _dt_module
from html.parser import HTMLParser
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote

app = Flask(__name__)
CORS(app)

NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
NEWSDATA_URL = "https://newsdata.io/api/1/latest"
NEWSDATA_ARCHIVE_URL = "https://newsdata.io/api/1/archive"

MAX_PAGES = 5


def _fetch_pages(url, base_params):
    all_results = []
    next_page = None
    last_error = None

    for _ in range(MAX_PAGES):
        params = dict(base_params)
        if next_page:
            params["page"] = next_page

        try:
            r = requests.get(url, params=params, timeout=12)
            payload = r.json()
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            break
        except ValueError:
            last_error = "Invalid response from news provider."
            break

        if r.status_code != 200:
            msg = None
            if isinstance(payload, dict):
                results_obj = payload.get("results")
                if isinstance(results_obj, dict):
                    msg = results_obj.get("message")
                msg = msg or payload.get("message")
            last_error = msg or f"News provider returned status {r.status_code}"
            break

        results = payload.get("results") or []
        all_results.extend(results)

        next_page = payload.get("nextPage")
        if not next_page:
            break

    return all_results, last_error


def _pub_date_sort_key(item):
    """எடுக்கக்கூடிய அளவுக்கு அசல் publish நேரத்தை sort key ஆக மாற்றும் (parse தோல்வியுற்றால் 0)."""
    raw = item.get("pubDate", "")
    if not raw:
        return 0
    try:
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
        pass
    from datetime import datetime as _dt_fallback, timezone as _tz_fallback
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return _dt_fallback.strptime(raw[:19], fmt).replace(tzinfo=_tz_fallback.utc).timestamp()
        except Exception:
            continue
    return 0


def fetch_google_news_rss(query, language="en"):
    """கொடுக்கப்பட்ட query-க்கு Google News RSS மூலமாக நேரடியாக செய்திகளை ஸ்கிராப் செய்து தரும்."""
    encoded_query = quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl={language}-IN&gl=IN&ceid=IN:{language}"

    try:
        resp = requests.get(rss_url, timeout=10)
        if resp.status_code != 200:
            return []

        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall('.//item'):
            title = item.find('title')
            pub_date = item.find('pubDate')
            source = item.find('source')
            link = item.find('link')

            items.append({
                "title": title.text if title is not None else "No Title",
                "description": title.text if title is not None else "",
                "pubDate": pub_date.text if pub_date is not None else "",
                "source_id": source.text if source is not None else "Google News",
                "link": link.text if link is not None else ""
            })
        return items
    except Exception as e:
        print("RSS Scraping Error:", str(e))
        return []


# ---------------------------------------------------------------------------
# Wayback Machine (archive.org) fallback for old dates that our live
# web-scraping cache cannot cover. Best-effort: real historical headlines
# extracted from an archived snapshot closest to the requested date — never
# invented content, but coverage/accuracy depends on what archive.org has.
# ---------------------------------------------------------------------------

WAYBACK_AVAILABLE_URL = "https://archive.org/wayback/available"

WAYBACK_SOURCE_PAGES = {
    "top": "https://timesofindia.indiatimes.com",
    "politics": "https://timesofindia.indiatimes.com/india",
    "business": "https://economictimes.indiatimes.com",
    "sports": "https://timesofindia.indiatimes.com/sports",
    "technology": "https://timesofindia.indiatimes.com/technology",
    "environment": "https://timesofindia.indiatimes.com/environment",
    "world": "https://timesofindia.indiatimes.com/world",
}


class _HeadlineLinkExtractor(HTMLParser):
    """ஒரு archived HTML page-ல் இருந்து headline-மாதிரி <a> links-ஐ heuristic-ஆக
    extract செய்யும் (real text-ஐ மட்டும் எடுக்கும், எதுவும் உருவாக்காது)."""

    def __init__(self):
        super().__init__()
        self.items = []
        self._current_href = None
        self._current_text = []
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._in_a = True
            self._current_href = dict(attrs).get("href", "")
            self._current_text = []

    def handle_data(self, data):
        if self._in_a:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            text = " ".join(self._current_text).strip()
            text = " ".join(text.split())
            if 35 <= len(text) <= 180 and self._current_href:
                self.items.append((text, self._current_href))
            self._in_a = False
            self._current_href = None
            self._current_text = []


def _wayback_nearest_snapshot(url, target_date):
    """archive.org-ன் 'available' API மூலமாக, கொடுக்கப்பட்ட தேதிக்கு நெருக்கமான
    snapshot URL-ஐயும் அதன் actual தேதியையும் திருப்பும். கிடைக்கவில்லை என்றால் (None, None)."""
    timestamp = target_date.strftime("%Y%m%d")
    try:
        resp = requests.get(
            WAYBACK_AVAILABLE_URL,
            params={"url": url, "timestamp": timestamp},
            timeout=12
        )
        data = resp.json()
        closest = (data.get("archived_snapshots") or {}).get("closest")
        if not closest or not closest.get("available"):
            return None, None
        snap_url = closest.get("url")
        snap_ts = closest.get("timestamp")
        snap_date = _dt_module.strptime(snap_ts[:8], "%Y%m%d").date() if snap_ts else None
        return snap_url, snap_date
    except Exception as e:
        print("[wayback] availability error:", str(e))
        return None, None


def fetch_wayback_headlines(source_url, target_date, max_items=25):
    """ஒரு target date-க்கு நெருக்கமான archived snapshot-ஐ எடுத்து, அதிலிருந்த
    real headline links-ஐ extract செய்து news-item dicts ஆகத் திருப்பும்."""
    snap_url, snap_date = _wayback_nearest_snapshot(source_url, target_date)
    if not snap_url:
        return [], None

    try:
        resp = requests.get(snap_url, timeout=15)
        if resp.status_code != 200:
            return [], snap_date
        parser = _HeadlineLinkExtractor()
        parser.feed(resp.text)
    except Exception as e:
        print("[wayback] fetch/parse error:", str(e))
        return [], snap_date

    seen_texts = set()
    items = []
    pub_date_str = snap_date.strftime("%Y-%m-%d") if snap_date else ""
    for text, href in parser.items:
        if text in seen_texts:
            continue
        seen_texts.add(text)
        link = href if href.startswith("http") else source_url.rstrip("/") + "/" + href.lstrip("/")
        items.append({
            "title": text,
            "description": text,
            "pubDate": pub_date_str,
            "source_id": "Wayback Archive (archive.org)",
            "link": link,
        })
        if len(items) >= max_items:
            break

    return items, snap_date


# ---------------------------------------------------------------------------
# Continuous background web-scraper + request-triggered refresh
# (Render free-tier instances sleep when idle, killing background threads,
#  so we ALSO refresh opportunistically whenever a real request comes in.)
# ---------------------------------------------------------------------------

SCRAPE_CATEGORY_QUERIES = {
    "top": ["India news", "India latest news", "India headlines today"],
    "politics": ["India politics", "India parliament", "India government policy"],
    "business": ["India business economy", "India stock market", "RBI India economy", "India startup funding"],
    "sports": ["India sports", "India cricket", "Indian Premier League", "India Olympics"],
    "technology": ["India technology", "India startup tech", "India AI news", "India smartphone launch"],
    "environment": ["India environment climate", "India pollution", "India monsoon", "India wildlife"],
    "world": ["world news", "world economy", "international politics today"],
}

SCRAPE_STATE_NAMES = [
    "Tamil Nadu", "Kerala", "Karnataka", "Andhra Pradesh",
    "Delhi", "Maharashtra", "Gujarat", "West Bengal",
]

SCRAPE_LANGUAGES = ["en", "ta"]
SCRAPE_INTERVAL_SECONDS = 300  # 5 minutes
STORE_MAX_ITEMS = 6000

NEWS_STORE = []                 # accumulated scraped articles
NEWS_STORE_LOCK = threading.Lock()
SEEN_LINKS = set()

_last_scrape_started_at = 0.0
_scrape_in_progress = False
_scrape_trigger_lock = threading.Lock()


def _scrape_one_cycle():
    new_items = []

    for lang in SCRAPE_LANGUAGES:
        for cat_key, queries in SCRAPE_CATEGORY_QUERIES.items():
            for q in queries:
                items = fetch_google_news_rss(q, lang)
                for it in items:
                    it["category"] = cat_key
                    it["language"] = lang
                new_items.extend(items)

        for st in SCRAPE_STATE_NAMES:
            items = fetch_google_news_rss(st, lang)
            for it in items:
                it["category"] = "top"
                it["language"] = lang
            new_items.extend(items)

    with NEWS_STORE_LOCK:
        for it in new_items:
            link = it.get("link") or it.get("title")
            if not link or link in SEEN_LINKS:
                continue
            SEEN_LINKS.add(link)
            NEWS_STORE.append(it)

        NEWS_STORE.sort(key=_pub_date_sort_key, reverse=True)

        if len(NEWS_STORE) > STORE_MAX_ITEMS:
            removed = NEWS_STORE[STORE_MAX_ITEMS:]
            del NEWS_STORE[STORE_MAX_ITEMS:]
            for r in removed:
                SEEN_LINKS.discard(r.get("link") or r.get("title"))

    print(f"[scraper] cycle complete. store size = {len(NEWS_STORE)}")


def _run_scrape_cycle_guarded():
    global _scrape_in_progress
    try:
        _scrape_one_cycle()
    except Exception as e:
        print("[scraper] error:", str(e))
    finally:
        with _scrape_trigger_lock:
            _scrape_in_progress = False


def _maybe_trigger_refresh():
    """Request வரும்போது cache பழசா இருந்தா (5 நிமிடத்துக்கு மேல்), ஒரு background
    refresh-ஐ non-blocking-ஆ ஆரம்பிக்கும். இது Render free-tier sleep காரணமாக
    background thread நின்றுபோனாலும், traffic வரும்போதே cache புதுப்பிக்க உதவும்."""
    global _last_scrape_started_at, _scrape_in_progress

    now = time.time()
    with _scrape_trigger_lock:
        if _scrape_in_progress:
            return
        if now - _last_scrape_started_at < SCRAPE_INTERVAL_SECONDS and NEWS_STORE:
            return
        _scrape_in_progress = True
        _last_scrape_started_at = now

    threading.Thread(target=_run_scrape_cycle_guarded, daemon=True).start()


def _scrape_loop():
    """Server தூங்காம continuous-ஆ ஓடினா (paid plan / uptime-pinger வச்சிருந்தா),
    இந்த loop-உம் regular-ஆ refresh பண்ணிக்கொண்டே இருக்கும்."""
    while True:
        _maybe_trigger_refresh()
        time.sleep(SCRAPE_INTERVAL_SECONDS)


_scraper_thread = threading.Thread(target=_scrape_loop, daemon=True)
_scraper_thread.start()


@app.route("/api/scrape-now")
def scrape_now():
    """Keep-alive ping (cron-job.org / GitHub Actions) இதை call பண்ணும்.
    இது FIRE-AND-FORGET-ஆ இயங்கும் — உடனே response திருப்பிடும் (cron-job.org-ன்
    30 வினாடி free-plan timeout-ஐ ஒருபோதும் தொடாது). Scraping background thread-ல்
    தொடரும் — Render container sleep ஆகாம இருக்க இதை frequent-ஆ (1 நிமிடத்துக்கு
    ஒருமுறை) ping பண்ணுங்க, அப்போ background thread-க்கு முழு cycle முடிக்க
    போதுமான நேரம் கிடைக்கும்."""
    _maybe_trigger_refresh()
    return jsonify({
        "status": "refresh triggered (or already running / still fresh)",
        "store_size": len(NEWS_STORE)
    })


@app.route("/api/news")
def get_news():
    category = request.args.get("category")
    state = request.args.get("state")
    query = request.args.get("query")
    language = request.args.get("language", "en")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    # ---- Date-range search: filter from OUR OWN scraped cache by actual pubDate ----
    # (NewsData.io's Archive endpoint needs a paid plan, and a plain Google News
    #  scrape ignores the date entirely — so we filter real pubDate values instead.)
    if from_date or to_date:
        try:
            fy, fm, fd = (int(x) for x in from_date.split("-")) if from_date else (None, None, None)
        except Exception:
            fy = fm = fd = None
        try:
            ty, tm, td = (int(x) for x in to_date.split("-")) if to_date else (None, None, None)
        except Exception:
            ty = tm = td = None

        from datetime import date as _date, timezone as _tz, datetime as _dt

        from_bound = _date(fy, fm, fd) if fy else None
        to_bound = _date(ty, tm, td) if ty else None

        with NEWS_STORE_LOCK:
            pool = list(NEWS_STORE)

        def in_date_range(item):
            ts = _pub_date_sort_key(item)
            if not ts:
                return False
            item_date = _dt.fromtimestamp(ts, tz=_tz.utc).date()
            if from_bound and item_date < from_bound:
                return False
            if to_bound and item_date > to_bound:
                return False
            return True

        def matches_all(item):
            if item.get("language") != language:
                return False
            if category and category != "top" and item.get("category") != category:
                return False
            if state:
                haystack = (item.get("title", "") + " " + item.get("description", "")).lower()
                if state.lower() not in haystack:
                    return False
            if query:
                haystack = (item.get("title", "") + " " + item.get("description", "")).lower()
                if query.lower() not in haystack:
                    return False
            return in_date_range(item)

        filtered = [it for it in pool if matches_all(it)]
        filtered.sort(key=_pub_date_sort_key, reverse=True)

        if filtered:
            return jsonify({
                "results": filtered,
                "notice": f"{len(filtered)} News"
            })

        # Cache has nothing for that period — try NewsData Archive as a last resort
        # (works only if your NewsData.io plan includes archive access).
        base_params = {"apikey": NEWS_API_KEY, "language": language}
        if category and category != "top":
            base_params["category"] = category
        if state:
            base_params["country"] = "in"
            base_params["q"] = f"{query} {state}" if query else state
        elif query:
            base_params["q"] = query
        base_params["from_date"] = from_date
        base_params["to_date"] = to_date

        archive_results, archive_error = _fetch_pages(NEWSDATA_ARCHIVE_URL, base_params)

        # NewsData.io's Archive endpoint can silently ignore from_date/to_date on
        # some plans and just return generic recent results — so we double-check
        # every item's own pubDate before trusting it.
        archive_in_range = [it for it in archive_results if in_date_range(it)]

        if archive_in_range:
            archive_in_range.sort(key=_pub_date_sort_key, reverse=True)
            return jsonify({"results": archive_in_range, "notice": None})

        # Nothing in our cache AND NewsData Archive for this exact period.
        today = _dt.now(tz=_tz.utc).date()
        range_start = from_bound or to_bound
        is_future_request = bool(range_start) and range_start > today

        if is_future_request:
            return jsonify({
                "results": [],
                "notice": "எதிர்கால தேதிகளுக்கான செய்திகள் இன்னும் நடக்கவில்லை — எதுவும் காட்ட முடியாது."
            })

        # Past date, no exact cache/archive match: try a REAL Wayback Machine
        # snapshot of a relevant Indian news homepage close to that date.
        target_date_for_snapshot = from_bound or to_bound or today
        source_url = WAYBACK_SOURCE_PAGES.get(category or "top", WAYBACK_SOURCE_PAGES["top"])
        wayback_items, snap_date = fetch_wayback_headlines(source_url, target_date_for_snapshot)

        if (query or state) and wayback_items:
            needle = (query or state).lower()
            wayback_items = [it for it in wayback_items if needle in it["title"].lower()]

        if wayback_items:
            snap_label = snap_date.strftime("%d-%m-%Y") if snap_date else "தெரியவில்லை"
            return jsonify({
                "results": wayback_items,
                "notice": (
                    ""
                )
            })

        # Past date with no exact match: never fabricate news for a date we don't
        # have — instead, show the closest REAL cached news we do have (same
        # category/state/query, ignoring the date), clearly labelled as such.
        def matches_no_date(item):
            if item.get("language") != language:
                return False
            if category and category != "top" and item.get("category") != category:
                return False
            if state:
                haystack = (item.get("title", "") + " " + item.get("description", "")).lower()
                if state.lower() not in haystack:
                    return False
            if query:
                haystack = (item.get("title", "") + " " + item.get("description", "")).lower()
                if query.lower() not in haystack:
                    return False
            return True

        nearest = [it for it in pool if matches_no_date(it)]
        nearest.sort(key=_pub_date_sort_key, reverse=True)
        nearest = nearest[:30]

        if nearest:
            return jsonify({
                "results": nearest,
                "notice": (
                    ""
                )
            })

        return jsonify({
            "results": [],
            "notice": ""
        })

    # Every normal browsing/search request also nudges the cache to refresh
    # if it's stale — this is what keeps news current even on Render free tier.
    _maybe_trigger_refresh()

    # ---- Free-text search: scrape live for exactly this query ----
    if query:
        search_term = f"{query} {state}" if state else query
        live_results = fetch_google_news_rss(search_term, language)
        live_results.sort(key=_pub_date_sort_key, reverse=True)
        return jsonify({
            "results": live_results,
            "notice": ""
        })

    # ---- Category / state browsing: serve from the continuously-updated cache ----
    with NEWS_STORE_LOCK:
        pool = list(NEWS_STORE)

    def matches(item):
        if item.get("language") != language:
            return False
        if category and category != "top" and item.get("category") != category:
            return False
        if state:
            haystack = (item.get("title", "") + " " + item.get("description", "")).lower()
            if state.lower() not in haystack:
                return False
        return True

    filtered = [it for it in pool if matches(it)]

    if not filtered:
        search_term = state or (category if category and category != "top" else "India news")
        filtered = fetch_google_news_rss(search_term, language)
        filtered.sort(key=_pub_date_sort_key, reverse=True)
        notice = ""
    else:
        notice = f"{len(filtered)} News"

    return jsonify({"results": filtered, "notice": notice})


@app.route("/api/analyze", methods=["POST"])
def analyze_news():
    if not GEMINI_API_KEY:
        return jsonify({"error": "Render Environment-ல் GEMINI_API_KEY சேர்க்கப்படவில்லை."}), 500

    data = request.get_json(force=True)
    headline = data.get("title", "")
    desc = data.get("desc", "")
    date = data.get("date", "")
    focus = data.get("focus")

    extra_key = ""
    if focus == "india_economic_impact":
        extra_key = (
            "5. 'india_effect': 3-4 sentences in Tamil specifically explaining the direct or indirect "
            "COST/ECONOMIC impact of this event on India - e.g. fuel/import prices, exports, rupee value, "
            "interest rates, trade, inflation, stock market. Base this ONLY on real, verifiable connections; "
            "if there is genuinely no meaningful impact on India, say that clearly instead of inventing one.\n"
        )

    system_prompt = (
        "You are an expert investigative journalist and economist writing in rich, clear Tamil. "
        "Analyze the provided REAL news item and respond ONLY with a valid raw JSON object (no markdown, no ```json). "
        "Do not invent facts, numbers, or events that are not reasonably supported by the headline/details given. "
        "Strictly use these keys:\n"
        "1. 'what_happened': 10-14 detailed sentences in Tamil explaining the event as thoroughly as possible. "
        "Cover ALL of the following, in flowing paragraph form (not a bulleted list): WHO is involved (people, "
        "officials, organizations, companies named specifically); WHAT exactly happened, step by step, in the order "
        "it unfolded; WHERE precisely (city/district, state, or country); WHEN (specific date/time references if "
        "available); WHY it started or what triggered it; WHAT actions, statements, or decisions were taken by each "
        "party involved; any numbers, figures, or statistics mentioned (amounts, counts, percentages); and the "
        "CURRENT status or latest situation as of the given date. Do not summarize briefly - expand on every fact "
        "given in the headline/details with full context and explanation. Be specific and comprehensive, never "
        "generic or vague.\n"
        "2. 'past_history': 4-5 sentences in Tamil detailing SPECIFIC similar incidents that happened previously "
        "in India or worldwide. For EACH past incident you mention, you MUST explicitly state BOTH: "
        "(a) the exact or approximate DATE (day if known, month, and year) - e.g. 'செப்டம்பர் 2019', 'ஜூலை 15, 2021'; "
        "(b) the exact PLACE - city/district AND state if in India (e.g. 'கோயம்புத்தூர், தமிழ்நாடு'), or country if outside India. "
        "If you are not confident of the exact date or place, give your best-known year/state at minimum, and say "
        "'கிட்டத்தட்ட' (approximately) rather than omitting it entirely. Never describe a past incident without "
        "attaching both a date and a place to it.\n"
        "3. 'scientific_reasons': 3-4 sentences in Tamil covering scientific/economic causes, facts, and what experts say.\n"
        "4. 'future_outlook': 2-3 sentences in Tamil on whether this could happen again, future risks, and prevention.\n"
        f"{extra_key}"
    )
    user_prompt = f"Headline: {headline}\nDetails: {desc}\nDate: {date}"

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{system_prompt}\n\n{user_prompt}"}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 8000
        }
    }

    gemini_url = (
        f"https://generativelanguage.googleapis.com"
        f"/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        resp = requests.post(gemini_url, json=payload, headers={"Content-Type": "application/json"}, timeout=60)
        if resp.status_code != 200:
            print("Gemini API Error:", resp.text)
            return jsonify({"error": f"Gemini Error ({resp.status_code}): {resp.text}"}), resp.status_code

        res_json = resp.json()
        raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
        return jsonify({"analysis": raw_text})
    except Exception as e:
        print("Backend Error:", str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
