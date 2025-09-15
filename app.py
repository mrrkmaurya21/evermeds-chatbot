# app.py — FastAPI backend for FAQ bot (keyword + fuzzy), hardened v1.1
# Endpoints:
#   POST /ask            -> {"q": "..."} -> {"answer": "...", "citations": [...]}
#   POST /chat           -> {"message": "..."} -> {"reply": "...", "citations": [...]}
#   GET  /healthz        -> health/status
#   POST /admin/reload   -> reload faq.json (protected via X-Admin-Token)
#   GET  /ui             -> serves chat UI file (chatui.html by default)
#
# Env you can set on Render:
#   ALLOW_ORIGINS="https://yourdomain.com,https://www.yourdomain.com"
#   ANSWER_THRESHOLD="0.60"
#   UI_FILE="chatui.html"
#   FRAME_ANCESTORS="https://yourdomain.com https://www.yourdomain.com"
#   ADMIN_TOKEN="set-a-strong-secret"
#
# Notes:
# - Keep faq.json in repo root (same folder as app.py).
# - If you change file name, update Procfile accordingly: app:app

import json, re, os
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Dict

from fastapi import FastAPI, Body, Header
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, FileResponse, RedirectResponse


HERE = Path(__file__).parent
KB_PATH = HERE / "faq.json"

ANSWER_THRESHOLD = float(os.getenv("ANSWER_THRESHOLD", "0.60"))
UI_FILE = os.getenv("UI_FILE", "chatui.html")
FRAME_ANCESTORS = os.getenv("FRAME_ANCESTORS", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # REQUIRED to protect /admin/reload in prod

# -------------------- Utils --------------------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _fix_and_norm_item(it: Dict) -> Dict:
    q = (it.get("q") or "").strip()
    a = (it.get("a") or "").strip()
    url = (it.get("url") or "/").strip() or "/"
    aliases = it.get("aliases") or []
    if not isinstance(aliases, list):
        aliases = []

    return {
        "q": q, "a": a, "url": url, "aliases": aliases,
        # precomputed normalized fields for speed
        "q_norm": norm(q),
        "a_norm": norm(a),
        "aliases_norm": [norm(x) for x in aliases]
    }

def _load_kb() -> List[Dict]:
    if not KB_PATH.exists():
        print("WARNING: faq.json not found — creating empty KB.")
        return []
    raw = KB_PATH.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("faq.json must be a JSON array")
        fixed = [_fix_and_norm_item(it) for it in data]
        return fixed
    except Exception as e:
        print("ERROR loading faq.json:", e)
        return []

# in-memory KB (list of dicts with precomputed norms)
KB: List[Dict] = _load_kb()

def score(query: str, item: Dict) -> float:
    """keyword hits + fuzzy similarity using precomputed normalized fields."""
    qn = norm(query)
    # candidate texts: q, a, aliases
    texts = [item["q_norm"], item["a_norm"]] + item["aliases_norm"]
    best = 0.0
    for tn in texts:
        if not tn:
            continue
        kw = sum(1 for w in qn.split() if w and w in tn)   # keyword containment
        fuzz = SequenceMatcher(None, qn, tn).ratio()       # fuzzy
        best = max(best, kw + fuzz)
    return best

def retrieve(query: str, top_k: int = 3) -> List[Dict]:
    scored = [(score(query, it), it) for it in KB]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:top_k]]

# -------------------- FastAPI app --------------------

app = FastAPI(title="EverMeds FAQ Bot", version="1.1.0")

# CORS
origins_env = os.getenv("ALLOW_ORIGINS", "*")
if origins_env.strip():
    allow_origins = [o.strip() for o in origins_env.split(",")] if origins_env != "*" else ["*"]
else:
    allow_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AskIn(BaseModel):
    q: str

class ChatIn(BaseModel):
    message: str

@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "items": len(KB),
        "threshold": ANSWER_THRESHOLD,
        "allow_origins": allow_origins
    }

# ----- Admin: reload KB (protected) -----
@app.post("/admin/reload")
def admin_reload(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN:
        # If you forget to set ADMIN_TOKEN, keep it no-op protected
        return JSONResponse({"error": "ADMIN_TOKEN not set on server"}, status_code=403)
    if x_admin_token != ADMIN_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    global KB
    KB = _load_kb()
    return {"reloaded": True, "items": len(KB)}

# ----- Main Q&A -----
@app.post("/ask")
def ask(inp: AskIn):
    q = (inp.q or "").strip()
    if not q:
        return {"answer": "Please ask a question.", "citations": []}

    hits = retrieve(q, top_k=3)
    top = hits[0] if hits else None
    if (not top) or (score(q, top) < ANSWER_THRESHOLD):
        return {
            "answer": "Sorry—this topic isn’t on our site yet. Please check our Contact/Support page.",
            "citations": ["/contact"]
        }

    # Attach 1–3 citations (dedup)
    cites = []
    for h in hits[:3]:
        u = h.get("url") or "/"
        if u not in cites:
            cites.append(u)

    return {"answer": top.get("a", ""), "citations": cites}

# backwards-compat endpoint if old UI calls /chat with {"message": "..."}
@app.post("/chat")
def chat(inp: ChatIn):
    result = ask(AskIn(q=inp.message))
    return {"reply": result["answer"], "citations": result.get("citations", [])}

# Serve Chat UI directly from this service
@app.get("/ui", include_in_schema=False)
def ui():
    path = HERE / UI_FILE  # e.g., chatui.html
    if path.exists():
        resp = FileResponse(str(path), media_type="text/html")
        # Allow embedding from your site(s)
        if FRAME_ANCESTORS:
            resp.headers["Content-Security-Policy"] = f"frame-ancestors 'self' {FRAME_ANCESTORS}"
        return resp
    return JSONResponse({"error": f"{UI_FILE} not found"}, status_code=404)

# Legacy alias -> redirect to /ui (307 preserves method)
@app.get("/widget", include_in_schema=False)
def widget():
    # legacy path support; redirect to /ui
    return RedirectResponse(url="/ui", status_code=307)


# local dev runner
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=True)

