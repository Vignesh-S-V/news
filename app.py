import os
import time
import threading
from email.utils import parsedate_to_datetime
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

MAX_PAGES = 10


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
   
    raw = item.get("pubDate", "")
    try:
        return parsedate_to_datetime(raw).timestamp()
    except Exception:
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


@app.route("/api/news")
def get_news():
    category = request.args.get("category")
    state = request.args.get("state")
    query = request.args.get("query")
    language = request.args.get("language", "en")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    # ---- Date-range archive search still goes through NewsData.io ----
    if from_date or to_date:
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

        all_results, last_error = _fetch_pages(NEWSDATA_ARCHIVE_URL, base_params)

        if not all_results and last_error:
            search_term = query or state or category or "India news"
            search_term += f" {from_date[:4]}"
            scraped_results = fetch_google_news_rss(search_term, language)
            if scraped_results:
                return jsonify({
                    "results": scraped_results,
                    "notice": ""
                })
            return jsonify({"error": last_error}), 502

        return jsonify({"results": all_results, "notice": None})

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
        "1. 'what_happened': 3-4 sentences in Tamil explaining the event in detail.\n"
        "2. 'past_history': 3-4 sentences in Tamil detailing where and when similar incidents have happened previously in India or worldwide.\n"
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
            "maxOutputTokens": 1200
        }
    }

    gemini_url = (
        f"https://generativelanguage.googleapis.com"
        f"/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        resp = requests.post(gemini_url, json=payload, headers={"Content-Type": "application/json"}, timeout=20)
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
