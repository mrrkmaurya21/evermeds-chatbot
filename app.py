import os
import re
import json
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__)

# --- CORS: allow your site (add more if needed) ---
CORS(app, resources={
    r"/*": {
        "origins": [
            "https://evermeds.in",
            "http://localhost:3000",
            "http://localhost:8080"
        ]
    }
})

# --- OpenAI client with clear guard for missing key ---
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY not set in environment")
client = OpenAI(api_key=OPENAI_KEY, timeout=30)  # default timeout


# ---------- Helpers (FAQ optional, never break) ----------
def load_faq_safe():
    try:
        with open("faq.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def best_faq_snippets(user_text: str, faq_items, top_k: int = 3):
    """
    Very simple keyword overlap scorer (no extra libs).
    Returns top-k FAQ items that share words with the user query.
    """
    q = set(re.findall(r"[a-zA-Z0-9]+", (user_text or "").lower()))
    scored = []
    for item in faq_items or []:
        qa_text = (item.get("question", "") + " " + item.get("answer", "")).lower()
        t = set(re.findall(r"[a-zA-Z0-9]+", qa_text))
        score = len(q & t)
        if score > 0:
            scored.append((score, item))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [it for _, it in scored[:top_k]]


# ---------- Health ----------
@app.get("/health")
def health():
    return {"ok": True}


# ---------- FAQs (reads faq.json from repo root) ----------
@app.get("/faq")
def get_faq():
    try:
        with open("faq.json", "r", encoding="utf-8") as f:
            faq_data = json.load(f)
        return jsonify(faq_data)
    except FileNotFoundError:
        return jsonify({"error": "faq.json not found in repo root"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------- Minimal in-iframe Widget UI ----------
@app.get("/widget")
def widget():
    html = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EverMeds Assistant</title>
<style>
  body{font:16px system-ui;margin:0;background:#fff}
  header{background:#1e90ff;color:#fff;padding:10px 12px;font-weight:700}
  .box{padding:12px}
  .row{display:flex;gap:8px;margin-top:8px}
  textarea{width:100%;height:120px;padding:8px;border:1px solid #ccc;border-radius:8px}
  button{padding:8px 12px;border:0;border-radius:8px;cursor:pointer;background:#1e90ff;color:#fff}
  pre{white-space:pre-wrap;font-family:ui-monospace, SFMono-Regular, Menlo, monospace;background:#f6f8fa;padding:10px;border-radius:8px}
  .faq{margin-top:14px}
  .faq h4{margin:12px 0 6px}
  .faq-item{margin:8px 0}
  .faq-q{font-weight:600}
</style>
<header>EverMeds Assistant</header>
<div class="box">
  <div class="row">
    <textarea id="msg" placeholder="e.g., Which diabetes product is right for me?"></textarea>
  </div>
  <div class="row">
    <button id="send">Send</button>
  </div>
  <h4>Reply</h4>
  <pre id="out">—</pre>

  <div class="faq">
    <h4>Quick FAQs</h4>
    <div id="faqList">Loading FAQs…</div>
  </div>
</div>
<script>
  const out = document.getElementById('out');
  document.getElementById('send').onclick = async () => {
    const text = document.getElementById('msg').value.trim();
    if(!text){ out.textContent = 'Please type something.'; return; }
    if(text.length > 4000){ out.textContent = 'Input too long.'; return; }
    out.textContent = 'Thinking…';
    try{
      const r = await fetch('/api/chat', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({messages:[{role:'user', content:text}]})
      });
      if(!r.ok){ out.textContent = 'Error: ' + r.status; return; }
      const data = await r.json();
      out.textContent = data.reply || '(no reply)';
    }catch(e){
      out.textContent = 'Network error';
    }
  };

  // Load FAQs (best-effort)
  (async function(){
    try{
      const r = await fetch('/faq');
      const box = document.getElementById('faqList');
      if(!r.ok){ box.textContent = 'FAQs unavailable.'; return; }
      const items = await r.json();
      if(!Array.isArray(items)){ box.textContent = 'No FAQs found.'; return; }
      box.innerHTML = items.map(x => (
        '<div class="faq-item"><div class="faq-q">'+
        (x.question ? String(x.question) : '')+
        '</div><div class="faq-a">'+
        (x.answer ? String(x.answer) : '')+
        '</div></div>'
      )).join('');
    }catch(e){
      const box = document.getElementById('faqList');
      box.textContent = 'FAQs unavailable.';
    }
  })();
</script>
"""
    resp = make_response(html)
    # Security / embed headers
    resp.headers["X-Frame-Options"] = "ALLOWALL"  # intentionally embeddable in iframe
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


# ---------- Chat proxy (FAQ-aware, graceful fallback) ----------
@app.post("/api/chat")
def api_chat():
    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "no messages"}), 400

    # Basic input length guard (user text total)
    user_len = sum(len(m.get("content", "")) for m in messages if m.get("role") == "user")
    if user_len > 4000:
        return jsonify({"error": "input too long"}), 413

    # Try to enrich with FAQs, but do NOT fail if missing
    faq_items = load_faq_safe()
    user_text = " ".join([m.get("content", "") for m in messages if m.get("role") == "user"])
    top_faqs = best_faq_snippets(user_text, faq_items, top_k=3) if faq_items else []

    system_ctx = "You are EverMeds assistant. Be concise, safe, and helpful."
    if top_faqs:
        system_ctx += "\nUse these FAQs if relevant:\n" + json.dumps(top_faqs, ensure_ascii=False)

    messages_with_ctx = [{"role": "system", "content": system_ctx}] + messages

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages_with_ctx,
            temperature=0.3
        )
        reply = r.choices[0].message.content
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------- Run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
