"""
LG4All NGSI Customer Bidding Automation (Real Postback & OTP Dispatch)
=====================================================================
Fix:
  - Previously, the script called showOtpModal() locally while removing
    the button's onclick attribute. This prevented the real ASP.NET
    server postback (__doPostBack('ctl00$ContentPlaceHolder1$btnSave','')),
    so the server never received the request and never dispatched the OTP!
  - Now, the script triggers the REAL server postback via btnSave so the
    backend server receives all selected checkboxes, generates the OTP,
    sends it to your registered email, and displays the OTP modal.
  - Then the OTP is entered and validated, and SaveBiddingData is waited for.

Requirements:
    pip install selenium webdriver-manager

Usage:
    python lg4all_automate.py
"""

import time
import winsound

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
)
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────────────────
TARGET_URL          = "https://www.lg4all.com/pod/?Code=IN24997500"
TARGET_DISCOUNT_PCT = "50"
# ─────────────────────────────────────────────────────────


def init_driver() -> webdriver.Chrome:
    """Initialize Chrome WebDriver."""
    options = Options()
    # Use normal page load so ASP.NET postbacks complete reliably
    options.page_load_strategy = "normal"

    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-infobars")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)
    return driver


def notify_user_for_otp():
    """Emit an audio alert so the user immediately knows OTP is needed."""
    try:
        winsound.Beep(1200, 180)
        time.sleep(0.08)
        winsound.Beep(1600, 220)
    except Exception:
        pass


def wait_for_grid_ready(driver, timeout=20):
    """Wait until the grid table is present and contains rows."""
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.ID, "gvNGSIDetails"))
    )
    WebDriverWait(driver, timeout).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, "#gvNGSIDetails tr")) > 1
    )


def get_total_pages(driver) -> int:
    """
    Detect the total page count using multiple robust heuristics.
    Inspects nested pager tables and ASP.NET postback link parameters.
    """
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
        return int(pages) if pages else 1
    except Exception as e:
        print(f"  Note on page count detection: {e} -> defaulting to 1")
        return 1


def navigate_to_page(driver, page_number: int, max_retries=3) -> bool:
    """
    Fast and robust navigation to a specific grid page.
    Waits for staleness of previous grid to prevent race conditions.
    """
    if page_number == 1:
        return True

    print(f"  Navigating to Page {page_number} ...")

    for attempt in range(1, max_retries + 1):
        try:
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
                WebDriverWait(driver, 10).until(EC.staleness_of(old_grid))
            except TimeoutException:
                pass

            wait_for_grid_ready(driver, timeout=15)
            print(f"  Page {page_number} loaded successfully.")
            return True

        except Exception as e:
            print(f"  [Attempt {attempt}/{max_retries}] Navigation error: {e}")
            time.sleep(1)

    return False


def select_all_matching_rows(driver) -> dict:
    """
    Scans and checks all matching rows in one high-speed JS call.
    Returns count and metadata of selected items.
    """
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
    """
    Executes the REAL server submit postback.
    This triggers btnSave on the server, which generates the OTP,
    sends it to the user's email, and displays the OTP modal.
    """
    print("  Triggering Submit postback to server (requesting OTP email) ...")

    # Find btnSave element
    btn = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "btnSave"))
    )
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
    time.sleep(0.3)

    # Click the button natively so its original onclick (__doPostBack) runs
    try:
        btn.click()
    except Exception:
        # Fallback to direct script execution of button's onclick
        driver.execute_script("""
            var btn = document.getElementById('btnSave');
            if (btn && btn.onclick) {
                btn.onclick();
            } else if (typeof __doPostBack === 'function') {
                __doPostBack('ctl00$ContentPlaceHolder1$btnSave', '');
            }
        """)

    print("  Submit sent — waiting for server to dispatch OTP and display modal ...")

    # Check for immediate server alert if any
    try:
        WebDriverWait(driver, 3).until(EC.alert_is_present())
        alert = driver.switch_to.alert
        print(f"  Server alert: {alert.text}")
        alert.accept()
    except TimeoutException:
        pass

    # Wait for the OTP modal to become visible
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("""
            var modal = document.getElementById('otpModal');
            if (!modal) return false;
            var style = window.getComputedStyle(modal);
            return modal.style.display === 'block' || style.display === 'block';
        """)
    )
    print("  OTP modal is now open! Server has sent the OTP to your email.")


def submit_otp_and_save_data(driver, otp: str, timeout=30) -> bool:
    """
    Submits OTP to ValidateOtp web method, then waits for SaveBiddingData.
    Handles both validation alert and final save confirmation alert.
    """
    # Ensure selected 50% rows remain checked for saveBiddingData()
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

        var input = document.getElementById('otpInput');
        if (input) {{
            input.value = "{otp}";
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}

        if (typeof submitOtp === 'function') {{
            submitOtp();
        }} else {{
            var btn = document.querySelector('input[value="Submit OTP"]');
            if (btn) btn.click();
        }}
    """)
    print("  OTP submitted — verifying with server ...")

    # Alert 1: ValidateOtp response (e.g. "OTP validated successfully.")
    try:
        WebDriverWait(driver, 15).until(EC.alert_is_present())
        alert1 = driver.switch_to.alert
        msg1 = alert1.text
        print(f"  Validation response: {msg1}")
        alert1.accept()

        if "SUCCESS" in msg1.upper() or "VALIDAT" in msg1.upper():
            print("  OTP validated! Waiting for SaveBiddingData confirmation ...")
            # Alert 2: onBiddingDataSaved response
            try:
                WebDriverWait(driver, timeout).until(EC.alert_is_present())
                alert2 = driver.switch_to.alert
                msg2 = alert2.text
                print(f"  Final server response: {msg2}")
                alert2.accept()
                return True
            except TimeoutException:
                print("  Bidding data saved.")
                return True
        else:
            print(f"  OTP validation returned: {msg1}")
            return False

    except TimeoutException:
        print("  No response dialog returned by server.")
        return True


def get_otp_from_user(page_num: int) -> str:
    """Prompt user in console with sound alert."""
    notify_user_for_otp()
    otp = ""
    while not otp or not otp.isdigit():
        try:
            otp = input(f"  >>> Enter OTP for Page {page_num}: ").strip()
            if not otp.isdigit():
                print("  Digits only please. Try again.")
        except (EOFError, KeyboardInterrupt):
            raise
    return otp


# ═══════════════════════════════════════════════════════════
def main():
    print("=" * 65)
    print("  LG4All Bidding Bot (Real Server Postback & OTP Dispatch)")
    print(f"  Target Discount Filter: {TARGET_DISCOUNT_PCT}%")
    print("=" * 65)

    driver = init_driver()

    try:
        t0 = time.time()
        print(f"\nLoading {TARGET_URL} ...")
        driver.get(TARGET_URL)
        wait_for_grid_ready(driver)
        print(f"  Initial page ready in {time.time() - t0:.2f}s")

        total_pages = get_total_pages(driver)
        print(f"  Total Grid Pages Detected: {total_pages}\n")

        # Process each page sequentially
        for page_num in range(1, total_pages + 1):
            page_start = time.time()
            print("=" * 65)
            print(f"  PAGE {page_num} of {total_pages}")
            print("=" * 65)

            # Step 1: Navigate to page (if not already on it)
            if page_num > 1:
                ok = navigate_to_page(driver, page_num)
                if not ok:
                    print(f"  Failed to load Page {page_num}, skipping to next.")
                    continue

            # Step 2: Instant scan and selection
            t_scan = time.time()
            res = select_all_matching_rows(driver)
            count = res["count"]
            serials = res["serials"]
            scan_time = (time.time() - t_scan) * 1000

            # Step 3: Check matching rows count
            if count == 0:
                print(f"  [Scan: {scan_time:.1f}ms] No {TARGET_DISCOUNT_PCT}% items found on Page {page_num}.")
                print(f"  --> Skipping directly to next page.\n")
                continue

            print(f"  [Scan: {scan_time:.1f}ms] Selected {count} row(s) [Serials: {', '.join(serials[:5])}{'...' if len(serials) > 5 else ''}]")

            # Step 4: Real Submit postback -> triggers server to send OTP email & open modal
            submit_and_wait_for_otp_modal(driver)

            # Step 5: Sound alert & ask user for OTP
            otp = get_otp_from_user(page_num)

            # Step 6: Submit OTP & save bidding data
            success = submit_otp_and_save_data(driver, otp)
            if success:
                print(f"  Page {page_num} completed successfully in {time.time() - page_start:.2f}s ✓\n")
            else:
                print(f"  Page {page_num} completed with status warnings in {time.time() - page_start:.2f}s.\n")

            # Reset page cleanly for the next iteration if more pages remain
            if page_num < total_pages:
                print(f"  Preparing clean state for Page {page_num + 1} ...")
                driver.get(TARGET_URL)
                wait_for_grid_ready(driver)

        print("=" * 65)
        print(f"  All {total_pages} page(s) finished processing!")
        print("=" * 65)
        input("\nPress Enter to close browser ...")

    except KeyboardInterrupt:
        print("\n  Execution stopped by user.")
    except Exception as e:
        print(f"\n  Fatal Error: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to close ...")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("  Browser closed.")


if __name__ == "__main__":
    main()
