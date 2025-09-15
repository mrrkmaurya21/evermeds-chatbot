# app.py — FastAPI backend for FAQ bot (keyword + fuzzy), v1.2
# Endpoints:
#   POST /ask            -> {"q": "..."} -> {"answer": "...", "citations": [...]}
#   POST /chat           -> {"message": "..."} -> {"reply": "...", "citations": [...]}
#   GET  /healthz        -> health/status
#   GET  /               -> small info JSON (helps HEAD/GET root checks)
#   GET  /ui             -> serves chat UI file (chatui.html by default)
#   GET  /widget         -> legacy alias -> redirects to /ui
#   POST /admin/reload   -> reload faq.json (protected via X-Admin-Token)
#
# Env (Render → Settings → Environment):
#   ALLOW_ORIGINS="https://yourdomain.com,https://www.yourdomain.com"
#   FRAME_ANCESTORS="https://yourdomain.com https://www.yourdomain.com"
#   UI_FILE="chatui.html"
#   ADMIN_TOKEN="set-a-strong-secret"
#   ANSWER_THRESHOLD="0.60"   # optional
#
# Files:
#   - faq.json (Q/A KB) in the repo root (same folder as app.py)
#   - chatui.html (served by /ui)

import json, re, os
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Dict

from fastapi import FastAPI, Header
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, FileResponse, RedirectResponse

HERE = Path(__file__).parent
KB_PATH = HERE / "faq.json"

ANSWER_THRESHOLD = float(os.getenv("ANSWER_THRESHOLD", "0.60"))
UI_FILE         = os.getenv("UI_FILE", "chatui.html")
FRAME_ANCESTORS = os.getenv("FRAME_ANCESTORS", "")
ADMIN_TOKEN     = os.getenv("ADMIN_TOKEN", "")

# ---------- helpers ----------

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
        "q_norm": norm(q), "a_norm": norm(a),
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
        return [_fix_and_norm_item(it) for it in data]
    except Exception as e:
        print("ERROR loading faq.json:", e)
        return []

KB: List[Dict] = _load_kb()

def score(query: str, item: Dict) -> float:
    qn = norm(query)
    texts = [item["q_norm"], item["a_norm"]] + item["aliases_norm"]
    best = 0.0
    for tn in texts:
        if not tn: 
            continue
        kw   = sum(1 for w in qn.split() if w and w in tn)   # keyword hits
        fuzz = SequenceMatcher(None, qn, tn).ratio()         # fuzzy ratio
        best = max(best, kw + fuzz)
    return best

def retrieve(query: str, top_k: int = 3) -> List[Dict]:
    scored = [(score(query, it), it) for it in KB]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:top_k]]

# ---------- app ----------

app = FastAPI(title="EverMeds FAQ Bot", version="1.2.0")

# CORS
origins_env = os.getenv("ALLOW_ORIGINS", "*")
allow_origins = ["*"] if origins_env.strip() in ["*", ""] else [o.strip() for o in origins_env.split(",")]

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

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "EverMeds FAQ Bot", "items": len(KB), "routes": ["/ask", "/ui", "/healthz"]}

@app.get("/healthz")
def healthz():
    return {"ok": True, "items": len(KB), "threshold": ANSWER_THRESHOLD, "allow_origins": allow_origins}

@app.post("/admin/reload")
def admin_reload(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN:
        return JSONResponse({"error": "ADMIN_TOKEN not set on server"}, status_code=403)
    if x_admin_token != ADMIN_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    global KB
    KB = _load_kb()
    return {"reloaded": True, "items": len(KB)}

@app.post("/ask")
def ask(inp: AskIn):
    q = (inp.q or "").strip()
    if not q:
        return {"answer": "Please ask a question.", "citations": []}
    hits = retrieve(q, top_k=3)
    top  = hits[0] if hits else None
    if (not top) or (score(q, top) < ANSWER_THRESHOLD):
        return {"answer": "Sorry—this topic isn’t on our site yet. Please check our Contact/Support page.", "citations": ["/contact"]}
    cites = []
    for h in hits[:3]:
        u = h.get("url") or "/"
        if u not in cites: 
            cites.append(u)
    return {"answer": top.get("a", ""), "citations": cites}

@app.post("/chat")
def chat(inp: ChatIn):
    result = ask(AskIn(q=inp.message))
    return {"reply": result["answer"], "citations": result.get("citations", [])}

@app.get("/ui", include_in_schema=False)
def ui():
    path = HERE / UI_FILE
    if path.exists():
        resp = FileResponse(str(path), media_type="text/html")
        if FRAME_ANCESTORS:
            resp.headers["Content-Security-Policy"] = f"frame-ancestors 'self' {FRAME_ANCESTORS}"
        return resp
    return JSONResponse({"error": f"{UI_FILE} not found"}, status_code=404)

@app.get("/widget", include_in_schema=False)
def widget():
    return RedirectResponse(url="/ui", status_code=307)

# local dev runner
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=True)
