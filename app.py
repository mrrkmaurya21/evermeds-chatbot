import os, json, re, requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# -------------------------------------------------
# Load secrets / config from .env (same folder)
# -------------------------------------------------
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a brief, friendly support bot for EverMeds. Be concise. "
    "If asked about medicines/dosage, add: 'This is not medical advice; consult your doctor.'"
)

# Startup sanity logs
print("ENV found:", os.path.isfile(".env"))
print("KEY startswith sk- ?", str(OPENAI_API_KEY or "").startswith("sk-"))
print("MODEL:", MODEL)
if not OPENAI_API_KEY or not OPENAI_API_KEY.startswith("sk-"):
    raise RuntimeError("OPENAI_API_KEY missing/invalid. Put it in .env and restart.")

# -------------------------------------------------
# Flask app
# -------------------------------------------------
app = Flask(__name__)

# CORS for browser (local/dev = '*'; prod me exact origin set karein)
@app.after_request
def add_cors_headers(resp):
    # Example for production: resp.headers["Access-Control-Allow-Origin"] = "https://www.evermeds.in"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp

# -------------------------------------------------
# Load FAQ (faq.json in same folder)
# -------------------------------------------------
FAQ = []
try:
    with open("faq.json", "r", encoding="utf-8") as f:
        FAQ = json.load(f)
    print(f"FAQ loaded: {len(FAQ)} items")
except Exception as e:
    print("FAQ not loaded:", e)

_word = re.compile(r"[a-z0-9]+")

def _norm(text: str):
    return _word.findall(text.lower())

def retrieve_snippets(user_msg: str, k: int = 3):
    """Naive keyword-overlap retrieval from FAQ."""
    if not FAQ:
        return []
    uq = set(_norm(user_msg))
    scored = []
    for item in FAQ:
        text = (item.get("q", "") + " " + item.get("a", ""))
        it = set(_norm(text))
        score = len(uq & it)  # overlap size
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:k]]

# -------------------------------------------------
# OpenAI call
# -------------------------------------------------
def ask_openai(user_text: str) -> str:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text}
            ],
            "temperature": 0.2
        },
        timeout=30
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# -------------------------------------------------
# Routes
# -------------------------------------------------
@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    # CORS preflight
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "empty message"}), 400

    # 1) Try FAQ first (fast path)
    snippets = retrieve_snippets(msg, k=3)
    if snippets:
        top = snippets[0]
        # simple heuristic: if >=3 token overlap, return direct FAQ answer
        if len(set(_norm(msg)) & set(_norm(top.get("q","") + " " + top.get("a","")))) >= 3:
            ans = top.get("a", "")
            if any(w in msg.lower() for w in ["mg","dose","dosage","tablet","medicine","drug"]):
                ans += "\n\n*Note: This is general info, not medical advice. Please consult your doctor.*"
            return jsonify({"reply": ans})

    # 2) Else: build context from top FAQ hits and call OpenAI
    context = ""
    if snippets:
        ctx_lines = [f"- Q: {s.get('q','')} A: {s.get('a','')}" for s in snippets]
        context = "Relevant FAQ snippets:\n" + "\n".join(ctx_lines)

    try:
        prompt = (context + "\n\nUser: " + msg) if context else msg
        ans = ask_openai(prompt)
    except requests.HTTPError as e:
        return jsonify({"error": f"Upstream error: {e.response.text[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if any(w in msg.lower() for w in ["mg","dose","dosage","tablet","medicine","drug"]):
        ans += "\n\n*Note: This is general info, not medical advice. Please consult your doctor.*"

    return jsonify({"reply": ans})

@app.get("/health")
def health():
    return "ok", 200

# -------------------------------------------------
# Main
# -------------------------------------------------
if __name__ == "__main__":
    # Local dev server
    app.run(host="127.0.0.1", port=5000, debug=True)
