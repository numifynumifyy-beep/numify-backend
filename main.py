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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.tiktok.com/"
    }
    url = f"https://www.tiktok.com/@{username}/live"
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        resp = await client.get(url, headers=headers)
        text = resp.text
        match = re.search(r'"roomId"\s*:\s*"(\d+)"', text)
        if match:
            return match.group(1)
        match = re.search(r'room_id=(\d+)', text)
        if match:
            return match.group(1)
        match = re.search(r'"liveRoomId"\s*:\s*"(\d+)"', text)
        if match:
            return match.group(1)
    return ""

async def fetch_chat_messages(room_id: str):
    all_messages = []

    urls_to_try = [
        f"https://webcast.tiktok.com/webcast/im/fetch/?room_id={room_id}&cursor=0&internal_ext=&fetch_rule=1&version_code=180800",
        f"https://www.tiktok.com/api/live/get_chat/?room_id={room_id}&cursor=0&count=50",
        f"https://webcast.us.tiktok.com/webcast/im/fetch/?room_id={room_id}&cursor=0&fetch_rule=1",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
    }

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for url in urls_to_try:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        msgs = (
                            data.get("data", {}).get("messages", []) or
                            data.get("messages", []) or
                            data.get("data", []) or
                            []
                        )
                        if msgs:
                            for m in msgs:
                                if isinstance(m, dict):
                                    text = (
                                        m.get("content") or
                                        m.get("comment") or
                                        m.get("text") or
                                        m.get("message") or ""
                                    )
                                    if text:
                                        all_messages.append(text)
                            if all_messages:
                                break
                    except Exception:
                        text_content = resp.text
                        words = re.findall(r'"content"\s*:\s*"([^"]+)"', text_content)
                        all_messages.extend(words)
                        if all_messages:
                            break
            except Exception:
                continue

    return all_messages

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
            await websocket.send_json({
                "type": "error",
                "message": f"Could not find live stream for @{username}. Make sure they are live right now."
            })
            return

        await websocket.send_json({
            "type": "status",
            "message": f"Connected! Monitoring chat for @{username}..."
        })

        seen_comments = set()
        seen_numbers = set()
        cycle = 0

        while True:
            cycle += 1
            try:
                messages = await fetch_chat_messages(room_id)

                await websocket.send_json({
                    "type": "status",
                    "message": f"Scanning... found {len(messages)} messages this cycle"
                })

                for content in messages:
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

            except Exception as e:
                await websocket.send_json({
                    "type": "status",
                    "message": f"Cycle {cycle} error: {str(e)[:60]}"
                })

            await asyncio.sleep(3)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass