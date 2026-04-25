import asyncio
import re
import time
import phonenumbers
import firebase_admin
from firebase_admin import credentials, firestore, auth
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
from datetime import datetime, timezone

cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REGION = "TN"

def extract_numbers(text):
    found = set()
    for match in phonenumbers.PhoneNumberMatcher(text, REGION):
        formatted = phonenumbers.format_number(
            match.number, phonenumbers.PhoneNumberFormat.E164
        )
        found.add(formatted)
    for m in re.findall(r'\b\d{8,12}\b', text):
        found.add(m)
    return found

def verify_token(id_token: str):
    try:
        return auth.verify_id_token(id_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

def check_subscription(uid: str):
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
    try:
        init = await websocket.receive_json()
        token = init.get("token")
        live_url = init.get("url", "").strip()

        decoded = auth.verify_id_token(token)
        uid = decoded["uid"]

        if not check_subscription(uid):
            await websocket.send_json({"type": "error", "message": "No active subscription"})
            return

        await websocket.send_json({"type": "status", "message": "Launching browser..."})

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-zygote",
                    "--single-process"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720}
            )
            page = await context.new_page()

            await websocket.send_json({"type": "status", "message": "Opening TikTok Live..."})

            try:
                await page.goto(live_url, timeout=30000, wait_until="domcontentloaded")
            except Exception:
                await websocket.send_json({"type": "status", "message": "Page loaded, continuing..."})

            await asyncio.sleep(8)
            await websocket.send_json({"type": "status", "message": "Scanning chat..."})

            seen_comments, seen_numbers = set(), set()
            empty_cycles = 0

            while True:
                elements = await page.query_selector_all("div[data-e2e='chat-message']")
                if not elements:
                    elements = await page.query_selector_all("[class*='ChatMessage']")
                if not elements:
                    elements = await page.query_selector_all("[class*='chat-message']")
                if not elements:
                    elements = await page.query_selector_all("[class*='chatMessage']")

                if not elements:
                    empty_cycles += 1
                    await websocket.send_json({
                        "type": "status",
                        "message": f"Waiting for chat... ({empty_cycles * 2}s)"
                    })
                    await asyncio.sleep(2)
                    continue

                empty_cycles = 0

                for el in elements:
                    try:
                        text = (await el.inner_text()).strip()
                    except Exception:
                        continue
                    if not text or text in seen_comments:
                        continue
                    seen_comments.add(text)
                    for num in extract_numbers(text):
                        if num not in seen_numbers:
                            seen_numbers.add(num)
                            await websocket.send_json({
                                "type": "number",
                                "number": num,
                                "comment": text[:120],
                                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                            })

                await asyncio.sleep(2)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass