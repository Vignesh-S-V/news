import os
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


def fetch_google_news_rss(query, language="en"):
    """NewsData.io Archive கிடைக்காதபோது கூகுள் நியூஸ் RSS மூலமாக தரவுகளை ஸ்கிராப் செய்து தரும்."""
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


@app.route("/api/news")
def get_news():
    category = request.args.get("category")
    state = request.args.get("state")
    query = request.args.get("query")
    language = request.args.get("language", "en")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")

    base_params = {
        "apikey": NEWS_API_KEY,
        "language": language,
    }

    if state:
        base_params["country"] = "in"
        if query:
            base_params["q"] = f"{query} {state}"
        else:
            base_params["q"] = state
    else:
        if query:
            base_params["q"] = query
        elif category and category != "top":
            base_params["category"] = category

    if from_date:
        base_params["from_date"] = from_date
    if to_date:
        base_params["to_date"] = to_date

    used_archive = bool(from_date or to_date)
    target_url = NEWSDATA_ARCHIVE_URL if used_archive else NEWSDATA_URL

    all_results, last_error = _fetch_pages(target_url, base_params)

    if not all_results and (used_archive or last_error):
        search_term = query or state or category or "India news"
        if used_archive and from_date:
            search_term += f" {from_date[:4]}"

        scraped_results = fetch_google_news_rss(search_term, language)
        if scraped_results:
            return jsonify({
                "results": scraped_results,
                "notice": ""
            })

    if not all_results and last_error:
        return jsonify({"error": last_error}), 502

    return jsonify({"results": all_results, "notice": None})


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

    # எந்த பிராக்கெட்டும் இல்லாத சுத்தமான மற்றும் பாதுகாப்பான URL (indentation சரி செய்யப்பட்டது)
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
