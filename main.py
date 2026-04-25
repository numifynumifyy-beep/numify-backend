import os
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
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
            except Exception as e:
                await websocket.send_json({"type": "status", "message": "Page loaded with warnings, continuing..."})

            # Wait for page to load
            await asyncio.sleep(8)

            await websocket.send_json({"type": "status", "message": "Scanning chat..."})

            seen_comments, seen_numbers = set(), set()
            empty_cycles = 0

            while True:
                # Try multiple selectors
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
                        "message": f"Waiting for chat messages... ({empty_cycles * 2}s)"
                    })
                    if empty_cycles > 30:
                        await websocket.send_json({
                            "type": "status",
                            "message": "No chat found. Make sure the live is active."
                        })
                    await asyncio.sleep(2)
                    continue

                empty_cycles = 0

                for el in elements:
                    try:
                        text = (await el.inner_text()).strip()
                    except:
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
        except:
            pass