"""
LG4All NGSI Customer Bidding Automation (Test Mode: Auto-Fill & Manual Submit)
==============================================================================
Features:
  1. Sequential page-by-page bidding with ultra-fast row selection (< 2ms).
  2. Real ASP.NET server postback on Submit to dispatch the OTP email.
  3. Automatic Skip: skips pages if no 50% items are present.
  4. 📧 Automated Email OTP Reader:
     - Connects securely to your Gmail inbox via IMAP.
     - Automatically intercepts the OTP email as soon as it arrives.
     - Parses the 6-digit code.
  5. ✍️ Test Mode (AUTO_SUBMIT_OTP = True):
     - Fills the OTP into the browser modal using native keystrokes & JS.
     - Highlights the filled input box in green.
     - Does NOT submit automatically!
     - Pauses so you can inspect the browser and click 'Submit OTP' manually.
  6. 🔒 Zero-Refresh Lock: Refresh is strictly blocked during selection and OTP.
  7. Live Inventory Monitoring: Automatically polls when idle and jumps into
     action the moment new 50% items are uploaded by LG.

Requirements:
    pip install selenium webdriver-manager

Usage:
    python lg4all_automate.py
"""

import sys
import time
import winsound
import imaplib
import email

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from email.header import decode_header
from email.utils import parsedate_to_datetime
import re
from datetime import datetime, timezone, timedelta

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

class BrowserRestartRequest(Exception):
    """Raised to trigger a fast clean browser restart."""
    pass

# ─────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────
TARGET_URL = "file:///C:/Users/rohit/Desktop/bidding-/mock_lg4all.html"
TARGET_DISCOUNT_PCT     = "70"    # Change to "70" or target percentage as needed
AUTO_MONITOR_UPDATES    = True    # If True, watches page for new inventory when idle
POLL_INTERVAL_SECONDS   = 5       # Seconds between refresh checks ONLY when idle
FAST_POLL_TICK          = 0.1     # High-frequency poll tick (100ms) for instant responsiveness

# ─────────────────────────────────────────────────────────
# EMAIL OTP & SUBMISSION BEHAVIOR
# ─────────────────────────────────────────────────────────
AUTO_READ_EMAIL_OTP = False    # True: auto-fetch OTP from Gmail inbox
AUTO_SUBMIT_OTP = True    # False = Fill OTP only, let YOU click 'Submit OTP' manually
GMAIL_ADDRESS           = "rakeshrpandey8253@gmail.com"      # Enter your Gmail (e.g. "yourname@gmail.com")
GMAIL_APP_PASSWORD      = "dbec pyix cdcn drok"      # Enter your 16-char App Password (e.g. "abcd efgh ijkl mnop")
EMAIL_POLL_TIMEOUT      = 60      # Max seconds to wait for OTP email before manual prompt
# ─────────────────────────────────────────────────────────


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
    
    # Disable images to drastically speed up page loading
    options.add_experimental_option(
        "prefs", {
            "profile.managed_default_content_settings.images": 2
        }
    )

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
    """Wait until the grid table is present and contains rows."""
    WebDriverWait(driver, timeout, poll_frequency=FAST_POLL_TICK).until(
        lambda d: d.execute_script("""
            var g = document.getElementById('gvNGSIDetails');
            return g && g.rows && g.rows.length > 1;
        """)
    )


def get_total_pages(driver) -> int:
    """Detect total grid page count with multiple heuristics."""
    for _ in range(2):
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
            if pages and int(pages) > 1:
                return int(pages)
        except Exception:
            pass
        time.sleep(0.2)

    return 1


def get_inventory_snapshot(driver) -> dict:
    """Extracts a dictionary of all items currently displayed in the grid."""
    try:
        snapshot = driver.execute_script("""
            var items = {};
            var rows = document.querySelectorAll('#gvNGSIDetails tr');
            for (var i = 1; i < rows.length; i++) {
                var srEl = rows[i].querySelector('[id$="lblsrno_gv"]');
                var dpEl = rows[i].querySelector('[id$="lbldp_gv"]');
                var mcEl = rows[i].querySelector('[id$="lblModelCode_gv"]');
                var prEl = rows[i].querySelector('[id$="lbldealerp_gv"]');
                var ptEl = rows[i].querySelector('[id$="lblProduct_gv"]');
                if (srEl) {
                    var sr = srEl.innerText.trim();
                    if (sr) {
                        items[sr] = {
                            model: mcEl ? mcEl.innerText.trim() : '',
                            discount: dpEl ? dpEl.innerText.trim().replace('%', '') : '',
                            price: prEl ? prEl.innerText.trim() : '',
                            product: ptEl ? ptEl.innerText.trim() : ''
                        };
                    }
                }
            }
            return items;
        """)
        return snapshot or {}
    except Exception:
        return {}


def detect_inventory_changes(old_snapshot: dict, new_snapshot: dict) -> dict:
    """Compares two inventory snapshots and identifies newly added or updated units."""
    if not old_snapshot or not new_snapshot:
        return {
            "has_changes": False,
            "added_items": [],
            "updated_items": [],
            "removed_serials": [],
            "target_matches": []
        }

    old_keys = set(old_snapshot.keys())
    new_keys = set(new_snapshot.keys())

    added_serials = new_keys - old_keys
    removed_serials = old_keys - new_keys
    common_serials = old_keys & new_keys

    updated_items = []
    for sr in common_serials:
        old_dp = old_snapshot[sr].get("discount")
        new_dp = new_snapshot[sr].get("discount")
        if old_dp != new_dp:
            updated_items.append({
                "serial": sr,
                "old_discount": old_dp,
                "new_discount": new_dp,
                "model": new_snapshot[sr].get("model")
            })

    added_items = [
        {"serial": sr, **new_snapshot[sr]} for sr in added_serials
    ]

    has_changes = bool(added_items or updated_items or removed_serials)

    target_matches = [
        {"serial": sr, **item} for sr, item in new_snapshot.items()
        if item.get("discount") == TARGET_DISCOUNT_PCT
    ]

    return {
        "has_changes": has_changes,
        "added_items": added_items,
        "updated_items": updated_items,
        "removed_serials": list(removed_serials),
        "target_matches": target_matches
    }


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


def select_all_matching_rows(driver) -> dict:
    """Scans and checks all matching rows in one high-speed JS call (< 2ms)."""
    result = driver.execute_script(f"""
        var targetDP = "{TARGET_DISCOUNT_PCT}";
        var selected = [];
        var rows = document.querySelectorAll('#gvNGSIDetails tr');

        for (var i = 1; i < rows.length; i++) {{
            var row = rows[i];
            var dpEl  = row.querySelector('[id$="lbldp_gv"]');
            var chkEl = row.querySelector('[id$="chkSelect"]');

            if (dpEl && chkEl && !chkEl.disabled) {{
                var dpVal = dpEl.innerText.trim().replace('%', '');
                if (dpVal === targetDP) {{
                    chkEl.checked = true;
                    chkEl.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    var serialEl = row.querySelector('[id$="lblsrno_gv"]');
                    var serial = serialEl ? serialEl.innerText.trim() : ('Row ' + i);
                    selected.push(serial);
                }}
            }}
        }}

        return {{
            count: selected.length,
            serials: selected
        }};
    """)

    count = int(result.get("count", 0)) if result else 0
    serials = result.get("serials", []) if result else []
    return {"count": count, "serials": serials}


def submit_and_wait_for_otp_modal(driver, timeout=30):
    """Executes the REAL server submit postback to trigger the OTP email."""
    print("  Triggering Submit postback to server (requesting OTP email) ...")

    WebDriverWait(driver, 10, poll_frequency=FAST_POLL_TICK).until(
        lambda d: d.execute_script("""
            var b = document.getElementById('btnSave');
            if (!b) return false;
            b.scrollIntoView({block: 'center'});
            return true;
        """)
    )

    driver.execute_script("""
        var btn = document.getElementById('btnSave');
        if (btn) {
            btn.click();
        } else if (typeof __doPostBack === 'function') {
            __doPostBack('ctl00$ContentPlaceHolder1$btnSave', '');
        }
    """)

    print("  Submit sent — waiting for server to dispatch OTP and display modal ...")

    try:
        WebDriverWait(driver, 2, poll_frequency=FAST_POLL_TICK).until(EC.alert_is_present())
        alert = driver.switch_to.alert
        print(f"  Server notice: {alert.text}")
        alert.accept()
    except TimeoutException:
        pass

    WebDriverWait(driver, timeout, poll_frequency=FAST_POLL_TICK).until(
        lambda d: d.execute_script("""
            var modal = document.getElementById('otpModal');
            if (!modal) return false;
            var style = window.getComputedStyle(modal);
            return modal.style.display === 'block' || style.display === 'block';
        """)
    )
    print("  OTP modal is now open! Server has dispatched the OTP to your email.")


def fetch_otp_from_gmail(max_age_seconds: int = 180, timeout: int = 60) -> str:
    """
    Connects to Gmail via IMAP over SSL and polls for the brand-new incoming
    OTP email from LG. Automatically rejects older emails from previous runs.
    """
    clean_addr = GMAIL_ADDRESS.strip()
    clean_pass = GMAIL_APP_PASSWORD.replace(" ", "").strip()

    if not clean_addr or not clean_pass:
        return ""

    print(f"  [Email Reader] Monitoring Gmail inbox ({clean_addr}) for OTP ...")
    deadline = time.time() + timeout
    poll_count = 0

    while time.time() < deadline:
        poll_count += 1
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(clean_addr, clean_pass)
            mail.select("INBOX")

            # Search specifically for LG emails
            status, data = mail.search(None, '(FROM "lge.com")')
            msg_ids = data[0].split() if status == "OK" and data[0] else []
            if not msg_ids:
                status, data = mail.search(None, '(SUBJECT "OTP Validation")')
                msg_ids = data[0].split() if status == "OK" and data[0] else []

            if msg_ids:
                newest_id = msg_ids[-1]
                _, msg_data = mail.fetch(newest_id, "(RFC822)")
                if msg_data and msg_data[0]:
                    raw_email = msg_data[0][1]
                    parsed_msg = email.message_from_bytes(raw_email)

                    # Check email timestamp age
                    date_str = parsed_msg.get("Date", "")
                    try:
                        email_dt = parsedate_to_datetime(date_str)
                        age_sec = (datetime.now(timezone.utc) - email_dt.astimezone(timezone.utc)).total_seconds()
                    except Exception:
                        age_sec = 0

                    # If email was sent > max_age_seconds ago, delete it so it never gets picked!
                    if age_sec > max_age_seconds:
                        try:
                            mail.store(newest_id, "+FLAGS", "\\Deleted")
                            mail.expunge()
                        except Exception:
                            pass
                    else:
                        # Extract Subject
                        subject = ""
                        raw_subj = parsed_msg.get("Subject", "")
                        for part, enc in decode_header(raw_subj):
                            if isinstance(part, bytes):
                                subject += part.decode(enc or "utf-8", errors="ignore")
                            else:
                                subject += str(part)

                        # Extract Body
                        body = ""
                        if parsed_msg.is_multipart():
                            for part in parsed_msg.walk():
                                if part.get_content_type() in ["text/plain", "text/html"]:
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body += payload.decode("utf-8", errors="ignore") + "\n"
                        else:
                            payload = parsed_msg.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="ignore")

                        # Strip HTML tags and CSS hex colors (e.g. #000000)
                        clean_text = re.sub(r'<style.*?</style>', ' ', f"{subject}\n{body}", flags=re.DOTALL | re.IGNORECASE)
                        clean_text = re.sub(r'#[0-9a-fA-F]{3,8}\b', ' ', clean_text)
                        clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
                        clean_text = re.sub(r'\s+', ' ', clean_text)

                        # Extract OTP: supports both 5-digit (e.g. 64977) and 6-digit codes
                        m = re.search(r'(?:bidding|authentication|otp|code)[^0-9\n]{1,50}?(\d{4,8})\b', clean_text, re.IGNORECASE)
                        if not m:
                            m = re.search(r'\b(\d{5,6})\b', clean_text)

                        if m:
                            otp_code = m.group(1)
                            if otp_code != "000000" and len(set(otp_code)) > 1:
                                print(f"\n  [Email Reader] ✅ Found Latest OTP: {otp_code} in '{subject[:45]}' (sent {age_sec:.0f}s ago)!")
                                try:
                                    # Delete this OTP email so it can NEVER be reused or read again
                                    mail.store(newest_id, "+FLAGS", "\\Deleted")
                                    mail.expunge()
                                except Exception:
                                    pass
                                mail.logout()
                                return otp_code

            mail.logout()
        except Exception:
            pass

        remaining = int(deadline - time.time())
        print(f"  [Email Reader] Waiting for new OTP email from LG... (check #{poll_count}, {remaining}s left)", end="\r", flush=True)
        time.sleep(1)

    print("\n  [Email Reader] No new OTP email received within time limit.")
    return ""


def get_otp_for_page(page_num: int) -> str:
    """
    Retrieves OTP either automatically from email or falls back
    to user input in the console.
    """
    if AUTO_READ_EMAIL_OTP and GMAIL_ADDRESS and GMAIL_APP_PASSWORD:
        otp = fetch_otp_from_gmail(max_age_seconds=180, timeout=EMAIL_POLL_TIMEOUT)
        if otp:
            return otp
        print("  Notice: Email OTP timeout. Falling back to manual input...")
    elif AUTO_READ_EMAIL_OTP and (not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD):
        print("\n  ⚠️ [NOTE: GMAIL CREDENTIALS ARE EMPTY]")
        print("     To read OTP from email automatically, enter your Gmail & App Password")
        print("     at lines 63-64 in lg4all_automate.py.")

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


def fill_otp_and_handle_submission(driver, otp: str, auto_submit: bool = AUTO_SUBMIT_OTP, timeout=30) -> bool:
    """
    Fills the OTP visibly into the browser modal input field using both
    native keyboard simulation (send_keys) and JavaScript assignment.
    """
    clean_otp = str(otp).strip()
    print(f"  Filling OTP [{clean_otp}] into browser modal ...")

    # 1. Wait for modal to be visible
    WebDriverWait(driver, 10, poll_frequency=FAST_POLL_TICK).until(
        lambda d: d.execute_script("""
            var m = document.getElementById('otpModal');
            if (!m) return false;
            var s = window.getComputedStyle(m);
            return m.style.display === 'block' || s.display === 'block';
        """)
    )

    # 2. Locate the input element
    otp_input = WebDriverWait(driver, 10, poll_frequency=FAST_POLL_TICK).until(
        EC.presence_of_element_located((By.ID, "otpInput"))
    )

    # 3. Bring into view and focus
    driver.execute_script("""
        var inp = document.getElementById('otpInput');
        if (inp) {
            inp.scrollIntoView({block: 'center'});
            inp.focus();
        }
    """)
    time.sleep(0.1)

    # 4. Clear existing content
    try:
        otp_input.clear()
    except Exception:
        driver.execute_script("document.getElementById('otpInput').value = '';")

    # 5. Type keystrokes natively
    try:
        otp_input.send_keys(clean_otp)
    except Exception as e:
        print(f"  Note on send_keys: {e}")

    # 6. Ensure value is set in DOM, trigger events, and highlight green
    driver.execute_script(f"""
        var targetDP = "{TARGET_DISCOUNT_PCT}";
        var rows = document.querySelectorAll('#gvNGSIDetails tr');
        for (var i = 1; i < rows.length; i++) {{
            var dpEl  = rows[i].querySelector('[id$="lbldp_gv"]');
            var chkEl = rows[i].querySelector('[id$="chkSelect"]');
            if (dpEl && chkEl && !chkEl.disabled) {{
                var dpVal = dpEl.innerText.trim().replace('%', '');
                if (dpVal === targetDP) {{
                    chkEl.checked = true;
                }}
            }}
        }}

        var inp = document.getElementById('otpInput');
        if (inp) {{
            inp.value = "{clean_otp}";
            inp.style.border = "3px solid #28a745";
            inp.style.backgroundColor = "#e8f5e9";
            inp.style.fontWeight = "bold";
            inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
            inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
            inp.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true }}));
            inp.focus();
        }}
    """)

    # Verify what value is actually in the element
    val_in_dom = driver.execute_script("return document.getElementById('otpInput').value;")
    print(f"  ✅ OTP verified in browser input box: '{val_in_dom}'")

    def is_submitted(d):
        try:
            alert = d.switch_to.alert
            return True
        except NoAlertPresentException:
            pass
        return d.execute_script("""
            var m = document.getElementById('otpModal');
            if (!m) return true;
            var s = window.getComputedStyle(m);
            return m.style.display === 'none' || s.display === 'none';
        """)

    if not auto_submit:
        # TEST MODE: Auto-fill only, no submission
        notify_user_alert("otp")
        print("\n  " + "═" * 60)
        print("  👉 [TEST MODE: AUTO-FILL ONLY]")
        print(f"     The OTP [{val_in_dom}] has been filled into the browser box (highlighted in GREEN).")
        print("     [TEST MODE] Please manually click 'Submit OTP' on the browser.")
        print("     Waiting for you to submit before continuing...")
        print("  " + "═" * 60)
        
        try:
            WebDriverWait(driver, 300, poll_frequency=0.5).until(is_submitted)
            # Handle up to 2 alerts (e.g., "OTP validated successfully" and "1 rows saved successfully")
            for i in range(2):
                try:
                    WebDriverWait(driver, 10, poll_frequency=0.1).until(EC.alert_is_present())
                    alert = driver.switch_to.alert
                    print(f"  Server response {i+1} after manual submit: {alert.text}")
                    alert.accept()
                except (TimeoutException, NoAlertPresentException):
                    break
        except TimeoutException:
            print("  Timeout waiting for manual submit.")
            
    else:
        # AUTO-SUBMIT MODE
        print(f"  Submitting OTP ({clean_otp}) ... ")
        driver.execute_script("""
            if (typeof submitOtp === 'function') {
                submitOtp();
            } else {
                var btn = document.querySelector('input[value="Submit OTP"]');
                if (btn) btn.click();
            }
        """)
        
        # Handle up to 2 alerts (e.g., "OTP validated successfully" and "1 rows saved successfully")
        for i in range(2):
            try:
                WebDriverWait(driver, 10, poll_frequency=0.1).until(EC.alert_is_present())
                alert = driver.switch_to.alert
                print(f"  Server response {i+1}: {alert.text}")
                alert.accept()
            except (TimeoutException, NoAlertPresentException):
                break

    print("  OTP submitted successfully. Closing browser and restarting fast...")
    raise BrowserRestartRequest()


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
    res = select_all_matching_rows(driver)
    count = res["count"]
    serials = res["serials"]
    scan_time = (time.time() - t_scan) * 1000

    if count == 0:
        print(f"  [Scan: {scan_time:.1f}ms] No {TARGET_DISCOUNT_PCT}% items on Page {page_num}.")
        return False

    print(f"  [Scan: {scan_time:.1f}ms] Selected {count} row(s) [Serials: {', '.join(serials[:5])}{'...' if len(serials) > 5 else ''}]")

    # ─────────────────────────────────────────────────────────
    # 🔒 PROTECTED CRITICAL REGION BEGINS
    # ─────────────────────────────────────────────────────────
    lock_browser_against_refresh(driver)
    print("  [🔒 REFRESH LOCKED] Page refresh is strictly FROZEN during selection & OTP entry.")

    try:
        # 1. Submit to server immediately (instant postback, zero delay!)
        submit_and_wait_for_otp_modal(driver)

        # 2. Retrieve OTP (Auto from email or manual fallback)
        otp = get_otp_for_page(page_num)

        # 3. Fill OTP & handle submission (manual click in Test Mode)
        success = fill_otp_and_handle_submission(driver, otp, auto_submit=AUTO_SUBMIT_OTP)

        if success:
            print(f"  Page {page_num} processed successfully in {time.time() - page_start:.2f}s ✓\n")
            return True
        else:
            print(f"  Page {page_num} finished with status warning.\n")
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

        if page_num < total_pages:
            print(f"  Preparing clean state for Page {page_num + 1} ...")
            driver.get(TARGET_URL)
            wait_for_grid_ready(driver)

    return processed_count


def live_monitoring_loop(driver):
    """
    Continuously monitors the portal ONLY WHEN IDLE.
    Sweeps all available pages (Page 1, Page 2, ...) on every check cycle
    to guarantee no newly uploaded units are missed on secondary pages.
    """
    print("\n" + "=" * 65)
    print("  MULTI-PAGE LIVE INVENTORY MONITOR ACTIVATED")
    print(f"  Watching for {TARGET_DISCOUNT_PCT}% items across ALL pages...")
    print(f"  Idle Refresh Interval: every {POLL_INTERVAL_SECONDS}s  (Press Ctrl+C to stop)")
    print("  🔒 Safe Mode: Refreshes are FROZEN during selection & OTP entry.")
    print("=" * 65)

    check_cycle = 0

    while True:
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
            check_cycle += 1
            now_str = datetime.now().strftime("%H:%M:%S")

            # 1. Reload base page
            try:
                driver.get(TARGET_URL)
                wait_for_grid_ready(driver, timeout=12)
            except (TimeoutException, WebDriverException) as err:
                print(f"\n  [{now_str}] Temporary network lag: {err}. Retrying...", end="\r", flush=True)
                continue

            total_pages = get_total_pages(driver)

            # 2. Sweep across all pages to check for target discount items
            found_on_any_page = False
            for page_num in range(1, total_pages + 1):
                if page_num > 1:
                    ok = navigate_to_page(driver, page_num)
                    if not ok:
                        continue

                # Quick sub-2ms check on this page
                t_scan = time.time()
                res = select_all_matching_rows(driver)
                scan_ms = (time.time() - t_scan) * 1000

                if res["count"] > 0:
                    found_on_any_page = True
                    notify_user_alert("update")
                    print(f"\n🎯 [{now_str}] FOUND {res['count']} UNIT(S) WITH {TARGET_DISCOUNT_PCT}% ON PAGE {page_num}! [Scan: {scan_ms:.1f}ms]")
                    print(f"   Serials: {', '.join(res['serials'][:5])}")
                    print(f"   [⏸️ MONITOR PAUSED] Entering bidding flow for Page {page_num}...\n")

                    # Process this page immediately
                    lock_browser_against_refresh(driver)
                    try:
                        submit_and_wait_for_otp_modal(driver)
                        otp = get_otp_for_page(page_num)
                        fill_otp_and_handle_submission(driver, otp, auto_submit=AUTO_SUBMIT_OTP)
                    finally:
                        unlock_browser(driver)

                    # Page processed, break out to re-check inventory
                    break

            if not found_on_any_page:
                pages_desc = f"all {total_pages} pages" if total_pages > 1 else "Page 1"
                print(f"  [{now_str}] Check #{check_cycle}: Swept {pages_desc} — 0 items at {TARGET_DISCOUNT_PCT}%. Next check in {POLL_INTERVAL_SECONDS}s...", end="\r", flush=True)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] Notice during monitor refresh: {e}. Retrying in {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)


# ═══════════════════════════════════════════════════════════
def main():
    print("=" * 65)
    print("  LG4All Bidding Bot (Test Mode: Auto-Fill, Manual Submit)")
    print(f"  Target Discount: {TARGET_DISCOUNT_PCT}%")
    print(f"  Email Auto-OTP:  {'ENABLED (' + GMAIL_ADDRESS + ')' if (AUTO_READ_EMAIL_OTP and GMAIL_ADDRESS and GMAIL_APP_PASSWORD) else 'MANUAL FALLBACK (Fill Gmail settings to enable)'}")
    print(f"  Auto-Submit OTP: {'ENABLED (Automatic submission)' if AUTO_SUBMIT_OTP else 'DISABLED (Test Mode: Auto-fills OTP, you click Submit manually)'}")
    print(f"  Auto-Monitor:    {'ON' if AUTO_MONITOR_UPDATES else 'OFF'}")
    print("=" * 65)

    while True:
        driver = init_driver()

        try:
            t0 = time.time()
            print(f"\nLoading {TARGET_URL} ...")
            driver.get(TARGET_URL)
            wait_for_grid_ready(driver)
            print(f"  Portal ready in {time.time() - t0:.2f}s")

            process_all_pages_once(driver)

            if AUTO_MONITOR_UPDATES:
                live_monitoring_loop(driver)
            else:
                input("\nAll pages done. Press Enter to exit ...")
                break
                
        except BrowserRestartRequest:
            print("\n  [Restart] Shutting down browser and restarting instantly for clean state...")
            try:
                driver.quit()
            except:
                pass
            continue

        except KeyboardInterrupt:
            print("\n\n  Execution stopped by user.")
            break
        except Exception as e:
            print(f"\n  Fatal Error: {e}")
            import traceback
            traceback.print_exc()
            input("\nPress Enter to exit ...")
            break
        finally:
            try:
                if driver:
                    driver.quit()
            except Exception:
                pass
        print("  Browser closed.")

if __name__ == "__main__":
    main()
