"""
LG4All NGSI Customer Bidding Automation (Automatic OTP Submission)
==============================================================================
Features:
  1. Sequential page-by-page bidding with ultra-fast row selection (< 2ms).
  2. Real ASP.NET server postback on Submit to dispatch the OTP email.
  3. Automatic Skip: skips pages with no inventory at the configured target percentage.
  4. 📧 Automated Email OTP Reader:
     - Connects securely to your Gmail inbox via IMAP.
     - Automatically intercepts the OTP email as soon as it arrives.
     - Parses the 6-digit code.
  5. Fills and automatically submits the OTP as soon as it is detected.
  6. 🔒 Zero-Refresh Lock: Refresh is strictly blocked during selection and OTP.
  7. Live Inventory Monitoring: Automatically polls when idle and jumps into
     action the moment new inventory at the configured target percentage is uploaded by LG.

Requirements:
    pip install selenium webdriver-manager imapclient

Usage:
    python lg4all_automate.py
"""

import sys
import time
import winsound
import email
import threading
import csv
import os
from queue import Empty, Queue
from imapclient import IMAPClient

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from email.header import decode_header
from email.utils import parsedate_to_datetime
import re
from datetime import datetime, timezone

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    UnexpectedAlertPresentException,
    NoAlertPresentException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────
TARGET_URL              = "https://www.lg4all.com/pod/?Code=IN24997500"
TARGET_DISCOUNT_PCT     = "70"  # Set to "50" or "70" to choose the one target percentage
TARGET_DISCOUNT_LABEL   = f"{TARGET_DISCOUNT_PCT}%"
AUTO_MONITOR_UPDATES    = True    # If True, watches page for new inventory when idle
POLL_INTERVAL_SECONDS   = 0.25     # Fallback refresh interval when the page has no live DOM updates
FAST_POLL_TICK          = 0.01    # Ultra-high-frequency poll tick (10ms) for zero-latency responsiveness

# ─────────────────────────────────────────────────────────
# EMAIL OTP & SUBMISSION BEHAVIOR
# ─────────────────────────────────────────────────────────
AUTO_READ_EMAIL_OTP     = True    # True: auto-fetch OTP from Gmail inbox
AUTO_SUBMIT_OTP         = True     # Submit immediately after filling the detected OTP
DIAGNOSTIC_STOP_AFTER_OTP_FILL = os.getenv("LG4ALL_DIAGNOSTIC_STOP_AFTER_OTP_FILL", "") == "1"
GMAIL_ADDRESS           = os.getenv("LG4ALL_GMAIL_ADDRESS", "rakeshrpandey8253@gmail.com").strip()
GMAIL_APP_PASSWORD      = os.getenv("LG4ALL_GMAIL_APP_PASSWORD", "dbec pyix cdcn drok").strip()
EMAIL_POLL_TIMEOUT      = 60      # Max seconds to wait for OTP email before manual prompt
OTP_POLL_INTERVAL       = 0.1     # Fast inbox checks; keeps the IMAP session open between checks
# ─────────────────────────────────────────────────────────

SESSION_CSV_FILENAME = f"successful_bids_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv"


def latest_mail_uid(mail) -> int:
    """Return the highest inbox UID so this bid only accepts a newly-arrived email."""
    uids = mail.search("ALL")
    return max((int(uid) for uid in uids), default=0)


def latest_otp_uid(mail) -> int:
    """Return the newest UID from the expected LG Grade A OTP sender/subject."""
    uids = mail.search([
        "FROM", "LG_GRADE_A_SALES@lge.com",
        "SUBJECT", "OTP Validation for NGSI Grade A",
    ])
    return max((int(uid) for uid in uids), default=0)


def start_otp_listener():
    """Start looking for this bid's OTP before the submit postback is sent."""
    if not (AUTO_READ_EMAIL_OTP and GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        return None

    # Timestamp and launch the mailbox baseline asynchronously. The IMAP search
    # runs while Selenium selects rows and dispatches the OTP request, so it
    # cannot add its network round trip to the inventory-selection path.
    request_started_at = datetime.now(timezone.utc)
    result_queue = Queue(maxsize=1)

    def listen():
        try:
            mail = GLOBAL_IMAP_CLIENT or get_imap_client(validate_existing=False)
            if mail is None:
                raise ConnectionError("Gmail IMAP connection is unavailable before OTP request.")
            baseline_started = time.perf_counter()
            baseline_uid = latest_otp_uid(mail)
            print(f"  [OTP Timing] Per-request mailbox baseline ready in {(time.perf_counter() - baseline_started) * 1000:.1f}ms (parallel with selection).")
            code = fetch_otp_from_gmail(
                max_age_seconds=180,
                timeout=EMAIL_POLL_TIMEOUT,
                after_uid=baseline_uid,
                after_timestamp=request_started_at.timestamp(),
            )
            result_queue.put_nowait(code)
        except Exception as exc:
            print(f"\n  [Email Reader] OTP listener failed: {exc}")
            result_queue.put_nowait("")

    threading.Thread(target=listen, name="otp-listener", daemon=True).start()
    return result_queue

def background_save_bids(items, page_num):
    """Saves successful bids to CSV in the background with 0 latency impact."""
    if not items:
        return
    try:
        file_exists = os.path.isfile(SESSION_CSV_FILENAME)
        with open(SESSION_CSV_FILENAME, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Page", "Serial Number", "Model Code", "Price"])
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in items:
                writer.writerow([timestamp, page_num, item.get("serial", ""), item.get("model", ""), item.get("price", "")])
    except Exception:
        pass


def init_driver() -> webdriver.Chrome:
    """Initialize an optimized Chrome WebDriver instance."""
    options = Options()
    options.page_load_strategy = "eager"  # 'eager' loads DOM immediately without waiting for images/CSS

    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--no-first-run")
    options.add_argument("--log-level=3")
    
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)
    return driver


def safe_clear_alert(driver) -> str:
    """Dismiss or accept any unexpected modal alert to prevent crashes."""
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        return text
    except Exception:
        return ""


def lock_browser_against_refresh(driver):
    """
    Prevents any browser-level reload, navigation, or unload events
    while row selection and OTP entry are actively in progress.
    """
    try:
        driver.execute_script("""
            window.__bidding_locked = true;
            window.addEventListener('beforeunload', function(e) {
                if (window.__bidding_locked) {
                    e.preventDefault();
                    e.returnValue = 'Selection or OTP in progress — do not refresh!';
                    return e.returnValue;
                }
            }, { capture: true });
        """)
    except Exception:
        pass


def unlock_browser(driver):
    """Releases the browser protection lock after bidding is completely saved."""
    try:
        driver.execute_script("window.__bidding_locked = false;")
    except Exception:
        pass


def notify_user_alert(alert_type="otp"):
    """Emit audio tones so user is alerted immediately."""
    try:
        if alert_type == "update":
            winsound.Beep(900, 120)
            time.sleep(0.04)
            winsound.Beep(1200, 140)
            time.sleep(0.04)
            winsound.Beep(1500, 200)
        else:
            winsound.Beep(1200, 180)
            time.sleep(0.08)
            winsound.Beep(1600, 220)
    except Exception:
        pass


def wait_for_grid_ready(driver, timeout=20):
    """Wait until the grid table is present, or 'No Data' is displayed (in DOM or via Alert)."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        # Check for alert first to prevent UnexpectedAlertPresentException
        try:
            alert = driver.switch_to.alert
            alert_text = alert.text
            alert.accept()
            if "no data" in alert_text.lower() or "no record" in alert_text.lower():
                return
        except Exception:
            pass

        try:
            ready = driver.execute_script("""
                var g = document.getElementById('gvNGSIDetails');
                if (g && g.rows && g.rows.length > 1) return true;
                var text = document.body ? (document.body.innerText || document.body.textContent || "") : "";
                if (text.match(/no data/i) || text.match(/no record/i)) return true;
                return false;
            """)
            if ready:
                return
        except Exception:
            pass
            
        time.sleep(FAST_POLL_TICK)


def spawn_background_tab(driver, target_url):
    """Create a spare tab in the background without switching the active tab."""
    old_handles = set(driver.window_handles)

    try:
        # CDP's background flag prevents Chrome from selecting the newly created target.
        driver.execute_cdp_cmd("Target.createTarget", {"url": target_url, "background": True})
    except WebDriverException as exc:
        raise RuntimeError(f"Chrome could not create a background spare tab: {exc}") from exc

    WebDriverWait(driver, 5, poll_frequency=FAST_POLL_TICK).until(
        lambda d: any(handle not in old_handles for handle in d.window_handles)
    )
    new_handle = [h for h in driver.window_handles if h not in old_handles][0]

    driver.background_tab_handle = new_handle
    return new_handle


def open_initial_tabs_concurrently(driver, target_url):
    """Start the spare in the background and keep the first tab focused throughout."""
    # Create/load the spare without selecting it, while the first tab remains selected.
    spare_handle = spawn_background_tab(driver, target_url)

    # Start the first tab's load while the spare continues loading in the background.
    driver.execute_script("window.location.replace(arguments[0]);", target_url)
    wait_for_grid_ready(driver, timeout=20)
    return spare_handle


def rotate_to_spare_tab(driver):
    """Close the completed tab, activate the prepared spare, and replenish the pool."""
    spare_handle = getattr(driver, "background_tab_handle", None)
    if not spare_handle or spare_handle not in driver.window_handles:
        spare_handle = spawn_background_tab(driver, TARGET_URL)

    driver.close()
    # close() is synchronous in WebDriver; switch directly instead of polling
    # the handle list and adding an avoidable round trip.
    driver.switch_to.window(spare_handle)
    driver.background_tab_handle = None

    # Start the next spare loading first, while keeping the selected tab active.
    try:
        spawn_background_tab(driver, TARGET_URL)
    except WebDriverException as error:
        print(f"  [Warning] Could not pre-open the next spare tab: {error}")

    # A previous bid can reshuffle the listing. Reload the newly active spare
    # so its scan starts from current inventory, while CDP returns without
    # Selenium's normal navigation wait; the grid-ready check gates scanning.
    old_document = driver.execute_script("return performance.timeOrigin")
    reload_started = time.perf_counter()
    driver.execute_cdp_cmd("Page.reload", {"ignoreCache": True})
    def document_was_reloaded(d):
        try:
            return d.execute_script("return performance.timeOrigin") != old_document
        except WebDriverException:
            return False

    WebDriverWait(driver, 20, poll_frequency=FAST_POLL_TICK).until(document_was_reloaded)
    wait_for_grid_ready(driver, timeout=20)
    print(f"  [Tab Rotation] Refreshed spare tab and grid ready in {(time.perf_counter() - reload_started):.3f}s.")
    return spare_handle


def get_total_pages(driver) -> int:
    """Detect total grid page count with multiple heuristics."""
    try:
        pages = driver.execute_script("""
            var pageNums = [];
            var cells = document.querySelectorAll('#gvNGSIDetails table td');
            for (var i = 0; i < cells.length; i++) {
                var t = cells[i].innerText.trim();
                if (/^\\d+$/.test(t)) {
                    pageNums.push(parseInt(t, 10));
                }
            }
            var links = document.querySelectorAll('#gvNGSIDetails a[href*="Page$"]');
            for (var j = 0; j < links.length; j++) {
                var match = links[j].href.match(/Page\\$(\\d+)/);
                if (match) {
                    pageNums.push(parseInt(match[1], 10));
                }
            }
            return pageNums.length > 0 ? Math.max.apply(null, pageNums) : 1;
        """)
        return max(1, int(pages))
    except Exception:
        pass
    return 1


def navigate_to_page(driver, page_number: int, max_retries=3) -> bool:
    """Fast and robust navigation to a specific grid page."""
    if page_number == 1:
        return True

    print(f"  Navigating to Page {page_number} ...")

    for attempt in range(1, max_retries + 1):
        try:
            safe_clear_alert(driver)
            old_grid = driver.find_element(By.ID, "gvNGSIDetails")

            dispatched = driver.execute_script(f"""
                var targetPage = "{page_number}";
                var links = document.querySelectorAll('#gvNGSIDetails table td a, #gvNGSIDetails a[href*="Page$"]');
                for (var i = 0; i < links.length; i++) {{
                    if (links[i].innerText.trim() === targetPage || links[i].href.indexOf('Page$' + targetPage) !== -1) {{
                        links[i].click();
                        return true;
                    }}
                }}
                if (typeof __doPostBack === 'function') {{
                    __doPostBack('ctl00$ContentPlaceHolder1$gvNGSIDetails', 'Page$' + targetPage);
                    return true;
                }}
                return false;
            """)

            if not dispatched:
                print(f"  Could not dispatch navigation to page {page_number}.")

            try:
                WebDriverWait(driver, 10, poll_frequency=FAST_POLL_TICK).until(EC.staleness_of(old_grid))
            except TimeoutException:
                pass

            wait_for_grid_ready(driver, timeout=15)
            print(f"  Page {page_number} loaded successfully.")
            return True

        except Exception as e:
            print(f"  [Attempt {attempt}/{max_retries}] Navigation notice: {e}")
            time.sleep(0.5)

    return False


def count_matching_rows(driver) -> int:
    """Check eligibility without changing the page, so empty pages skip OTP setup."""
    return int(driver.execute_script("""
        const target = arguments[0];
        return Array.from(document.querySelectorAll('#gvNGSIDetails tr')).slice(1).filter(row => {
            const discount = row.querySelector('[id$="lbldp_gv"]');
            const checkbox = row.querySelector('[id$="chkSelect"]');
            return discount && checkbox && !checkbox.disabled &&
                target.includes(discount.innerText.trim().replace('%', ''));
        }).length;
    """, [TARGET_DISCOUNT_PCT]) or 0)


def select_all_matching_rows(driver, submit_for_otp=False) -> dict:
    """Select matching rows and optionally trigger the OTP postback in the same JS task."""
    result = driver.execute_script("""
        var targetDPs = arguments[0];
        var selected_serials = [];
        var selected_items = [];
        var rows = document.querySelectorAll('#gvNGSIDetails tr');

        for (var i = 1; i < rows.length; i++) {
            var row = rows[i];
            var dpEl  = row.querySelector('[id$="lbldp_gv"]');
            var chkEl = row.querySelector('[id$="chkSelect"]');

            if (dpEl && chkEl && !chkEl.disabled) {
                var dpVal = dpEl.innerText.trim().replace('%', '');
                if (targetDPs.indexOf(dpVal) !== -1) {
                    chkEl.checked = true;
                    chkEl.dispatchEvent(new Event('change', { bubbles: true }));
                    
                    var serialEl = row.querySelector('[id$="lblsrno_gv"]');
                    var mcEl = row.querySelector('[id$="lblModelCode_gv"]');
                    var prEl = row.querySelector('[id$="lbldealerp_gv"]');
                    
                    var serial = serialEl ? serialEl.innerText.trim() : ('Row ' + i);
                    var model = mcEl ? mcEl.innerText.trim() : '';
                    var price = prEl ? prEl.innerText.trim() : '';
                    
                    selected_serials.push(serial);
                    selected_items.push({serial: serial, model: model, price: price});
                }
            }
        }

        var submitted = false;
        if (arguments[1] && selected_serials.length) {
            var saveButton = document.getElementById('btnSave');
            if (saveButton) {
                saveButton.click();
                submitted = true;
            } else if (typeof __doPostBack === 'function') {
                __doPostBack('ctl00$ContentPlaceHolder1$btnSave', '');
                submitted = true;
            }
        }

        return {
            count: selected_serials.length,
            serials: selected_serials,
            items: selected_items,
            submitted: submitted
        };
    """, [TARGET_DISCOUNT_PCT], submit_for_otp)

    count = int(result.get("count", 0)) if result else 0
    serials = result.get("serials", []) if result else []
    items = result.get("items", []) if result else []
    return {"count": count, "serials": serials, "items": items,
            "submitted": bool(result.get("submitted", False)) if result else False}


def wait_for_inventory_dom_update(driver, timeout: float) -> bool:
    """Wake immediately on client-side grid updates; return False for refresh fallback."""
    driver.set_script_timeout(timeout + 2)
    try:
        return bool(driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            const grid = document.getElementById('gvNGSIDetails');
            if (!grid) return done(false);

            let finished = false;
            const finish = changed => {
                if (finished) return;
                finished = true;
                window.__inventoryWaiter = null;
                clearTimeout(timer);
                done(changed);
            };
            const timer = setTimeout(() => finish(false), arguments[0] * 1000);
            window.__inventoryWaiter = () => finish(true);

            if (!window.__inventoryObserver) {
                window.__inventoryObserver = new MutationObserver(() => {
                    if (window.__inventoryWaiter) window.__inventoryWaiter();
                });
                window.__inventoryObserver.observe(grid, {
                    childList: true,
                    characterData: true,
                    subtree: true
                });
            }
        """, timeout))
    except WebDriverException:
        return False


def accept_submission_alerts(driver, count: int = 2):
    """Accept each chained JavaScript alert as soon as Chrome exposes it."""
    started = time.perf_counter()
    responses = []

    for index in range(count):
        # The first alert can be delayed by OTP validation; the second follows
        # the save postback after the first is accepted. Poll tightly but keep
        # a bounded wait for the server response.
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            try:
                alert = driver.switch_to.alert
                response = alert.text
                alert.accept()
                responses.append(response)
                print(f"  Server response {index + 1}: {response}")
                break
            except NoAlertPresentException:
                time.sleep(0.005)
        else:
            break

    print(f"  [OTP Timing] Accepted {len(responses)} alert(s) in {(time.perf_counter() - started) * 1000:.1f}ms.")
    return responses


def submit_and_wait_for_otp_modal(driver, timeout=30, submit=True):
    """Executes the REAL server submit postback to trigger the OTP email."""
    if submit:
        print("  Triggering Submit postback to server (requesting OTP email) ...")
        driver.execute_script("""
            var btn = document.getElementById('btnSave');
            if (btn) btn.click();
            else if (typeof __doPostBack === 'function')
                __doPostBack('ctl00$ContentPlaceHolder1$btnSave', '');
        """)

    print("  Submit sent — waiting for server to dispatch OTP and display modal ...")



    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            alert = driver.switch_to.alert
            print(f"  Server notice: {alert.text}")
            alert.accept()
        except Exception:
            pass

        try:
            is_open = driver.execute_script("""
                var modal = document.getElementById('otpModal');
                if (!modal) return false;
                var style = window.getComputedStyle(modal);
                return modal.style.display === 'block' || style.display === 'block';
            """)
            if is_open:
                print("  OTP modal is now open! Server has dispatched the OTP to your email.")
                return
        except Exception:
            pass
            
        time.sleep(FAST_POLL_TICK)


GLOBAL_IMAP_CLIENT = None
GLOBAL_IMAP_BASELINE_UID = 0

def get_imap_client(force_reconnect=False, validate_existing=True):
    global GLOBAL_IMAP_CLIENT
    clean_addr = GMAIL_ADDRESS.strip()
    clean_pass = GMAIL_APP_PASSWORD.replace(" ", "").strip()

    if not clean_addr or not clean_pass:
        return None

    if GLOBAL_IMAP_CLIENT is not None and not force_reconnect:
        try:
            if validate_existing:
                GLOBAL_IMAP_CLIENT.noop()
            return GLOBAL_IMAP_CLIENT
        except Exception:
            force_reconnect = True

    if GLOBAL_IMAP_CLIENT is not None:
        try:
            GLOBAL_IMAP_CLIENT.logout()
        except Exception:
            pass
        GLOBAL_IMAP_CLIENT = None

    try:
        client = IMAPClient("imap.gmail.com", ssl=True, timeout=10)
        client.login(clean_addr, clean_pass)
        client.select_folder("INBOX", readonly=True)
        client.noop()
        GLOBAL_IMAP_CLIENT = client
    except Exception as exc:
        try:
            client.logout()
        except Exception:
            pass
        GLOBAL_IMAP_CLIENT = None
        raise ConnectionError(f"Could not establish a Gmail IMAP session: {exc}") from exc

    return GLOBAL_IMAP_CLIENT


def connect_imap_with_retry(attempts: int = 4):
    """Connect and verify Gmail before Chrome is opened; retry brief network failures."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            client = get_imap_client(force_reconnect=attempt > 1)
            if client is None:
                raise RuntimeError("Gmail address or app password is not configured.")
            if not client.has_capability("IDLE"):
                print("  [Gmail] Connected; server lacks IDLE, so the listener will poll instead.")
            return client
        except Exception as exc:
            last_error = exc
            print(f"  [Gmail] Connection attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                time.sleep(min(0.5 * attempt, 2.0))
    raise ConnectionError(f"Gmail IMAP unavailable after {attempts} attempts: {last_error}")


def extract_otp_from_email(raw_email: bytes, max_age_seconds: int, after_timestamp=None, received_at=None):
    """Return (OTP, subject, age) from an email, tolerating small clock skew."""
    parsed_msg = email.message_from_bytes(raw_email)
    date_str = parsed_msg.get("Date", "")
    try:
        email_dt = parsedate_to_datetime(date_str)
    except Exception:
        email_dt = None
    received_timestamp = received_at.timestamp() if isinstance(received_at, datetime) else None
    message_timestamp = received_timestamp if received_timestamp is not None else (
        email_dt.timestamp() if email_dt is not None else None
    )
    if message_timestamp is None:
        age_sec = 0
    else:
        age_sec = datetime.now(timezone.utc).timestamp() - message_timestamp
    if age_sec > max_age_seconds or age_sec < -120:
        return None

    # IMAP INTERNALDATE is the mailbox receipt time. Prefer it to the sender's
    # Date header so a delayed or quoted message cannot supply a prior OTP.
    if after_timestamp is not None and message_timestamp is not None and message_timestamp < after_timestamp - 2:
        return None

    subject = ""
    for part, encoding in decode_header(parsed_msg.get("Subject", "")):
        if isinstance(part, bytes):
            subject += part.decode(encoding or "utf-8", errors="ignore")
        else:
            subject += str(part)

    # This inbox receives other mail and Gmail groups repeated OTP messages into
    # a conversation. Accept only the LG Grade A notification we expect.
    sender = parsed_msg.get("From", "").casefold()
    if "lg_grade_a_sales@lge.com" not in sender or "otp validation for ngsi grade a" not in subject.casefold():
        return None

    body_parts = []
    for part in parsed_msg.walk() if parsed_msg.is_multipart() else [parsed_msg]:
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            if part.get_content_type() == "text/plain":
                # Ignore quoted prior messages, which often contain the old OTP.
                body = re.split(
                    r"(?im)^\s*(?:On .{1,240}wrote:|[-_]{2,}\s*Original Message\s*[-_]*|From:\s+)",
                    body,
                    maxsplit=1,
                )[0]
                body = re.sub(r"(?m)^\s*>.*(?:\n|$)", " ", body)
            else:
                body = re.sub(r"(?is)<blockquote\b[^>]*>.*?</blockquote\s*>", " ", body)
                body = re.sub(r"(?is)<div\b[^>]*class=[\"'][^\"']*(?:gmail_quote|yahoo_quoted)[^\"']*[\"'][^>]*>.*", " ", body)
            body_parts.append(body)

    clean_text = re.sub(r"<style.*?</style>", " ", f"{subject}\n{' '.join(body_parts)}", flags=re.DOTALL | re.IGNORECASE)
    clean_text = re.sub(r"#[0-9a-fA-F]{3,8}\b", " ", clean_text)
    clean_text = re.sub(r"<[^>]+>", " ", clean_text)
    clean_text = re.sub(r"\s+", " ", clean_text)

    match = re.search(
        r"\bOTP\s+for\s+authentication\s+for\s+GRADE\s+A\s+customer\s+bidding\s+is\s+(\d{4,6})\b",
        clean_text,
        re.IGNORECASE,
    )
    if not match:
        return None

    code = match.group(1)
    if code == "000000" or len(set(code)) == 1:
        return None
    return code, subject, age_sec


def fetch_otp_from_gmail(max_age_seconds: int = 180, timeout: int = 60, after_uid: int = 0, after_timestamp=None) -> str:
    """
    Polls only messages newer than the UID captured before the OTP request.
    """
    clean_addr = GMAIL_ADDRESS.strip()
    if not clean_addr:
        return ""

    print("  [Email Reader] Monitoring configured Gmail inbox for OTP ...")
    deadline = time.time() + timeout
    listener_started = time.perf_counter()
    first_search = True
    # Reuse the already-validated persistent connection without adding a NOOP
    # round trip before the first mailbox search.
    mail = get_imap_client(validate_existing=False)

    while time.time() < deadline:
        if not mail:
            mail = get_imap_client(force_reconnect=True)
            if not mail:
                time.sleep(0.5)
                continue

        try:
            # Restrict the server-side search to this notification thread's
            # sender and subject, then fetch only its newest individual message.
            uid_range = f"{after_uid + 1}:*"
            search_started = time.perf_counter()
            msg_ids = mail.search([
                "UID", uid_range,
                "FROM", "LG_GRADE_A_SALES@lge.com",
                "SUBJECT", "OTP Validation for NGSI Grade A",
            ])
            search_ms = (time.perf_counter() - search_started) * 1000
            if first_search:
                print(f"  [OTP Timing] First filtered IMAP search: {search_ms:.1f}ms.")
                first_search = False
            if msg_ids:
                message_uid = max(msg_ids, key=int)
                fetch_started = time.perf_counter()
                msg_data = mail.fetch([message_uid], ["RFC822", "INTERNALDATE"])
                fetch_ms = (time.perf_counter() - fetch_started) * 1000
                message = msg_data.get(message_uid, {})
                raw_email = message.get(b"RFC822")
                if raw_email:
                    parse_started = time.perf_counter()
                    extracted = extract_otp_from_email(
                        raw_email,
                        max_age_seconds,
                        after_timestamp=after_timestamp,
                        received_at=message.get(b"INTERNALDATE"),
                    )
                    parse_ms = (time.perf_counter() - parse_started) * 1000
                    if extracted:
                        otp_code, subject, age_sec = extracted
                        elapsed = time.perf_counter() - listener_started
                        print(
                            f"\n  [Email Reader] New OTP detected in '{subject[:45]}'. "
                            f"[since listener armed: {elapsed:.3f}s | search: {search_ms:.1f}ms | "
                            f"fetch: {fetch_ms:.1f}ms | parse: {parse_ms:.1f}ms]"
                        )
                        return otp_code

        except Exception as exc:
            print(f"\n  [Email Reader] IMAP search/fetch failed; reconnecting: {exc}")
            mail = None # Reconnect if the IMAP command fails

        remaining = int(deadline - time.time())
        if not mail:
            time.sleep(0.2)
            continue

        # Keep the polling strategy that was detecting OTPs reliably in this
        # setup. IDLE notifications proved unreliable here and could leave the
        # listener asleep after Gmail had already delivered the message.
        time.sleep(OTP_POLL_INTERVAL)

    print("\n  [Email Reader] No new OTP email received within time limit.")
    return ""


def get_otp_for_page(page_num: int, otp_result_queue=None) -> str:
    """
    Retrieves OTP either automatically from email or falls back
    to user input in the console.
    """
    if otp_result_queue is not None:
        try:
            otp = otp_result_queue.get(timeout=EMAIL_POLL_TIMEOUT)
        except Empty:
            otp = ""
        if otp:
            return otp
        print("  Notice: Email OTP timeout. Falling back to manual input...")
    elif AUTO_READ_EMAIL_OTP and (not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD):
        print("\n  ⚠️ [NOTE: GMAIL CREDENTIALS ARE EMPTY]")
        print("     To read OTP from email automatically, enter your Gmail & App Password")
        print("     Set LG4ALL_GMAIL_ADDRESS and LG4ALL_GMAIL_APP_PASSWORD in the environment.")

    # Manual fallback prompt with audio notification
    notify_user_alert("otp")
    otp = ""
    while not otp or not otp.isdigit():
        try:
            otp = input(f"  >>> Enter OTP for Page {page_num}: ").strip()
            if not otp.isdigit():
                print("  Digits only please. Try again.")
        except (EOFError, KeyboardInterrupt):
            raise
    return otp


def fill_otp_and_handle_submission(driver, otp: str, auto_submit: bool = AUTO_SUBMIT_OTP) -> bool:
    """
    Fills the OTP visibly into the browser modal input field using pure JavaScript
    for zero-latency execution, bypassing native send_keys overhead.
    """
    clean_otp = str(otp).strip()
    if not clean_otp.isdigit() or not 4 <= len(clean_otp) <= 8:
        raise ValueError("OTP must contain 4 to 8 digits.")
    print("  Filling OTP into browser modal ...")

    # 1. Wait for input to exist in DOM (no need for CSS display: block checks, just inject immediately)
    otp_input_ready = """
        const modal = document.getElementById('otpModal');
        return !!(document.getElementById('otpInput') ||
            (modal && modal.querySelector('input[placeholder*=\"OTP\" i], input[type=\"text\"]')) ||
            document.querySelector('input[placeholder*=\"OTP\" i]'));
    """
    WebDriverWait(driver, 2, poll_frequency=FAST_POLL_TICK).until(
        lambda d: d.execute_script(otp_input_ready)
    )

    # Fill and, when enabled, submit in the same browser command to avoid a round-trip gap.
    try:
        fill_started = time.perf_counter()
        driver.execute_script("""
        const modal = document.getElementById('otpModal');
        const inp = document.getElementById('otpInput') ||
            (modal && modal.querySelector('input[placeholder*=\"OTP\" i], input[type=\"text\"]')) ||
            document.querySelector('input[placeholder*=\"OTP\" i]');
        if (!inp) throw new Error('OTP input was not found in the open dialog.');
        inp.value = arguments[0];
        inp.style.border = "3px solid #28a745";
        inp.style.backgroundColor = "#e8f5e9";
        inp.style.fontWeight = "bold";
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        inp.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        inp.focus();
        if (arguments[1]) {
            if (typeof submitOtp === 'function') {
                submitOtp();
            } else {
                const button = document.querySelector('input[value="Submit OTP"]') ||
                    Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"]'))
                        .find(el => (el.innerText || el.value || '').trim() === 'Submit OTP');
                if (!button) throw new Error('Submit OTP button was not found.');
                button.click();
            }
        }
        """, clean_otp, auto_submit)
        fill_ms = (time.perf_counter() - fill_started) * 1000
        if auto_submit:
            print(f"  [OTP Timing] Browser fill+submit call completed in {fill_ms:.1f}ms.")
        else:
            print(f"  [OTP Timing] Browser fill completed in {fill_ms:.1f}ms; OTP submit was not executed.")
            if DIAGNOSTIC_STOP_AFTER_OTP_FILL:
                print("  [Diagnostic] Stopping after OTP fill; no OTP submission will be attempted.")
                raise SystemExit(0)
    except UnexpectedAlertPresentException:
        # The site's success alert may open immediately after submitOtp() runs.
        if not auto_submit:
            raise
        fill_ms = (time.perf_counter() - fill_started) * 1000
        print(f"  [OTP Timing] Fill+submit returned at the server alert in {fill_ms:.1f}ms.")

    def is_submitted(d):
        try:
            alert = d.switch_to.alert
            return True
        except Exception:
            pass
            
        try:
            return d.execute_script("""
                var m = document.getElementById('otpModal');
                if (!m) return true;
                var s = window.getComputedStyle(m);
                return m.style.display === 'none' || s.display === 'none';
            """)
        except Exception:
            return True

    if not auto_submit:
        # TEST MODE: Auto-fill only, no submission
        notify_user_alert("otp")
        print("\n  " + "═" * 60)
        print("  👉 [TEST MODE: AUTO-FILL ONLY]")
        print("     The OTP has been filled into the browser box (highlighted in GREEN).")
        print("     [TEST MODE] Please manually click 'Submit OTP' on the browser.")
        print("     Waiting for you to submit before continuing...")
        print("  " + "═" * 60)
        
        try:
            WebDriverWait(driver, 300, poll_frequency=0.5).until(is_submitted)
            responses = accept_submission_alerts(driver)
        except TimeoutException:
            print("  Timeout waiting for manual submit.")
            return False
            
    else:
        # AUTO-SUBMIT MODE
        print("  Submitting OTP ...")
        responses = accept_submission_alerts(driver)

    if not any("rows saved successfully" in response.casefold() for response in responses):
        print("  [BID STATUS] Rows-saved confirmation was not received; keeping this tab open.")
        return False

    print("  OTP submitted successfully. Managing dual-tab rotation...")
    rotate_to_spare_tab(driver)
    return True


def process_single_page(driver, page_num: int, total_pages: int) -> bool:
    """Processes a single page: scans for target discount, selects, submits, and enters OTP."""
    page_start = time.time()
    print("=" * 65)
    print(f"  PAGE {page_num} of {total_pages}")
    print("=" * 65)

    if page_num > 1:
        ok = navigate_to_page(driver, page_num)
        if not ok:
            print(f"  Failed to navigate to Page {page_num}, skipping.")
            return False

    t_scan = time.time()
    candidate_count = count_matching_rows(driver)
    scan_time = (time.time() - t_scan) * 1000

    if candidate_count == 0:
        print(f"  [Scan: {scan_time:.1f}ms] No {TARGET_DISCOUNT_LABEL} items on Page {page_num}.")
        return False

    # ─────────────────────────────────────────────────────────
    # 🔒 PROTECTED CRITICAL REGION BEGINS
    # ─────────────────────────────────────────────────────────
    lock_browser_against_refresh(driver)
    print("  [🔒 REFRESH LOCKED] Page refresh is strictly FROZEN during selection & OTP entry.")

    try:
        otp_result_queue = start_otp_listener()
        # Selection and the first server submit run together in one browser task.
        t_select_submit = time.time()
        res = select_all_matching_rows(driver, submit_for_otp=True)
        select_submit_ms = (time.time() - t_select_submit) * 1000
        count, serials, items = res["count"], res["serials"], res["items"]
        if count == 0:
            print("  Inventory changed before selection; skipping this page.")
            return False
        if not res["submitted"]:
            raise RuntimeError("Rows were selected, but the OTP request button was not found.")
        print(f"  [Detect: {scan_time:.1f}ms | select+request dispatch: {select_submit_ms:.1f}ms] Selected {count} row(s) [Serials: {', '.join(serials[:5])}{'...' if len(serials) > 5 else ''}]")
        submit_and_wait_for_otp_modal(driver, submit=False)

        # 2. Retrieve OTP (Auto from email or manual fallback)
        otp = get_otp_for_page(page_num, otp_result_queue)

        # 3. Fill OTP & handle submission (manual click in Test Mode)
        success = fill_otp_and_handle_submission(driver, otp, auto_submit=AUTO_SUBMIT_OTP)

        if success:
            threading.Thread(target=background_save_bids, args=(items, page_num), daemon=True).start()
            print(f"  Page {page_num} processed successfully in {time.time() - page_start:.2f}s ✓\n")
            return True
        else:
            print(f"  Page {page_num} finished with status warning.\n")
            return False
            
    except Exception as e:
        print(f"  [ERROR] Execution failed during submission flow: {e}")
        return False
    finally:
        unlock_browser(driver)


def process_all_pages_once(driver) -> int:
    """
    Performs one complete pass through all available grid pages.
    Guarantees zero page refreshes during row selection, submission, and OTP entry.
    """
    total_pages = get_total_pages(driver)
    processed_count = 0

    for page_num in range(1, total_pages + 1):
        did_process = process_single_page(driver, page_num, total_pages)
        if did_process:
            processed_count += 1
            # The listing reshuffles after a successful submission, so we break to restart the scan
            break

        if page_num < total_pages:
            print(f"  Preparing clean state for Page {page_num + 1} ...")
            driver.get(TARGET_URL)
            wait_for_grid_ready(driver)

    return processed_count


def live_monitoring_loop(driver, just_processed=False):
    """
    Continuously monitors the portal ONLY WHEN IDLE.
    Sweeps all available pages (Page 1, Page 2, ...) on every check cycle
    to guarantee no newly uploaded units are missed on secondary pages.
    """
    print("\n" + "=" * 65)
    print("  MULTI-PAGE LIVE INVENTORY MONITOR ACTIVATED")
    print(f"  Watching for {TARGET_DISCOUNT_LABEL} items across ALL pages...")
    print(f"  Idle Refresh Interval: every {POLL_INTERVAL_SECONDS}s  (Press Ctrl+C to stop)")
    print("  🔒 Safe Mode: Refreshes are FROZEN during selection & OTP entry.")
    print("=" * 65)

    check_cycle = 0

    while True:
        try:
            now_str = datetime.now().strftime("%H:%M:%S")

            if not just_processed:
                check_cycle += 1

                # Let an in-page live update wake us immediately; reload if the grid stays static.
                changed = wait_for_inventory_dom_update(driver, POLL_INTERVAL_SECONDS)
                if not changed:
                    try:
                        driver.get(TARGET_URL)
                        wait_for_grid_ready(driver, timeout=12)
                    except (TimeoutException, WebDriverException) as err:
                        print(f"\n  [{now_str}] Temporary network lag: {err}. Retrying...", end="\r", flush=True)
                        continue
            else:
                # We just switched to a fresh background tab, skip the sleep and reload!
                just_processed = False
                check_cycle += 1

            total_pages = get_total_pages(driver)

            # 2. Sweep across all pages to check for target discount items
            found_on_any_page = False
            for page_num in range(1, total_pages + 1):
                if page_num > 1:
                    ok = navigate_to_page(driver, page_num)
                    if not ok:
                        continue

                # Read-only eligibility check; selection and OTP request are combined below.
                t_scan = time.time()
                candidate_count = count_matching_rows(driver)
                scan_ms = (time.time() - t_scan) * 1000

                if candidate_count > 0:
                    # Process this page immediately
                    lock_browser_against_refresh(driver)
                    try:
                        otp_result_queue = start_otp_listener()
                        t_select_submit = time.time()
                        res = select_all_matching_rows(driver, submit_for_otp=True)
                        select_submit_ms = (time.time() - t_select_submit) * 1000
                        if res["count"] == 0:
                            print("  Inventory changed before selection; returning to monitor.")
                            continue
                        if not res["submitted"]:
                            raise RuntimeError("Rows were selected, but the OTP request button was not found.")

                        found_on_any_page = True
                        threading.Thread(target=notify_user_alert, args=("update",), daemon=True).start()
                        print(f"\n🎯 [{now_str}] FOUND {res['count']} UNIT(S) WITH {TARGET_DISCOUNT_LABEL} ON PAGE {page_num}! [Detect: {scan_ms:.1f}ms | select+request dispatch: {select_submit_ms:.1f}ms]")
                        print(f"   Serials: {', '.join(res['serials'][:5])}")
                        print(f"   [⏸️ MONITOR PAUSED] Selection and OTP request sent for Page {page_num}.\n")
                        submit_and_wait_for_otp_modal(driver, submit=False)

                        otp = get_otp_for_page(page_num, otp_result_queue)
                        success = fill_otp_and_handle_submission(driver, otp, auto_submit=AUTO_SUBMIT_OTP)
                        
                        if success:
                            threading.Thread(target=background_save_bids, args=(res['items'], page_num), daemon=True).start()
                        
                        just_processed = success
                    finally:
                        unlock_browser(driver)

                    # Page processed, break out to re-check inventory
                    break

            if not found_on_any_page:
                pages_desc = f"all {total_pages} pages" if total_pages > 1 else "Page 1"
                print(f"  [{now_str}] Check #{check_cycle}: Swept {pages_desc} — 0 items at {TARGET_DISCOUNT_LABEL}. Next check in {POLL_INTERVAL_SECONDS:g}s...", end="\r", flush=True)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] Notice during monitor refresh: {e}. Retrying in {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)


# ═══════════════════════════════════════════════════════════
def main():
    global GLOBAL_IMAP_BASELINE_UID
    print("=" * 65)
    print("  LG4All Bidding Bot (Automatic OTP Submission)")
    print(f"  Target Discount:  {TARGET_DISCOUNT_LABEL}")
    print(f"  Email Auto-OTP:  {'ENABLED' if (AUTO_READ_EMAIL_OTP and GMAIL_ADDRESS and GMAIL_APP_PASSWORD) else 'MANUAL FALLBACK (Fill Gmail settings to enable)'}")
    print(f"  Auto-Submit OTP: {'ENABLED (Automatic submission)' if AUTO_SUBMIT_OTP else 'DISABLED (Test Mode: Auto-fills OTP, you click Submit manually)'}")
    print(f"  Auto-Monitor:    {'ON' if AUTO_MONITOR_UPDATES else 'OFF'}")
    print("=" * 65)

    if AUTO_READ_EMAIL_OTP:
        if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
            print("\n  [Startup blocked] Set LG4ALL_GMAIL_ADDRESS and LG4ALL_GMAIL_APP_PASSWORD before opening Chrome.")
            print("  To use manual OTP entry instead, set AUTO_READ_EMAIL_OTP = False in the configuration.")
            return

        print("  [Init] Connecting to Gmail before opening Chrome...")
        try:
            mail = connect_imap_with_retry(attempts=4)
            GLOBAL_IMAP_BASELINE_UID = latest_mail_uid(mail)
            mode = "IDLE notifications" if mail.has_capability("IDLE") else "short-interval polling"
            print(f"  [Gmail] Connected to INBOX; baseline saved and OTP listener ready ({mode}).")
        except Exception as exc:
            print(f"\n  [Startup blocked] Gmail is not ready, so Chrome was not opened: {exc}")
            return

    driver = init_driver()

    try:
        t0 = time.time()
        print(f"\nOpening two tabs concurrently at {TARGET_URL} ...")
        open_initial_tabs_concurrently(driver, TARGET_URL)
        print(f"  First tab ready in {time.time() - t0:.2f}s; spare continues loading in background.")

        processed_count = process_all_pages_once(driver)

        if AUTO_MONITOR_UPDATES:
            live_monitoring_loop(driver, just_processed=(processed_count > 0))
        else:
            input("\nAll pages done. Press Enter to exit ...")

    except KeyboardInterrupt:
        print("\n\n  Execution stopped by user.")
    except Exception as e:
        print(f"\n  Fatal Error: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit ...")
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        print("  Browser closed.")

if __name__ == "__main__":
    main()
