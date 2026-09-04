import os
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)  # GitHub Pages-ல் இருந்து வரும் Request-ஐ அனுமதிக்கிறது

# API Key-களை Environment Variables-ல் இருந்து எடுக்கிறோம் (பாதுகாப்பானது)
# உண்மையான Key-ஐ இங்கே பேஸ்ட் செய்யக்கூடாது
NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

NEWSDATA_URL = "https://newsdata.io/api/1/news"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={GEMINI_API_KEY}"

@app.route("/api/news")
def get_news():
    category = request.args.get("category")
    state = request.args.get("state")
    language = request.args.get("language", "en")

    params = {
        "apikey": NEWS_API_KEY,
        "country": "in",
        "language": language,
    }
    if category and category != "top":
        params["category"] = category
    if state:
        params["q"] = state

    try:
        r = requests.get(NEWSDATA_URL, params=params, timeout=10)
        r.raise_for_status()
        return jsonify(r.json().get("results", []))
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/analyze", methods=["POST"])
def analyze_news():
    data = request.json
    headline = data.get("title", "")
    desc = data.get("desc", "")
    date = data.get("date", "")

    system_prompt = """You are a careful Indian news analyst writing for a Tamil-reading audience.
    Given a news headline and short description, respond with ONLY a valid JSON object. Do not include markdown formatting or backticks around the JSON. The JSON must exactly match this structure:
    {
      "what_happened": "2-4 sentences in Tamil describing what happened",
      "reason_science": "2-4 sentences in Tamil explaining the causes or background",
      "expert_view": "2-3 sentences in Tamil summarizing expert views",
      "future_outlook": "2-3 sentences in Tamil on future implications"
    }"""
    
    user_prompt = f"Headline: {headline}\nDescription: {desc}\nDate: {date}"

    payload = {
        "contents": [{"parts": [{"text": system_prompt + "\n\n" + user_prompt}]}]
    }

    try:
        r = requests.post(GEMINI_URL, json=payload, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        gemini_response = r.json()
        text_result = gemini_response["candidates"][0]["content"]["parts"][0]["text"]
        return jsonify({"analysis": text_result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)
