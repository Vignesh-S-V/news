import os
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
NEWSDATA_URL = "https://newsdata.io/api/1/news"

# newsdata.io free plan gives 10 articles per page.
# We auto-paginate up to MAX_PAGES so the frontend gets more than just 10.
MAX_PAGES = 5


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
        "country": "in",
        "language": language,
    }

    if query:
        # Search overrides category/state - full related news for that keyword
        base_params["q"] = query
    else:
        if category and category != "top":
            base_params["category"] = category
        if state:
            base_params["q"] = state

    if from_date:
        base_params["from_date"] = from_date
    if to_date:
        base_params["to_date"] = to_date

    all_results = []
    next_page = None
    last_error = None

    for _ in range(MAX_PAGES):
        params = dict(base_params)
        if next_page:
            params["page"] = next_page

        try:
            r = requests.get(NEWSDATA_URL, params=params, timeout=12)
            payload = r.json()
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            break
        except ValueError:
            last_error = "Invalid response from news provider."
            break

        if r.status_code != 200:
            # newsdata.io puts the reason inside payload["results"]["message"] usually
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

    if not all_results and last_error:
        return jsonify({"error": last_error}), 502

    return jsonify(all_results)


@app.route("/api/analyze", methods=["POST"])
def analyze_news():
    if not GEMINI_API_KEY:
        return jsonify({"error": "Render Environment-ல் GEMINI_API_KEY சேர்க்கப்படவில்லை."}), 500

    data = request.get_json(force=True)
    headline = data.get("title", "")
    desc = data.get("desc", "")
    date = data.get("date", "")

    system_prompt = (
        "You are an expert investigative journalist and scientist writing in rich, clear Tamil. "
        "Analyze the provided news item and respond ONLY with a valid raw JSON object (no markdown, no ```json). "
        "Strictly use these 4 keys:\n"
        "1. 'what_happened': 3-4 sentences in Tamil explaining the event in detail.\n"
        "2. 'past_history': 3-4 sentences in Tamil detailing where and when similar incidents have happened previously in India or worldwide.\n"
        "3. 'scientific_reasons': 3-4 sentences in Tamil covering scientific causes, facts, and what scientists/experts say.\n"
        "4. 'future_outlook': 2-3 sentences in Tamil on whether this could happen again, future risks, and prevention."
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
