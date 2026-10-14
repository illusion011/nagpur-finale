"""
LG4All Bidding Bot - Desktop Software Application (GUI)
======================================================
A modern, dark-themed Desktop Application for configuring and running the
LG4All Customer Portal Bidding Bot.

Features:
  - Custom Discount Target Selection (50%, 70%, custom %).
  - Auto-Submit OTP Toggle (ON = Full Auto, OFF = Test Mode / Manual Submit).
  - Gmail Credentials & App Password Management (Saved locally to config.json).
  - Live Inventory Auto-Monitor toggle & poll interval.
  - Real-Time Live Log Console inside the software window.
  - One-click START / STOP controls with thread safety.
"""

import sys
import os
import json
import time
import queue
import threading
import winsound
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import re
from datetime import datetime, timezone

import tkinter as tk
from tkinter import ttk, messagebox, font

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
# CONFIG FILE PATH
# ─────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "target_url": "https://www.lg4all.com/pod/?Code=IN24997500",
    "target_discount": "50",
    "auto_submit_otp": False,
    "gmail_address": "rakeshrpandey8253@gmail.com",
    "gmail_app_password": "dbec pyix cdcn drok",
    "auto_read_email_otp": True,
    "auto_monitor_updates": True,
    "poll_interval_seconds": 5,
    "email_poll_timeout": 60
}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                merged = DEFAULT_CONFIG.copy()
                merged.update(cfg)
                return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(config_data: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")


# ─────────────────────────────────────────────────────────
# QUEUE LOG STREAMER FOR GUI TERMINAL
# ─────────────────────────────────────────────────────────
class QueueLogger:
    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue

    def write(self, msg):
        if msg:
            self.log_queue.put(msg)

    def flush(self):
        pass


# ─────────────────────────────────────────────────────────
# BIDDING ENGINE WORKER CLASS
# ─────────────────────────────────────────────────────────
class BiddingEngine:
    def __init__(self, config: dict, log_queue: queue.Queue, stop_event: threading.Event):
        self.config = config
        self.log_queue = log_queue
        self.stop_event = stop_event
        self.driver = None

    def log(self, text: str):
        self.log_queue.put(text + "\n")

    def init_driver(self) -> webdriver.Chrome:
        self.log("[System] Launching Chrome WebDriver (Eager Mode, No Images)...")
        options = Options()
        options.page_load_strategy = "eager"

        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-first-run")
        options.add_argument("--log-level=3")

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

    def safe_clear_alert(self) -> str:
        try:
            alert = self.driver.switch_to.alert
            text = alert.text
            alert.accept()
            return text
        except Exception:
            return ""

    def lock_browser_against_refresh(self):
        try:
            self.driver.execute_script("""
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

    def unlock_browser(self):
        try:
            self.driver.execute_script("window.__bidding_locked = false;")
        except Exception:
            pass

    def notify_user_alert(self, alert_type="otp"):
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

    def wait_for_grid_ready(self, timeout=20):
        WebDriverWait(self.driver, timeout, poll_frequency=0.1).until(
            lambda d: d.execute_script("""
                var g = document.getElementById('gvNGSIDetails');
                return g && g.rows && g.rows.length > 1;
            """)
        )

    def get_total_pages(self) -> int:
        for _ in range(2):
            try:
                pages = self.driver.execute_script("""
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

    def navigate_to_page(self, page_number: int, max_retries=3) -> bool:
        if page_number == 1:
            return True

        self.log(f"  [Nav] Navigating to Page {page_number}...")

        for attempt in range(1, max_retries + 1):
            if self.stop_event.is_set():
                return False
            try:
                self.safe_clear_alert()
                old_grid = self.driver.find_element(By.ID, "gvNGSIDetails")

                dispatched = self.driver.execute_script(f"""
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

                try:
                    WebDriverWait(self.driver, 10, poll_frequency=0.1).until(EC.staleness_of(old_grid))
                except TimeoutException:
                    pass

                self.wait_for_grid_ready(timeout=15)
                self.log(f"  [Nav] Page {page_number} loaded.")
                return True

            except Exception as e:
                self.log(f"  [Nav Attempt {attempt}] Notice: {e}")
                time.sleep(0.5)

        return False

    def select_all_matching_rows(self) -> dict:
        target_dp = str(self.config.get("target_discount", "50")).strip()
        result = self.driver.execute_script(f"""
            var targetDP = "{target_dp}";
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

    def submit_and_wait_for_otp_modal(self, timeout=30):
        self.log("  [Postback] Sending Submit request to server for OTP email...")
        WebDriverWait(self.driver, 10, poll_frequency=0.1).until(
            lambda d: d.execute_script("""
                var b = document.getElementById('btnSave');
                if (!b) return false;
                b.scrollIntoView({block: 'center'});
                return true;
            """)
        )

        self.driver.execute_script("""
            var btn = document.getElementById('btnSave');
            if (btn) {
                btn.click();
            } else if (typeof __doPostBack === 'function') {
                __doPostBack('ctl00$ContentPlaceHolder1$btnSave', '');
            }
        """)

        try:
            WebDriverWait(self.driver, 2, poll_frequency=0.1).until(EC.alert_is_present())
            alert = self.driver.switch_to.alert
            self.log(f"  [Server Alert] {alert.text}")
            alert.accept()
        except TimeoutException:
            pass

        WebDriverWait(self.driver, timeout, poll_frequency=0.1).until(
            lambda d: d.execute_script("""
                var modal = document.getElementById('otpModal');
                if (!modal) return false;
                var style = window.getComputedStyle(modal);
                return modal.style.display === 'block' || style.display === 'block';
            """)
        )
        self.log("  [OTP Modal] Modal opened. OTP email dispatched by LG server.")

    def fetch_otp_from_gmail(self, max_age_seconds: int = 180, timeout: int = 60) -> str:
        clean_addr = self.config.get("gmail_address", "").strip()
        clean_pass = self.config.get("gmail_app_password", "").replace(" ", "").strip()

        if not clean_addr or not clean_pass:
            self.log("  [Gmail] Address or App Password missing.")
            return ""

        self.log(f"  [Gmail Reader] Polling inbox ({clean_addr}) for OTP...")
        deadline = time.time() + timeout
        poll_count = 0

        while time.time() < deadline:
            if self.stop_event.is_set():
                return ""
            poll_count += 1
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(clean_addr, clean_pass)
                mail.select("INBOX")

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

                        date_str = parsed_msg.get("Date", "")
                        try:
                            email_dt = parsedate_to_datetime(date_str)
                            age_sec = (datetime.now(timezone.utc) - email_dt.astimezone(timezone.utc)).total_seconds()
                        except Exception:
                            age_sec = 0

                        if age_sec > max_age_seconds:
                            try:
                                mail.store(newest_id, "+FLAGS", "\\Deleted")
                                mail.expunge()
                            except Exception:
                                pass
                        else:
                            subject = ""
                            raw_subj = parsed_msg.get("Subject", "")
                            for part, enc in decode_header(raw_subj):
                                if isinstance(part, bytes):
                                    subject += part.decode(enc or "utf-8", errors="ignore")
                                else:
                                    subject += str(part)

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

                            clean_text = re.sub(r'<style.*?</style>', ' ', f"{subject}\n{body}", flags=re.DOTALL | re.IGNORECASE)
                            clean_text = re.sub(r'#[0-9a-fA-F]{3,8}\b', ' ', clean_text)
                            clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
                            clean_text = re.sub(r'\s+', ' ', clean_text)

                            m = re.search(r'(?:bidding|authentication|otp|code)[^0-9\n]{1,50}?(\d{4,8})\b', clean_text, re.IGNORECASE)
                            if not m:
                                m = re.search(r'\b(\d{5,6})\b', clean_text)

                            if m:
                                otp_code = m.group(1)
                                if otp_code != "000000" and len(set(otp_code)) > 1:
                                    self.log(f"  [Gmail Reader] ✅ Found OTP: {otp_code} (sent {age_sec:.0f}s ago)")
                                    try:
                                        mail.store(newest_id, "+FLAGS", "\\Deleted")
                                        mail.expunge()
                                    except Exception:
                                        pass
                                    mail.logout()
                                    return otp_code
                mail.logout()
            except Exception as ex:
                pass

            time.sleep(1)

        self.log("  [Gmail Reader] Timeout: No new OTP email received within time limit.")
        return ""

    def fill_otp_and_handle_submission(self, otp: str) -> bool:
        auto_submit = bool(self.config.get("auto_submit_otp", False))
        target_dp = str(self.config.get("target_discount", "50")).strip()
        clean_otp = str(otp).strip()
        self.log(f"  [OTP] Filling OTP [{clean_otp}] into browser modal...")

        WebDriverWait(self.driver, 10, poll_frequency=0.1).until(
            lambda d: d.execute_script("""
                var m = document.getElementById('otpModal');
                if (!m) return false;
                var s = window.getComputedStyle(m);
                return m.style.display === 'block' || s.display === 'block';
            """)
        )

        otp_input = WebDriverWait(self.driver, 10, poll_frequency=0.1).until(
            EC.presence_of_element_located((By.ID, "otpInput"))
        )

        self.driver.execute_script("""
            var inp = document.getElementById('otpInput');
            if (inp) {
                inp.scrollIntoView({block: 'center'});
                inp.focus();
            }
        """)
        time.sleep(0.1)

        try:
            otp_input.clear()
        except Exception:
            self.driver.execute_script("document.getElementById('otpInput').value = '';")

        try:
            otp_input.send_keys(clean_otp)
        except Exception:
            pass

        self.driver.execute_script(f"""
            var targetDP = "{target_dp}";
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

        val_in_dom = self.driver.execute_script("return document.getElementById('otpInput').value;")
        self.log(f"  [OTP] Verified input box value: '{val_in_dom}'")

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
            self.notify_user_alert("otp")
            self.log("\n  ==================================================")
            self.log("  👉 [TEST MODE: AUTO-FILL ONLY]")
            self.log(f"     OTP [{val_in_dom}] filled in GREEN.")
            self.log("     Please manually click 'Submit OTP' in the Chrome browser.")
            self.log("     Waiting for submit action...")
            self.log("  ==================================================\n")

            try:
                WebDriverWait(self.driver, 300, poll_frequency=0.5).until(is_submitted)
                try:
                    alert = self.driver.switch_to.alert
                    self.log(f"  [Server Alert after Submit] {alert.text}")
                    alert.accept()
                except NoAlertPresentException:
                    pass
            except TimeoutException:
                self.log("  [Timeout] Waiting for manual submit.")
        else:
            self.log(f"  [Auto-Submit] Submitting OTP ({clean_otp})...")
            self.driver.execute_script("""
                if (typeof submitOtp === 'function') {
                    submitOtp();
                } else {
                    var btn = document.querySelector('input[value="Submit OTP"]');
                    if (btn) btn.click();
                }
            """)

            try:
                WebDriverWait(self.driver, 3, poll_frequency=0.1).until(EC.alert_is_present())
                alert = self.driver.switch_to.alert
                self.log(f"  [Server Alert] {alert.text}")
                alert.accept()
            except TimeoutException:
                pass

        target_url = self.config.get("target_url", DEFAULT_CONFIG["target_url"])
        self.log("  [OTP Done] Opening new browser tab for next pass...")
        self.driver.execute_script("window.open(arguments[0]);", target_url)
        self.driver.switch_to.window(self.driver.window_handles[-1])
        return True

    def process_single_page(self, page_num: int, total_pages: int) -> bool:
        page_start = time.time()
        target_dp = self.config.get("target_discount", "50")
        self.log(f"\n--- Processing Page {page_num} of {total_pages} (Target: {target_dp}%) ---")

        if page_num > 1:
            ok = self.navigate_to_page(page_num)
            if not ok:
                self.log(f"  Failed to navigate to Page {page_num}, skipping.")
                return False

        t_scan = time.time()
        res = self.select_all_matching_rows()
        count = res["count"]
        serials = res["serials"]
        scan_ms = (time.time() - t_scan) * 1000

        if count == 0:
            self.log(f"  [Scan: {scan_ms:.1f}ms] 0 items at {target_dp}% on Page {page_num}.")
            return False

        self.log(f"  [Scan: {scan_ms:.1f}ms] Selected {count} row(s) [Serials: {', '.join(serials[:5])}]")

        self.lock_browser_against_refresh()
        try:
            self.submit_and_wait_for_otp_modal()
            otp = self.fetch_otp_from_gmail(max_age_seconds=180, timeout=self.config.get("email_poll_timeout", 60))
            if not otp:
                self.notify_user_alert("otp")
                self.log("  ⚠️ Could not auto-fetch OTP. Please check Gmail settings.")
                return False

            success = self.fill_otp_and_handle_submission(otp)
            if success:
                self.log(f"  ✓ Page {page_num} completed in {time.time() - page_start:.2f}s")
                return True
            return False
        finally:
            self.unlock_browser()

    def process_all_pages_once(self) -> int:
        total_pages = self.get_total_pages()
        processed_count = 0
        target_url = self.config.get("target_url", DEFAULT_CONFIG["target_url"])

        for page_num in range(1, total_pages + 1):
            if self.stop_event.is_set():
                break
            did_process = self.process_single_page(page_num, total_pages)
            if did_process:
                processed_count += 1

            if page_num < total_pages and not self.stop_event.is_set():
                self.log(f"  Preparing clean state for Page {page_num + 1}...")
                self.driver.get(target_url)
                self.wait_for_grid_ready()

        return processed_count

    def live_monitoring_loop(self):
        target_dp = self.config.get("target_discount", "50")
        poll_interval = int(self.config.get("poll_interval_seconds", 5))
        target_url = self.config.get("target_url", DEFAULT_CONFIG["target_url"])

        self.log("\n" + "=" * 55)
        self.log(f"  LIVE MONITORING ACTIVATED (Target: {target_dp}%)")
        self.log(f"  Sweeping all pages every {poll_interval}s...")
        self.log("=" * 55)

        check_cycle = 0

        while not self.stop_event.is_set():
            time.sleep(poll_interval)
            if self.stop_event.is_set():
                break

            check_cycle += 1
            now_str = datetime.now().strftime("%H:%M:%S")

            try:
                self.driver.get(target_url)
                self.wait_for_grid_ready(timeout=12)
            except Exception as err:
                self.log(f"  [{now_str}] Temporary lag: {err}. Retrying...")
                continue

            total_pages = self.get_total_pages()
            found_on_any = False

            for page_num in range(1, total_pages + 1):
                if self.stop_event.is_set():
                    break
                if page_num > 1:
                    ok = self.navigate_to_page(page_num)
                    if not ok:
                        continue

                t_scan = time.time()
                res = self.select_all_matching_rows()
                scan_ms = (time.time() - t_scan) * 1000

                if res["count"] > 0:
                    found_on_any = True
                    self.notify_user_alert("update")
                    self.log(f"\n🎯 [{now_str}] FOUND {res['count']} UNIT(S) WITH {target_dp}% ON PAGE {page_num}! [Scan: {scan_ms:.1f}ms]")

                    self.lock_browser_against_refresh()
                    try:
                        self.submit_and_wait_for_otp_modal()
                        otp = self.fetch_otp_from_gmail(max_age_seconds=180, timeout=self.config.get("email_poll_timeout", 60))
                        if otp:
                            self.fill_otp_and_handle_submission(otp)
                    finally:
                        self.unlock_browser()
                    break

            if not found_on_any:
                pages_desc = f"all {total_pages} pages" if total_pages > 1 else "Page 1"
                self.log(f"  [{now_str}] Check #{check_cycle}: Swept {pages_desc} — 0 items at {target_dp}%.")

    def run(self):
        try:
            target_url = self.config.get("target_url", DEFAULT_CONFIG["target_url"])
            self.driver = self.init_driver()

            self.log(f"Loading Portal: {target_url}")
            self.driver.get(target_url)
            self.wait_for_grid_ready()
            self.log("  Portal grid loaded and ready.")

            self.process_all_pages_once()

            if self.config.get("auto_monitor_updates", True) and not self.stop_event.is_set():
                self.live_monitoring_loop()

        except Exception as e:
            self.log(f"\n  Fatal Engine Error: {e}")
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.log("  Browser closed.")


# ─────────────────────────────────────────────────────────
# MAIN TKINTER GUI APPLICATION
# ─────────────────────────────────────────────────────────
class LG4AllGuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LG4All NGSI Bidding Bot - Software Suite v2.0")
        self.root.geometry("920x720")
        self.root.minsize(800, 600)

        # Apply Modern Dark Theme Colors
        self.bg_dark = "#1e1e2e"
        self.bg_card = "#252538"
        self.fg_text = "#cdd6f4"
        self.accent_green = "#a6e3a1"
        self.accent_blue = "#89b4fa"
        self.accent_red = "#f38ba8"
        self.accent_yellow = "#f9e2af"
        self.border_color = "#45475a"

        self.root.configure(bg=self.bg_dark)

        # Thread & Queue control
        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.bot_thread = None
        self.is_running = False

        # Config state
        self.config = load_config()

        self._init_styles()
        self._build_ui()
        self._load_config_to_ui()

        # Start queue polling loop
        self.root.after(100, self._poll_log_queue)

    def _init_styles(self):
        self.style = ttk.Style()
        self.style.theme_use("clam")

        # Frame styles
        self.style.configure("TFrame", background=self.bg_dark)
        self.style.configure("Card.TFrame", background=self.bg_card, relief="flat")
        self.style.configure("TLabelframe", background=self.bg_card, foreground=self.fg_text, relief="solid", borderwidth=1)
        self.style.configure("TLabelframe.Label", background=self.bg_card, foreground=self.accent_blue, font=("Segoe UI", 10, "bold"))

        # Label styles
        self.style.configure("TLabel", background=self.bg_card, foreground=self.fg_text, font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", background=self.bg_dark, foreground=self.accent_green, font=("Segoe UI", 16, "bold"))

        # Entry & Combobox styles
        self.style.configure("TEntry", fieldbackground="#11111b", foreground="#cdd6f4", bordercolor=self.border_color)
        self.style.configure("TCombobox", fieldbackground="#11111b", foreground="#cdd6f4", background="#313244")

        # Checkbutton styles
        self.style.configure("TCheckbutton", background=self.bg_card, foreground=self.fg_text, font=("Segoe UI", 10))
        self.style.map("TCheckbutton", background=[("active", self.bg_card)], foreground=[("active", self.accent_blue)])

    def _build_ui(self):
        # 1. Header Bar
        header_frame = tk.Frame(self.root, bg=self.bg_dark, pady=10, padx=15)
        header_frame.pack(fill="x")

        title_lbl = tk.Label(
            header_frame,
            text="⚡ LG4All NGSI Bidding Automation Software",
            font=("Segoe UI", 15, "bold"),
            bg=self.bg_dark,
            fg=self.accent_blue
        )
        title_lbl.pack(side="left")

        self.status_lbl = tk.Label(
            header_frame,
            text="● STOPPED",
            font=("Segoe UI", 11, "bold"),
            bg=self.bg_dark,
            fg=self.accent_red
        )
        self.status_lbl.pack(side="right")

        # 2. Main Container (Split into Left Panel: Settings, Right Panel: Controls & Terminal)
        main_container = tk.Frame(self.root, bg=self.bg_dark, padx=15, pady=5)
        main_container.pack(fill="both", expand=True)

        left_frame = ttk.LabelFrame(main_container, text=" Configuration & Preferences ", padding=15)
        left_frame.pack(side="left", fill="both", expand=False, padx=(0, 10))
        left_frame.config(width=380)

        right_frame = tk.Frame(main_container, bg=self.bg_dark)
        right_frame.pack(side="right", fill="both", expand=True)

        # ── LEFT PANEL: CONFIGURATION ──
        # Target Discount
        row = 0
        lbl_disc = ttk.Label(left_frame, text="Target Discount (%):")
        lbl_disc.grid(row=row, column=0, sticky="w", pady=6)

        self.disc_var = tk.StringVar(value="50")
        disc_combo = ttk.Combobox(left_frame, textvariable=self.disc_var, values=["50", "70", "30", "40", "60", "80"], width=12)
        disc_combo.grid(row=row, column=1, sticky="w", pady=6, padx=5)

        # Auto Submit OTP Toggle
        row += 1
        self.auto_submit_var = tk.BooleanVar(value=False)
        chk_submit = ttk.Checkbutton(
            left_frame,
            text="Auto-Submit OTP (Full Auto)",
            variable=self.auto_submit_var,
            command=self._on_auto_submit_toggle
        )
        chk_submit.grid(row=row, column=0, columnspan=2, sticky="w", pady=6)

        self.lbl_mode_desc = tk.Label(
            left_frame,
            text="ℹ️ Test Mode: Auto-fills OTP in GREEN, manual click.",
            font=("Segoe UI", 9, "italic"),
            bg=self.bg_card,
            fg=self.accent_yellow,
            wraplength=320,
            justify="left"
        )
        self.lbl_mode_desc.grid(row=row+1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # Gmail Address
        row += 2
        lbl_gmail = ttk.Label(left_frame, text="Gmail Address:")
        lbl_gmail.grid(row=row, column=0, sticky="w", pady=4)
        self.entry_gmail = ttk.Entry(left_frame, width=28)
        self.entry_gmail.grid(row=row+1, column=0, columnspan=2, sticky="we", pady=(0, 8))

        # Gmail App Password
        row += 2
        lbl_pass = ttk.Label(left_frame, text="Gmail App Password:")
        lbl_pass.grid(row=row, column=0, sticky="w", pady=4)

        pass_frame = tk.Frame(left_frame, bg=self.bg_card)
        pass_frame.grid(row=row+1, column=0, columnspan=2, sticky="we", pady=(0, 8))

        self.entry_pass = ttk.Entry(pass_frame, show="*", width=22)
        self.entry_pass.pack(side="left", fill="x", expand=True)

        self.btn_show_pass = tk.Button(
            pass_frame, text="👁", command=self._toggle_pass_visibility,
            bg="#313244", fg="#cdd6f4", bd=0, padx=6, relief="flat"
        )
        self.btn_show_pass.pack(side="right", padx=(5, 0))

        # Auto Monitor Options
        row += 2
        self.auto_monitor_var = tk.BooleanVar(value=True)
        chk_monitor = ttk.Checkbutton(left_frame, text="Auto-Monitor Idle Inventory", variable=self.auto_monitor_var)
        chk_monitor.grid(row=row, column=0, columnspan=2, sticky="w", pady=6)

        row += 1
        lbl_poll = ttk.Label(left_frame, text="Idle Check Interval (sec):")
        lbl_poll.grid(row=row, column=0, sticky="w", pady=4)
        self.spin_poll = ttk.Spinbox(left_frame, from_=2, to=60, width=8)
        self.spin_poll.grid(row=row, column=1, sticky="w", pady=4)

        # Save Config Button
        row += 1
        btn_save = tk.Button(
            left_frame,
            text="💾 Save Configuration",
            command=self._save_config_from_ui,
            bg="#313244",
            fg=self.accent_blue,
            font=("Segoe UI", 10, "bold"),
            bd=1,
            relief="solid",
            pady=6
        )
        btn_save.grid(row=row, column=0, columnspan=2, sticky="we", pady=(15, 5))

        # ── RIGHT PANEL: CONTROLS & LOG TERMINAL ──
        # Control Buttons Frame
        ctrl_frame = ttk.LabelFrame(right_frame, text=" Software Controls ", padding=10)
        ctrl_frame.pack(fill="x", pady=(0, 10))

        self.btn_start = tk.Button(
            ctrl_frame,
            text="▶️ START BIDDING BOT",
            command=self._start_bot,
            bg="#232634",
            fg=self.accent_green,
            activebackground=self.accent_green,
            activeforeground="#11111b",
            font=("Segoe UI", 12, "bold"),
            bd=2,
            relief="raised",
            pady=8,
            padx=20
        )
        self.btn_start.pack(side="left", padx=10, fill="x", expand=True)

        self.btn_stop = tk.Button(
            ctrl_frame,
            text="⏹️ STOP BOT",
            command=self._stop_bot,
            bg="#232634",
            fg=self.accent_red,
            activebackground=self.accent_red,
            activeforeground="#11111b",
            font=("Segoe UI", 12, "bold"),
            bd=2,
            relief="raised",
            state="disabled",
            pady=8,
            padx=20
        )
        self.btn_stop.pack(side="right", padx=10, fill="x", expand=True)

        # Terminal Log Box
        log_frame = ttk.LabelFrame(right_frame, text=" Live Operations Log ", padding=10)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame,
            bg="#11111b",
            fg="#a6adc8",
            insertbackground="#cdd6f4",
            font=("Consolas", 10),
            bd=0,
            wrap="word"
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Clear log button bar
        log_bar = tk.Frame(log_frame, bg="#11111b")
        btn_clear = tk.Button(
            log_frame, text="Clear Log", command=self._clear_log,
            bg="#313244", fg="#cdd6f4", font=("Segoe UI", 8), bd=0, padx=8
        )
        btn_clear.place(relx=1.0, rely=0.0, anchor="ne", x=-20, y=5)

    def _toggle_pass_visibility(self):
        if self.entry_pass.cget("show") == "*":
            self.entry_pass.config(show="")
            self.btn_show_pass.config(text="🙈")
        else:
            self.entry_pass.config(show="*")
            self.btn_show_pass.config(text="👁")

    def _on_auto_submit_toggle(self):
        if self.auto_submit_var.get():
            self.lbl_mode_desc.config(
                text="⚡ Full Auto Mode: Fills OTP & submits instantly automatically!",
                fg=self.accent_green
            )
        else:
            self.lbl_mode_desc.config(
                text="ℹ️ Test Mode: Auto-fills OTP in GREEN, manual click.",
                fg=self.accent_yellow
            )

    def _load_config_to_ui(self):
        self.disc_var.set(self.config.get("target_discount", "50"))
        self.auto_submit_var.set(self.config.get("auto_submit_otp", False))
        self.entry_gmail.delete(0, tk.END)
        self.entry_gmail.insert(0, self.config.get("gmail_address", ""))
        self.entry_pass.delete(0, tk.END)
        self.entry_pass.insert(0, self.config.get("gmail_app_password", ""))
        self.auto_monitor_var.set(self.config.get("auto_monitor_updates", True))
        self.spin_poll.delete(0, tk.END)
        self.spin_poll.insert(0, str(self.config.get("poll_interval_seconds", 5)))
        self._on_auto_submit_toggle()

    def _save_config_from_ui(self):
        self.config["target_discount"] = self.disc_var.get().strip()
        self.config["auto_submit_otp"] = self.auto_submit_var.get()
        self.config["gmail_address"] = self.entry_gmail.get().strip()
        self.config["gmail_app_password"] = self.entry_pass.get().strip()
        self.config["auto_monitor_updates"] = self.auto_monitor_var.get()
        try:
            self.config["poll_interval_seconds"] = int(self.spin_poll.get().strip())
        except ValueError:
            self.config["poll_interval_seconds"] = 5

        save_config(self.config)
        messagebox.showinfo("Saved", "Configuration updated and saved to config.json successfully!")

    def _clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, msg)
                self.log_text.see(tk.END)
            except queue.Empty:
                break
        self.root.after(100, self._poll_log_queue)

    def _start_bot(self):
        if self.is_running:
            return

        self._save_config_from_ui()

        self.is_running = True
        self.stop_event.clear()

        self.status_lbl.config(text="● RUNNING", fg=self.accent_green)
        self.btn_start.config(state="disabled", bg="#181825", fg="#585b70")
        self.btn_stop.config(state="normal", bg="#f38ba8", fg="#11111b")

        self.log_text.insert(tk.END, "\n" + "═" * 50 + "\n")
        self.log_text.insert(tk.END, f"🚀 STARTING LG4ALL BIDDING BOT ENGINE [{datetime.now().strftime('%H:%M:%S')}]\n")
        self.log_text.insert(tk.END, f" Target Discount : {self.config.get('target_discount')}% \n")
        self.log_text.insert(tk.END, f" Auto-Submit OTP : {'ENABLED (Full Auto)' if self.config.get('auto_submit_otp') else 'DISABLED (Test Mode)'}\n")
        self.log_text.insert(tk.END, f" Gmail Inbox     : {self.config.get('gmail_address')}\n")
        self.log_text.insert(tk.END, "═" * 50 + "\n\n")
        self.log_text.see(tk.END)

        engine = BiddingEngine(self.config, self.log_queue, self.stop_event)
        self.bot_thread = threading.Thread(target=self._run_engine_thread, args=(engine,), daemon=True)
        self.bot_thread.start()

    def _run_engine_thread(self, engine: BiddingEngine):
        try:
            engine.run()
        finally:
            self.root.after(0, self._on_bot_stopped)

    def _stop_bot(self):
        if not self.is_running:
            return

        self.log_text.insert(tk.END, "\n🛑 STOP requested... Shutting down bot gracefully...\n")
        self.log_text.see(tk.END)
        self.stop_event.set()
        self.btn_stop.config(state="disabled")

    def _on_bot_stopped(self):
        self.is_running = False
        self.status_lbl.config(text="● STOPPED", fg=self.accent_red)
        self.btn_start.config(state="normal", bg="#232634", fg=self.accent_green)
        self.btn_stop.config(state="disabled", bg="#232634", fg=self.accent_red)
        self.log_text.insert(tk.END, "\n[System] Bot stopped and cleaned up.\n")
        self.log_text.see(tk.END)


def main():
    root = tk.Tk()
    app = LG4AllGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
