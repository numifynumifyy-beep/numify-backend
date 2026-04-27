import asyncio
import json
import os
import re
import time
import phonenumbers
import firebase_admin
from collections import deque
from firebase_admin import credentials, firestore, auth
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
from datetime import datetime, timezone

# ── Firebase init via environment variable (never commit serviceAccountKey.json) ──
_svc = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
if _svc:
    cred = credentials.Certificate(json.loads(_svc))
else:
    # Fallback for local dev only — never use in production
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

app = FastAPI()

# ── CORS: lock to your actual frontend domain ──
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://numifynumifyy-beep.github.io,http://localhost:3000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Concurrency limit: max simultaneous Playwright browsers ──
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_SCRAPERS", "3"))
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

REGION = os.environ.get("PHONE_REGION", "TN")


def extract_numbers(text: str) -> set:
    found = set()
    for match in phonenumbers.PhoneNumberMatcher(text, REGION):
        formatted = phonenumbers.format_number(
            match.number, phonenumbers.PhoneNumberFormat.E164
        )
        found.add(formatted)
    for m in re.findall(r'\b\d{8,12}\b', text):
        found.add(m)
    return found


def verify_token(id_token: str) -> dict:
    try:
        return auth.verify_id_token(id_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def check_subscription(uid: str) -> bool:
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return False
    data = doc.to_dict()
    if data.get("status") != "active":
        return False
    expiry = data.get("expiresAt")
    if expiry and expiry < datetime.now(timezone.utc):
        db.collection("users").document(uid).update({"status": "expired"})
        return False
    return True


@app.get("/")
def root():
    return {"status": "Numify backend is running"}


@app.post("/request-subscription")
async def request_subscription(body: dict, authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "")
    decoded = verify_token(token)
    uid = decoded["uid"]

    plans = {"30days": 100, "90days": 250, "year": 850}
    plan = body.get("plan")
    if plan not in plans:
        raise HTTPException(status_code=400, detail="Invalid plan")

    db.collection("subscription_requests").add({
        "uid": uid,
        "email": decoded.get("email"),
        "plan": plan,
        "price": plans[plan],
        "requestedAt": firestore.SERVER_TIMESTAMP,
        "status": "pending"
    })
    return {"message": "Request submitted"}


@app.websocket("/ws/scrape")
async def scrape_ws(websocket: WebSocket):
    await websocket.accept()
    browser = None

    try:
        init = await websocket.receive_json()
        token = init.get("token")
        live_url = init.get("url", "").strip()

        # Validate token
        decoded = auth.verify_id_token(token)
        uid = decoded["uid"]

        if not check_subscription(uid):
            await websocket.send_json({"type": "error", "message": "No active subscription"})
            return

        # Validate URL
        if not live_url.startswith("https://www.tiktok.com/") or "/live" not in live_url:
            await websocket.send_json({"type": "error", "message": "Invalid TikTok Live URL"})
            return

        # Check concurrency limit
        if _semaphore.locked() and _semaphore._value == 0:
            await websocket.send_json({"type": "error", "message": "Server busy, please try again shortly"})
            return

        async with _semaphore:
            await websocket.send_json({"type": "status", "message": "Launching browser..."})

            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--disable-sync",
                        "--disable-translate",
                        "--hide-scrollbars",
                        "--mute-audio",
                        "--no-first-run",
                        "--safebrowsing-disable-auto-update",
                        "--window-size=1280,720"
                    ]
                )

                try:
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        viewport={"width": 1280, "height": 720}
                    )
                    page = await context.new_page()

                    await websocket.send_json({"type": "status", "message": "Opening TikTok Live..."})

                    try:
                        await page.goto(live_url, timeout=60000, wait_until="domcontentloaded")
                    except Exception:
                        await websocket.send_json({"type": "status", "message": "Page loaded, continuing..."})

                    await asyncio.sleep(10)
                    await websocket.send_json({"type": "status", "message": "Scanning chat for numbers..."})

                    # Use deque with maxlen to cap memory usage
                    seen_comments = deque(maxlen=MAX_SEEN := 1000)
                    seen_comments_set = set()
                    seen_numbers = set()
                    empty_cycles = 0

                    SELECTORS = [
                        "div[data-e2e='chat-message']",
                        "[class*='ChatMessage']",
                        "[class*='chat-message']",
                        "[class*='chatMessage']",
                        "[class*='LiveComment']",
                        "[class*='comment']",
                    ]

                    while True:
                        elements = []
                        for selector in SELECTORS:
                            elements = await page.query_selector_all(selector)
                            if elements:
                                break

                        if not elements:
                            empty_cycles += 1
                            await websocket.send_json({
                                "type": "status",
                                "message": f"Waiting for chat... ({empty_cycles * 2}s)"
                            })
                            await asyncio.sleep(2)
                            continue

                        empty_cycles = 0
                        await websocket.send_json({
                            "type": "status",
                            "message": f"Scanning {len(elements)} chat messages..."
                        })

                        for el in elements:
                            try:
                                text = (await el.inner_text()).strip()
                            except Exception:
                                continue

                            if not text or text in seen_comments_set:
                                continue

                            # Evict oldest if at cap
                            if len(seen_comments) == MAX_SEEN:
                                oldest = seen_comments[0]
                                seen_comments_set.discard(oldest)

                            seen_comments.append(text)
                            seen_comments_set.add(text)

                            for num in extract_numbers(text):
                                if num not in seen_numbers:
                                    seen_numbers.add(num)
                                    await websocket.send_json({
                                        "type": "number",
                                        "number": num,
                                        "comment": text[:120],
                                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                                    })

                        await asyncio.sleep(2)

                finally:
                    # Always close browser to prevent resource leaks
                    if browser:
                        await browser.close()
                        browser = None

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        # Ensure browser is always cleaned up even on unexpected errors
        if browser:
            try:
                await browser.close()
            except Exception:
                pass