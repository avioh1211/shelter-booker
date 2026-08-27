import asyncio, re, os, json, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright

with open('config.json') as f:
    cfg = json.load(f)

DATA          = cfg['data']
TARGET_URL    = cfg['shelterUrl']
TARGET_DAY    = cfg['targetDay']
TARGET_MONTH  = cfg['targetMonth']
TARGET_DT_UTC = cfg['targetDateTimeUTC']   # ISO string, e.g. "2027-08-01T00:01:00+00:00"
MAX_ATTEMPTS  = 5
LEAD_SECONDS  = 90   # start the fast poll loop this many seconds before the target time

TEST_MODE = os.environ.get("TEST_MODE", "false").strip().lower() == "true"
TIMED_TEST = os.environ.get("TIMED_TEST", "false").strip().lower() == "true"
TEST_TARGET_MINUTES = float(os.environ.get("TEST_TARGET_MINUTES", "5") or 5)

COOKIE_BTN = "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"
SUBMIT_SELECTORS = ['a.place-submitbtn', '.place-submitbtn', "a:has-text('Book nu')"]

RUN_URL = ""
_server = os.environ.get("GITHUB_SERVER_URL")
_repo = os.environ.get("GITHUB_REPOSITORY")
_run_id = os.environ.get("GITHUB_RUN_ID")
if _server and _repo and _run_id:
    RUN_URL = f"{_server}/{_repo}/actions/runs/{_run_id}"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def send_notification(subject, message):
    """Best-effort email via FormSubmit.co — no API key or secret needed.
    The first email ever sent to a given address requires a one-time click
    to confirm; every send after that arrives directly."""
    email = DATA.get("email")
    if not email:
        return
    body = message
    if RUN_URL:
        body += f"\n\nFull run (with screenshots): {RUN_URL}"
    payload = json.dumps({
        "_subject": subject,
        "message": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://formsubmit.co/ajax/{email}",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"Notification sent (HTTP {resp.status}).")
    except Exception as e:
        log(f"Could not send notification email: {e}")


async def accept_cookies(page):
    try:
        await page.click(COOKIE_BTN, timeout=800)
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


async def find_day_cell(page, day):
    """Locate the target day's table cell, if the calendar shows it at all."""
    pattern = re.compile(r"^\s*" + re.escape(day) + r"\s*$")
    day_divs = page.locator("div.day").filter(has_text=pattern)
    count = await day_divs.count()
    for i in range(count):
        div = day_divs.nth(i)
        td = div.locator("xpath=ancestor::td[1]")
        td_class = await td.get_attribute("class") or ""
        yield div, td_class


async def is_day_available(page, day):
    async for _div, td_class in find_day_cell(page, day):
        if "td-disabled" not in td_class and "day-overlay-occupied" not in td_class:
            return True
    return False


async def day_cell_exists(page, day):
    async for _div, _td_class in find_day_cell(page, day):
        return True
    return False


async def refresh_and_check(page):
    """One full cycle: reload, re-accept cookies, get back to the right month,
    and check availability. Everything expensive lives here so the caller's
    loop stays as tight as the site allows."""
    await page.reload()
    await accept_cookies(page)
    await navigate_to_month(page, TARGET_MONTH)
    return await is_day_available(page, TARGET_DAY)


async def sleep_until_lead(target_dt):
    """Sleep (in one long stretch) until LEAD_SECONDS before target_dt.
    Uses the machine's own clock, not GitHub's cron trigger, so an early or
    late workflow start barely matters. Shared by real bookings and timed tests."""
    now = datetime.now(timezone.utc)
    seconds_until_lead = (target_dt - now).total_seconds() - LEAD_SECONDS
    if seconds_until_lead > 0:
        log(f"Target is {target_dt.isoformat()}. Sleeping {int(seconds_until_lead)}s until "
            f"{LEAD_SECONDS}s before it, then switching to fast polling.")
        await asyncio.sleep(seconds_until_lead)
    else:
        log("Already within the lead window (or past target) — polling starts now.")


async def wait_for_day_to_open(page):
    log(f"Fast-polling for day {TARGET_DAY} {TARGET_MONTH} to open...")
    refreshes = 0
    while True:
        try:
            if await refresh_and_check(page):
                log("DAY IS NOW OPEN! Booking immediately!")
                return
            refreshes += 1
            if refreshes % 10 == 0:
                log(f"Still waiting... ({refreshes} refreshes so far)")
        except Exception as e:
            log(f"Poll error: {e}")
        await asyncio.sleep(1)


async def click_day(page, day):
    log(f"Clicking day {day}...")
    clicked = False
    async for div, td_class in find_day_cell(page, day):
        if "td-disabled" in td_class or "day-overlay-occupied" in td_class:
            continue
        await div.click(force=True)
        clicked = True
        break
    if not clicked:
        raise RuntimeError(f"Day {day} not available!")
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


async def fill_fields(page):
    log("Waiting for form...")
    await page.wait_for_selector("div.place-form-wrapper", state="visible", timeout=8000)
    await page.fill("#Firstname",  DATA["fornavn"])
    await page.fill("#Lastname",   DATA["efternavn"])
    await page.fill("#Email",      DATA["email"])
    await fill_email_confirm(page, DATA["email"])
    await page.fill("#Phone",      DATA["telefon"])
    await page.fill("#PeopleQuantity", DATA["antal"])
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


async def run_test_mode(page):
    """Checks the whole pipeline works WITHOUT booking anything: site loads,
    cookies accept, the right month shows up, and the target day is at least
    present on the calendar (open or not)."""
    log("=== TEST MODE — will not submit a real booking ===")
    await page.goto(TARGET_URL)
    await accept_cookies(page)
    await navigate_to_month(page, TARGET_MONTH)
    found = await day_cell_exists(page, TARGET_DAY)
    available_now = await is_day_available(page, TARGET_DAY) if found else False
    await page.screenshot(path="test_run.png", full_page=True)
    if not found:
        raise RuntimeError(
            f"Day {TARGET_DAY} was not found in {TARGET_MONTH} on the calendar. "
            "Check that targetDay/targetMonth match the shelter site's own wording."
        )
    status = "already open" if available_now else "present but not yet open (expected)"
    return f"Test passed. Site reachable, cookies OK, month navigation OK, day {TARGET_DAY} {status}."


async def run_timed_test(page):
    """Simulates a target N minutes from now and runs the exact same
    sleep-then-fast-poll sequence as a real booking, but stops the instant
    the day would be clickable instead of actually clicking or submitting.
    Lets you watch the real timing logic work without waiting for game day
    (or risking a real submission)."""
    target = datetime.now(timezone.utc) + timedelta(minutes=TEST_TARGET_MINUTES)
    log(f"=== TIMED TEST — simulating target {target.isoformat()} "
        f"({TEST_TARGET_MINUTES:g} min from now). Nothing will be booked. ===")
    await page.goto(TARGET_URL)
    await accept_cookies(page)
    await navigate_to_month(page, TARGET_MONTH)

    await sleep_until_lead(target)

    log("Entering fast-poll phase (timed test — will NOT click or submit).")
    deadline = target + timedelta(seconds=60)  # keep watching a bit past the simulated moment
    refreshes = 0
    opened = False
    while datetime.now(timezone.utc) < deadline:
        try:
            if await refresh_and_check(page):
                opened = True
                log("Day shows as OPEN at this point — a real run would click it now. Stopping here.")
                break
            refreshes += 1
        except Exception as e:
            log(f"Poll error: {e}")
        await asyncio.sleep(1)

    await page.screenshot(path="timed_test.png", full_page=True)
    drift = (datetime.now(timezone.utc) - target).total_seconds()
    if opened:
        return (f"Timed test complete. Reached the simulated target and the day showed as OPEN "
                f"after {refreshes} refresh(es). No booking was submitted.")
    return (f"Timed test complete. Simulated target passed {int(drift)}s ago; the day did not show "
            f"as open in the watch window after {refreshes} refresh(es) (expected on a closed/non-live "
            f"date). No booking was submitted.")


async def run_real_booking(page):
    await page.goto(TARGET_URL)
    await accept_cookies(page)
    await navigate_to_month(page, TARGET_MONTH)

    target = datetime.fromisoformat(TARGET_DT_UTC.replace("Z", "+00:00"))
    await sleep_until_lead(target)
    await wait_for_day_to_open(page)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(f"Attempt {attempt}/{MAX_ATTEMPTS}")
            if attempt > 1:
                await refresh_and_check(page)
            await click_day(page, TARGET_DAY)
            await fill_fields(page)
            if not await click_submit(page):
                raise RuntimeError("Submit button not found!")
            await asyncio.sleep(3)
            body = (await page.content()).lower()
            done = any(k in body for k in ["bekraeftelse", "tak", "booket", "succes", "kvittering"])
            ts = datetime.now().strftime('%H%M%S')
            await page.screenshot(path=f"booking_{ts}.png", full_page=True)
            if done:
                return f"Booking CONFIRMED for {TARGET_DAY} {TARGET_MONTH}. Check {DATA['email']} for the official confirmation."
            return f"Form submitted for {TARGET_DAY} {TARGET_MONTH}, but confirmation text wasn't detected — please double-check {DATA['email']} and the site."
        except Exception as e:
            last_error = e
            log(f"Attempt {attempt} failed: {e}")
            try:
                await page.screenshot(path=f"error_{attempt}.png", full_page=True)
            except Exception:
                pass
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(2)

    raise RuntimeError(f"All {MAX_ATTEMPTS} attempts failed. Last error: {last_error}")


async def book_shelter():
    result_message = None
    result_ok = False
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=60)
        page = await browser.new_page()
        try:
            if TIMED_TEST:
                result_message = await run_timed_test(page)
            elif TEST_MODE:
                result_message = await run_test_mode(page)
            else:
                result_message = await run_real_booking(page)
            result_ok = True
        except Exception as e:
            result_message = f"FAILED: {e}"
            try:
                await page.screenshot(path="fatal_error.png", full_page=True)
            except Exception:
                pass
        finally:
            await browser.close()

    log(result_message)
    prefix = "[TIMED TEST] " if TIMED_TEST else ("[TEST] " if TEST_MODE else "")
    subject = prefix + ("Shelter booking OK" if result_ok else "Shelter booking FAILED")
    send_notification(subject, result_message)
    if not result_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    mode = "TIMED TEST" if TIMED_TEST else ("TEST" if TEST_MODE else "LIVE")
    log(f"[{mode}] Booking {TARGET_DAY} {TARGET_MONTH} on {TARGET_URL}")
    asyncio.run(book_shelter())
