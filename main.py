import asyncio
import re
import time
import httpx
import phonenumbers
import firebase_admin
from firebase_admin import credentials, firestore, auth
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
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

def extract_username(url: str) -> str:
    match = re.search(r'tiktok\.com/@([^/]+)', url)
    if match:
        return match.group(1)
    return ""

async def get_room_id(username: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.tiktok.com/"
    }
    url = f"https://www.tiktok.com/@{username}/live"
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        resp = await client.get(url, headers=headers)
        text = resp.text
        match = re.search(r'"roomId"\s*:\s*"(\d+)"', text)
        if match:
            return match.group(1)
        match = re.search(r'room_id=(\d+)', text)
        if match:
            return match.group(1)
    return ""

async def fetch_chat(room_id: str, cursor: str = "0"):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.tiktok.com/",
    }
    params = {
        "room_id": room_id,
        "cursor": cursor,
        "count": "20"
    }
    url = "https://www.tiktok.com/api/live/get_chat/"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            return resp.json()
    return {}

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

        username = extract_username(live_url)
        if not username:
            await websocket.send_json({"type": "error", "message": "Invalid TikTok URL"})
            return

        await websocket.send_json({"type": "status", "message": f"Finding live stream for @{username}..."})

        room_id = await get_room_id(username)
        if not room_id:
            await websocket.send_json({"type": "error", "message": f"Could not find live stream for @{username}. Make sure they are live right now."})
            return

        await websocket.send_json({"type": "status", "message": f"Connected! Room ID: {room_id}. Monitoring chat..."})

        seen_comments = set()
        seen_numbers = set()
        cursor = "0"

        while True:
            try:
                data = await fetch_chat(room_id, cursor)

                if not data:
                    await websocket.send_json({"type": "status", "message": "Waiting for messages..."})
                    await asyncio.sleep(3)
                    continue

                messages = data.get("data", {}).get("messages", [])
                if not messages:
                    messages = data.get("messages", [])

                for msg in messages:
                    try:
                        content = ""
                        if isinstance(msg, dict):
                            content = msg.get("content", "") or msg.get("comment", "") or msg.get("text", "") or str(msg)
                        if not content or content in seen_comments:
                            continue
                        seen_comments.add(content)

                        numbers = extract_numbers(content)
                        for num in numbers:
                            if num not in seen_numbers:
                                seen_numbers.add(num)
                                await websocket.send_json({
                                    "type": "number",
                                    "number": num,
                                    "comment": content[:120],
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                                })
                    except Exception:
                        continue

                new_cursor = data.get("data", {}).get("cursor", "")
                if new_cursor:
                    cursor = str(new_cursor)

            except Exception as e:
                await websocket.send_json({"type": "status", "message": f"Retrying... {str(e)[:40]}"})

            await asyncio.sleep(3)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass