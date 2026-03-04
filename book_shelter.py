import asyncio, re, json
from datetime import datetime
from playwright.async_api import async_playwright

with open('config.json') as f:
    cfg = json.load(f)

DATA         = cfg['data']
TARGET_URL   = cfg['shelterUrl']
TARGET_DAY   = cfg['targetDay']
TARGET_MONTH = cfg['targetMonth']
MAX_ATTEMPTS = 5

COOKIE_BTN = "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"
SUBMIT_SELECTORS = ['a.place-submitbtn', '.place-submitbtn', "a:has-text('Book nu')"]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

async def accept_cookies(page):
    try:
        await page.click(COOKIE_BTN, timeout=3000)
        log("Cookies accepted.")
    except Exception:
        pass

async def navigate_to_month(page, target_month):
    log(f"Navigating to {target_month}...")
    next_btn = page.locator("i.fa-chevron-right").first
    for _ in range(18):
        header = await page.locator(".calendar-header").inner_text()
        if target_month.lower() in header.lower():
            log(f"Arrived at {target_month}!")
            return
        await next_btn.click()
        await asyncio.sleep(0.4)
    raise RuntimeError(f"Could not find month: {target_month}")

async def click_day(page, day):
    log(f"Looking for day {day}...")
    pattern = re.compile(r"^\s*" + re.escape(day) + r"\s*$")
    day_divs = page.locator("div.day").filter(has_text=pattern)
    count = await day_divs.count()
    if count == 0:
        raise RuntimeError(f"Day {day} not found!")
    clicked = False
    for i in range(count):
        div = day_divs.nth(i)
        td_class = await div.locator("xpath=ancestor::td[1]").get_attribute("class") or ""
        if "day-overlay-occupied" in td_class or "td-disabled" in td_class:
            log(f"  Cell {i+1} booked, skipping")
            continue
        log(f"  Cell {i+1} available - clicking!")
        await div.click(force=True)
        clicked = True
        break
    if not clicked:
        raise RuntimeError(f"Day {day} is fully booked!")
    try:
        await page.wait_for_function("() => document.body.innerText.includes('Fra:')", timeout=6000)
        log("Date selection confirmed!")
    except Exception:
        log("Fra: not detected - continuing...")
    await asyncio.sleep(0.8)

async def fill_email_confirm(page, email):
    await page.evaluate(
        """(v) => {
            var el = document.querySelector('#Email2');
            if (!el) return;
            var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(el, v);
            ['input','change','blur'].forEach(function(e) {
                el.dispatchEvent(new Event(e, {bubbles: true}));
            });
        }""",
        email
    )
    log("EmailConfirm set via JS")

async def fill_fields(page):
    log("Waiting for form...")
    await page.wait_for_selector("div.place-form-wrapper", state="visible", timeout=8000)
    await page.fill("#Firstname",  DATA["fornavn"])
    await page.fill("#Lastname",   DATA["efternavn"])
    await page.fill("#Email",      DATA["email"])
    await fill_email_confirm(page, DATA["email"])
    await page.fill("#Phone",      DATA["telefon"])
    await page.fill("#PeopleQuantity", DATA['antal'])
    await page.check("input[name='B_Confirm']")
    await page.check("input[name='B_ConfirmPrivacy']")
    log("Form filled!")

async def click_submit(page):
    for sel in SUBMIT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                log(f"Clicking: {sel}")
                await btn.click()
                return True
        except Exception:
            continue
    return False

async def book_shelter():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=60)
        page = await browser.new_page()
        log(f"Opening {TARGET_URL}")
        await page.goto(TARGET_URL)
        await accept_cookies(page)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                log(f"Attempt {attempt}/{MAX_ATTEMPTS}")
                await page.reload()
                await asyncio.sleep(1.5)
                await accept_cookies(page)
                await navigate_to_month(page, TARGET_MONTH)
                await click_day(page, TARGET_DAY)
                await fill_fields(page)
                if not await click_submit(page):
                    raise RuntimeError("Submit button not found!")
                await asyncio.sleep(3)
                body = (await page.content()).lower()
                done = any(k in body for k in ["bekraeftelse","tak","booket","succes","kvittering"])
                log(f"CONFIRMED! Check {DATA['email']}" if done else f"Submitted - check {DATA['email']}")
                ts = datetime.now().strftime('%H%M%S')
                await page.screenshot(path=f"booking_{ts}.png", full_page=True)
                log("ALL DONE!")
                break
            except Exception as e:
                log(f"Attempt {attempt} failed: {e}")
                try:
                    await page.screenshot(path=f"error_{attempt}.png", full_page=True)
                except Exception:
                    pass
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(2)
                else:
                    log("All attempts exhausted.")
        await browser.close()

if __name__ == "__main__":
    log(f"Booking {TARGET_DAY} {TARGET_MONTH} on {TARGET_URL}")
    asyncio.run(book_shelter())
