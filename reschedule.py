import argparse
import re
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from itertools import count
from time import sleep
from urllib.parse import urljoin, urlsplit

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidSessionIdException,
    NoSuchElementException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.exceptions import HTTPError as BrowserConnectionError

from legacy.gmail import GMail, Message
from legacy_rescheduler import (
    CONSULATE_FIELD,
    AppointmentSubmissionUnknown,
    legacy_reschedule,
    select_consulate,
)
from settings import *


class SessionExpired(RuntimeError):
    """AIS is asking for sign-in again."""


def _is_login_page(url: str) -> bool:
    current = urlsplit(url)
    login = urlsplit(LOGIN_URL)
    return (
        current.scheme == login.scheme
        and current.netloc == login.netloc
        and current.path.rstrip("/") == login.path.rstrip("/")
    )


def _is_appointment_page(url: str) -> bool:
    current = urlsplit(url)
    site = urlsplit(LOGIN_URL)
    return (
        current.scheme == site.scheme
        and current.netloc == site.netloc
        and re.fullmatch(r"/en-ca/niv/schedule/\d+/appointment/?", current.path) is not None
    )


def log_message(message: str) -> None:
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")


def get_chrome_driver() -> tuple[WebDriver, str]:
    options = webdriver.ChromeOptions()
    if not SHOW_GUI:
        options.add_argument("headless")
        options.add_argument("window-size=1920x1080")
        options.add_argument("disable-gpu")
        options.add_argument('user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36')
    options.add_experimental_option("detach", DETACH)
    options.add_argument('--incognito')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    temp_dir = tempfile.mkdtemp(prefix="usvisa-chrome-")
    options.add_argument(f"--user-data-dir={temp_dir}")
    try:
        driver = webdriver.Chrome(options=options)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return driver, temp_dir


def login(driver: WebDriver) -> None:
    driver.get(LOGIN_URL)
    timeout = TIMEOUT

    email_input = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "user_email"))
    )
    email_input.send_keys(USER_EMAIL)

    password_input = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.ID, "user_password"))
    )
    password_input.send_keys(USER_PASSWORD)

    policy_checkbox = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox"))
    )
    policy_checkbox.click()

    login_button = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.NAME, "commit"))
    )
    login_button.click()


def _find_visible_action(driver: WebDriver, label: str):
    locators = (
        (By.LINK_TEXT, label),
        (By.XPATH, f"//button[normalize-space()='{label}']"),
        (By.XPATH, f"//input[@value='{label}']"),
    )
    for locator in locators:
        for element in driver.find_elements(*locator):
            if element.is_displayed() and element.is_enabled():
                return element
    return False


def _click_action_if_present(
    driver: WebDriver, label: str, timeout: float
) -> bool:
    try:
        action = WebDriverWait(driver, timeout).until(
            lambda current_driver: _find_visible_action(current_driver, label)
        )
    except TimeoutException:
        return False
    action.click()
    sleep(2)
    return True


def _has_visible_element(driver: WebDriver, locator) -> bool:
    return any(
        element.is_displayed()
        for element in driver.find_elements(*locator)
    )


def _appointment_page_state(driver: WebDriver):
    if _has_visible_element(driver, CONSULATE_FIELD):
        return "ready"
    if _find_visible_action(driver, "Schedule Appointment"):
        return "schedule"
    if _has_visible_element(driver, (By.CLASS_NAME, "icheckbox")):
        return "policy"
    if NUM_PARTICIPANTS > 1 and _find_visible_action(driver, "Continue"):
        return "participants"
    return False


def _prepare_appointment_page(driver: WebDriver) -> None:
    timeout = TIMEOUT
    for _ in range(3):
        state = WebDriverWait(driver, timeout).until(_appointment_page_state)
        if state == "ready":
            return
        if state in ("schedule", "participants"):
            label = "Schedule Appointment" if state == "schedule" else "Continue"
            _click_action_if_present(driver, label, timeout)
            continue

        policy_checkbox = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox"))
        )
        policy_checkbox.click()
        WebDriverWait(driver, timeout).until(
            lambda current_driver: _find_visible_action(current_driver, "Continue")
        ).click()

    WebDriverWait(driver, timeout).until(
        lambda current_driver: _appointment_page_state(current_driver) == "ready"
    )


def get_appointment_page(driver: WebDriver) -> None:
    timeout = TIMEOUT

    # Newer flows show a group-action page with this link. Older flows first
    # show a Continue link and then expose Schedule Appointment.
    if not _click_action_if_present(driver, "Schedule Appointment", 2):
        if not _click_action_if_present(driver, "Continue", timeout):
            raise TimeoutException(
                "Could not find either 'Schedule Appointment' or 'Continue'"
            )
        has_schedule_id = re.search(r"/schedule/\d+", driver.current_url)
        _click_action_if_present(driver, "Schedule Appointment", 2 if has_schedule_id else timeout)

    current_url = driver.current_url
    schedule_match = re.search(r"/schedule/(\d+)", current_url)
    if not schedule_match:
        raise RuntimeError("Could not find a schedule ID after sign-in")

    appointment_url = APPOINTMENT_PAGE_URL.format(id=schedule_match.group(1))
    driver.get(appointment_url)


def get_available_dates(driver: WebDriver) -> list[date] | None:
    browser_url = driver.current_url
    if _is_login_page(browser_url):
        raise SessionExpired("Browser returned to sign-in")
    if not _is_appointment_page(browser_url):
        raise RuntimeError("The browser is not on the appointment form")
    current_url = urlsplit(browser_url)

    request_url = (
        f"{current_url.scheme}://{current_url.netloc}{current_url.path.rstrip('/')}"
        f"{AVAILABLE_DATE_REQUEST_SUFFIX}"
    )
    request_headers = REQUEST_HEADERS.copy()
    request_headers["Cookie"] = "; ".join(
        f"{cookie['name']}={cookie['value']}" for cookie in driver.get_cookies()
    )
    request_headers["User-Agent"] = driver.execute_script("return navigator.userAgent")
    try:
        response = requests.get(
            request_url, headers=request_headers, timeout=TIMEOUT, allow_redirects=False
        )
    except requests.RequestException as error:
        log_message(f"Date request failed: {type(error).__name__}")
        return None
    if response.status_code == 401:
        raise SessionExpired("Date request returned HTTP 401")
    if 300 <= response.status_code < 400 and _is_login_page(
        urljoin(request_url, response.headers.get("Location", ""))
    ):
        raise SessionExpired("Date request redirected to sign-in")
    if response.status_code != 200:
        log_message(f"Date request returned HTTP {response.status_code}")
        return None
    try:
        dates_json = response.json()
        if not isinstance(dates_json, list):
            raise ValueError("Expected a list of dates")
        return [date.fromisoformat(item["date"]) for item in dates_json]
    except (KeyError, TypeError, ValueError):
        log_message("Date request returned an invalid response")
        return None


def eligible_dates(dates: list[date]) -> list[date]:
    try:
        earliest = date.fromisoformat(EARLIEST_ACCEPTABLE_DATE)
        latest = date.fromisoformat(LATEST_ACCEPTABLE_DATE)
        exclusions = [
            (date.fromisoformat(start), date.fromisoformat(end))
            for start, end in EXCLUSION_DATE_RANGES
        ]
    except (TypeError, ValueError) as error:
        raise ValueError("Set both start and end for each date range in YYYY-MM-DD format") from error
    if earliest > latest or any(start > end for start, end in exclusions):
        raise ValueError("Each date range must end on or after its start date")

    return [
        day for day in sorted(set(dates))
        if earliest <= day <= latest and not any(start <= day <= end for start, end in exclusions)
    ]


def send_notification(subject: str, message: str) -> bool:
    if not all((GMAIL_EMAIL, GMAIL_APPLICATION_PWD, RECEIVER_EMAIL)):
        return False
    gmail = None
    try:
        sender = f"{GMAIL_SENDER_NAME} <{GMAIL_EMAIL}>" if GMAIL_SENDER_NAME else GMAIL_EMAIL
        recipient = f"{RECEIVER_NAME} <{RECEIVER_EMAIL}>" if RECEIVER_NAME else RECEIVER_EMAIL
        gmail = GMail(sender, GMAIL_APPLICATION_PWD)
        gmail.send(Message(subject, to=recipient, text=message))
        return True
    except Exception as error:
        log_message(f"Email notification failed: {type(error).__name__}")
        return False
    finally:
        if gmail is not None:
            try:
                gmail.close()
            except Exception as error:
                log_message(f"Could not close email connection: {type(error).__name__}")


def send_slot_alert(candidate: date) -> bool:
    subject = f"{USER_CONSULATE} 美签面签空位 {candidate}"
    message = (
        f"AIS 查到 {USER_CONSULATE} 在 {candidate} 有符合你日期设置的面签空位。\n"
        f"脚本正在尝试自动改签。你可以登录 AIS 查看当前预约。\n{LOGIN_URL}"
    )
    sent = send_notification(subject, message)
    if sent:
        log_message(f"Gmail accepted the availability alert for {candidate}")
    return sent


def reschedule(
    driver: WebDriver, *,
    on_match: Callable[[date], None] | None = None,
) -> bool:
    for poll_number in count(1):
        log_message(f"Checking available dates (session check {poll_number})")
        dates = get_available_dates(driver)
        if dates is not None:
            candidates = eligible_dates(dates)
            if not candidates:
                log_message(f"{len(dates)} available dates. None match your date preferences.")
            elif on_match is not None:
                on_match(candidates[0])
            for candidate in candidates:
                log_message(f"Found a matching date at {USER_CONSULATE}: {candidate}")
                try:
                    selected = legacy_reschedule(driver, candidate)
                except AppointmentSubmissionUnknown:
                    if on_match is not None:
                        send_notification(
                            "美签面签改签状态待确认",
                            f"{USER_CONSULATE} {candidate} 的提交状态待确认。"
                            f"请登录 AIS 查看当前预约。\n{LOGIN_URL}",
                        )
                    raise
                except (
                    ElementClickInterceptedException,
                    ElementNotInteractableException,
                    NoSuchElementException,
                    StaleElementReferenceException,
                    TimeoutException,
                ) as error:
                    if _is_login_page(driver.current_url):
                        raise SessionExpired("Browser returned to sign-in") from error
                    log_message(f"Slot selection was interrupted: {type(error).__name__}")
                    break
                if not selected:
                    log_message(f"Could not select the exact slot for {candidate}")
                    continue
                if not TEST_MODE:
                    message = (
                        f"Submitted a reschedule request for {candidate} at {USER_CONSULATE}. "
                        "Check your AIS account for the updated appointment."
                    )
                    log_message(message)
                    if send_notification("Visa appointment reschedule submitted", message):
                        log_message("Gmail accepted the submission notification")
                return True
        sleep(DATE_REQUEST_DELAY)


def reschedule_with_new_session(
    *, check_only: bool = False, on_match: Callable[[date], None] | None = None,
) -> bool:
    driver, temp_dir = get_chrome_driver()
    try:
        attempts = 1 if check_only else NEW_SESSION_AFTER_FAILURES
        signed_in = False
        for attempt in range(attempts):
            try:
                if not signed_in or _is_login_page(driver.current_url):
                    signed_in = False
                    login(driver)
                    WebDriverWait(driver, TIMEOUT).until(
                        lambda current_driver: not _is_login_page(current_driver.current_url)
                    )
                    signed_in = True
                if not _is_appointment_page(driver.current_url):
                    get_appointment_page(driver)
                _prepare_appointment_page(driver)
                break
            except (InvalidSessionIdException, NoSuchWindowException, BrowserConnectionError):
                raise
            except Exception as error:
                log_message(f"Unable to open the appointment form: {type(error).__name__}")
                if attempt + 1 < attempts:
                    sleep(FAIL_RETRY_DELAY)
                elif signed_in and not check_only and not _is_login_page(driver.current_url):
                    raise
        else:
            return False

        if check_only:
            select_consulate(driver)
            log_message(f"Selected {USER_CONSULATE} on the appointment form")
            dates = get_available_dates(driver)
            if dates is None:
                return False
            candidates = eligible_dates(dates)
            log_message(f"Date check passed. {len(dates)} available, {len(candidates)} within your preferences.")
            if candidates:
                log_message(f"Earliest matching date: {candidates[0]}")
            return True
        log_message(f"Signed in. Checking every {DATE_REQUEST_DELAY} seconds while the session is active.")
        return reschedule(driver, on_match=on_match)
    except SessionExpired as error:
        log_message(f"AIS session expired: {error}")
        return False
    except (InvalidSessionIdException, NoSuchWindowException, BrowserConnectionError) as error:
        log_message(f"Browser session disconnected: {type(error).__name__}")
        return False
    except AppointmentSubmissionUnknown:
        raise
    except Exception as error:
        log_message(f"Appointment session failed: {type(error).__name__}")
        if check_only:
            return False
        raise
    finally:
        try:
            driver.quit()
        except Exception as error:
            log_message(f"Could not close the browser: {type(error).__name__}")
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Sign in, select the consulate and check dates once")
    mode.add_argument(
        "--notify-and-reschedule", action="store_true",
        help="Email matching dates and automatically submit a reschedule request",
    )
    args = parser.parse_args()
    if not USER_EMAIL or not USER_PASSWORD:
        parser.error("Set USER_EMAIL and USER_PASSWORD in .env")
    try:
        eligible_dates([])
    except ValueError as error:
        parser.error(str(error))

    if args.check:
        return 0 if reschedule_with_new_session(check_only=True) else 1

    if args.notify_and_reschedule:
        if TEST_MODE:
            parser.error("Set TEST_MODE = False to submit reschedule requests")
        if not all((GMAIL_EMAIL, GMAIL_APPLICATION_PWD, RECEIVER_EMAIL)):
            parser.error("Set GMAIL_EMAIL, GMAIL_APPLICATION_PWD and RECEIVER_EMAIL in .env")
        log_message("Automatic rescheduling and Gmail slot alerts enabled")
    elif TEST_MODE:
        log_message("Test mode: preview a matching slot without submitting the appointment")

    with ThreadPoolExecutor(max_workers=1) as alerts:
        queued: dict[date, Future[bool]] = {}

        def queue_slot_alert(candidate: date) -> None:
            previous = queued.get(candidate)
            if previous is not None and (not previous.done() or previous.result()):
                return
            queued[candidate] = alerts.submit(send_slot_alert, candidate)

        on_match = queue_slot_alert if args.notify_and_reschedule else None
        try:
            while not reschedule_with_new_session(on_match=on_match):
                sleep(NEW_SESSION_DELAY)
        except AppointmentSubmissionUnknown as error:
            log_message(str(error))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
