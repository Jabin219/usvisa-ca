import re
import traceback
import os
import shutil
import glob
from datetime import datetime, timedelta
from time import sleep
from typing import Union, List, Tuple

import requests
from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from legacy.gmail import GMail, Message
from legacy_rescheduler import legacy_reschedule
from request_tracker import RequestTracker
from settings import *


def log_message(message: str) -> None:
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")


def cleanup_old_chrome_dirs(max_age_hours: int = 24, exclude_dirs: List[str] = None) -> None:
    """
    清理旧的Chrome临时目录
    
    Args:
        max_age_hours: 保留多少小时内的目录（默认24小时）
        exclude_dirs: 要排除的目录列表（当前正在使用的目录）
    """
    if exclude_dirs is None:
        exclude_dirs = []
    
    try:
        # 查找所有匹配的Chrome临时目录
        pattern = "/tmp/chrome-*"
        chrome_dirs = glob.glob(pattern)
        
        current_time = datetime.now()
        cleaned_count = 0
        total_size = 0
        
        for dir_path in chrome_dirs:
            # 跳过当前正在使用的目录
            if dir_path in exclude_dirs:
                continue
            
            try:
                # 获取目录的修改时间
                dir_mtime = datetime.fromtimestamp(os.path.getmtime(dir_path))
                age = current_time - dir_mtime
                
                # 如果目录超过指定时间，则删除
                if age > timedelta(hours=max_age_hours):
                    # 计算目录大小
                    dir_size = sum(
                        os.path.getsize(os.path.join(dirpath, filename))
                        for dirpath, dirnames, filenames in os.walk(dir_path)
                        for filename in filenames
                    )
                    total_size += dir_size
                    
                    # 删除目录
                    shutil.rmtree(dir_path, ignore_errors=True)
                    cleaned_count += 1
                    log_message(f"已清理旧目录: {dir_path} (年龄: {age}, 大小: {dir_size / 1024 / 1024:.2f} MB)")
            except Exception as e:
                # 如果无法访问或删除某个目录，记录日志但继续处理其他目录
                log_message(f"清理目录时出错 {dir_path}: {e}")
                continue
        
        if cleaned_count > 0:
            log_message(f"清理完成: 删除了 {cleaned_count} 个旧目录，释放了 {total_size / 1024 / 1024:.2f} MB 空间")
    except Exception as e:
        log_message(f"清理临时目录时出错: {e}")


def get_chrome_driver() -> Tuple[WebDriver, str]:
    """
    创建Chrome驱动并返回驱动实例和临时目录路径
    
    Returns:
        tuple: (WebDriver实例, 临时目录路径)
    """
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
    
    # 创建临时目录路径
    temp_dir = f'/tmp/chrome-{datetime.now().strftime("%Y%m%d-%H%M%S")}'
    options.add_argument(f'--user-data-dir={temp_dir}')
    
    # 在创建新驱动前清理旧目录（排除当前目录）
    cleanup_old_chrome_dirs(max_age_hours=1, exclude_dirs=[temp_dir])
    
    driver = webdriver.Chrome(options=options)
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


def get_appointment_page(driver: WebDriver) -> None:
    timeout = TIMEOUT
    continue_button = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.LINK_TEXT, "Continue"))
    )
    continue_button.click()
    sleep(2)
    current_url = driver.current_url
    url_id = re.search(r"/(\d+)", current_url).group(1)
    appointment_url = APPOINTMENT_PAGE_URL.format(id=url_id)
    driver.get(appointment_url)


def get_available_dates(
    driver: WebDriver, request_tracker: RequestTracker
) -> Union[List[datetime.date], None]:
    request_tracker.log_retry()
    request_tracker.retry()
    try:
        # 检查浏览器会话是否仍然有效
        current_url = driver.current_url
    except Exception as e:
        log_message(f"Browser session lost: {e}")
        raise  # 重新抛出异常，让调用者知道需要重新创建会话
    
    try:
        schedule_base = current_url.split("/appointment")[0]
        request_url = schedule_base + "/appointment" + AVAILABLE_DATE_REQUEST_SUFFIX
        request_header_cookie = "".join(
            [f"{cookie['name']}={cookie['value']};" for cookie in driver.get_cookies()]
        )
        request_headers = REQUEST_HEADERS.copy()
        request_headers["Cookie"] = request_header_cookie
        request_headers["User-Agent"] = driver.execute_script("return navigator.userAgent")
    except Exception as e:
        log_message(f"Failed to get cookies or user agent: {e}")
        raise  # 重新抛出异常，让调用者知道需要重新创建会话
    try:
        response = requests.get(request_url, headers=request_headers)
    except Exception as e:
        log_message(f"Get available dates request failed: {e}")
        return None
    if response.status_code != 200:
        log_message(f"Failed with status code {response.status_code}")
        log_message(f"Response Text: {response.text}")
        return None
    try:
        dates_json = response.json()
    except:
        log_message("Failed to decode json")
        log_message(f"Response Text: {response.text}")
        return None
    dates = [datetime.strptime(item["date"], "%Y-%m-%d").date() for item in dates_json]
    return dates


def reschedule(driver: WebDriver, retryCount: int = 0) -> bool:
    date_request_tracker = RequestTracker(
        retryCount if (retryCount > 0) else DATE_REQUEST_MAX_RETRY,
        DATE_REQUEST_DELAY * retryCount if (retryCount > 0) else DATE_REQUEST_MAX_TIME
    )
    while date_request_tracker.should_retry():
        try:
            dates = get_available_dates(driver, date_request_tracker)
        except Exception as e:
            log_message(f"Browser session error in get_available_dates: {e}")
            log_message("Browser session may have been lost, need to recreate session")
            raise  # 重新抛出异常，让调用者知道需要重新创建会话
        
        if not dates:
            log_message("Error occured when requesting available dates")
            sleep(DATE_REQUEST_DELAY)
            continue
        earliest_available_date = dates[0]
        earliest_acceptable_date = datetime.strptime(EARLIEST_ACCEPTABLE_DATE, "%Y-%m-%d").date()
        latest_acceptable_date = datetime.strptime(LATEST_ACCEPTABLE_DATE, "%Y-%m-%d").date()
        if earliest_acceptable_date <= earliest_available_date <= latest_acceptable_date:
            # Check if the earliest available date falls in any of the excluded date ranges
            for i, (start, end) in enumerate(EXCLUSION_DATE_RANGES, 1):
                if datetime.strptime(start, "%Y-%m-%d").date() <= earliest_available_date <= datetime.strptime(end, "%Y-%m-%d").date():
                    log_message(f"UH OH! Date falls in excluded date range: {start} to {end}")
                    sleep(DATE_REQUEST_DELAY)
                    continue
            log_message(f"FOUND SLOT ON {earliest_available_date}!!!")
            try:
                if legacy_reschedule(driver, earliest_available_date):
                    gmail = GMail(f"{GMAIL_SENDER_NAME} <{GMAIL_EMAIL}>", GMAIL_APPLICATION_PWD)
                    msg = Message(
                        f"Visa Appointment Rescheduled for {earliest_available_date}",
                        to=f"{RECEIVER_NAME} <{RECEIVER_EMAIL}>",
                        text=f"Your visa appointment has been successfully rescheduled to {earliest_available_date} at {USER_CONSULATE} consulate."
                    )
                    gmail.send(msg)
                    gmail.close()
                    log_message("SUCCESSFULLY RESCHEDULED!!!")
                    return True
                return False
            except Exception as e:
                log_message(f"Rescheduling failed: {e}")
                traceback.print_exc()
                continue
        else:
            log_message(f"Earliest available date is {earliest_available_date}")
        sleep(DATE_REQUEST_DELAY)
    return False


def reschedule_with_new_session(retryCount: int = DATE_REQUEST_MAX_RETRY) -> bool:
    driver, temp_dir = get_chrome_driver()
    session_failures = 0
    timeout = TIMEOUT
    while session_failures < NEW_SESSION_AFTER_FAILURES:
        try:
            login(driver)
            get_appointment_page(driver)
            policy_checkbox_limit = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.CLASS_NAME, "icheckbox")))
            policy_checkbox_limit.click()
            continue_button = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.NAME, "commit")))
            continue_button.click() 
            break
        except Exception as e:
            log_message(f"Unable to get appointment page: {e}")
            session_failures += 1
            sleep(FAIL_RETRY_DELAY)
            continue
    
    try:
        rescheduled = reschedule(driver, retryCount)
    except Exception as e:
        log_message(f"Browser session lost during reschedule: {e}")
        log_message("Attempting to recreate session...")
        try:
            driver.quit()
        except:
            pass
        # 清理当前会话的临时目录
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except:
            pass
        # 重新创建会话并重试
        return reschedule_with_new_session(retryCount)
    finally:
        try:
            driver.quit()
        except:
            pass  # 如果 driver 已经关闭，忽略错误
        # 清理当前会话的临时目录
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
                log_message(f"已清理临时目录: {temp_dir}")
        except Exception as e:
            log_message(f"清理临时目录时出错 {temp_dir}: {e}")
    
    if rescheduled:
        return True
    else:
        return False


if __name__ == "__main__":
    session_count = 0
    log_message(f"Attempting to reschedule for email: {USER_EMAIL}")
    log_message(f"User Consulate: {USER_CONSULATE}")
    log_message(f"Earliest Acceptable Date: {EARLIEST_ACCEPTABLE_DATE}")
    log_message(f"Latest Acceptable Date: {LATEST_ACCEPTABLE_DATE}")

    if EXCLUSION_DATE_RANGES:
        log_message("Excluded Date Ranges:")
        for i, (start, end) in enumerate(EXCLUSION_DATE_RANGES, 1):
            log_message(f"  Range {i}: {start} to {end}")
    else:
        log_message("No date ranges excluded")

    # 程序启动时清理所有旧的临时目录
    log_message("正在清理旧的临时目录...")
    cleanup_old_chrome_dirs(max_age_hours=1)

    try:
        while True:
            session_count += 1
            log_message(f"Attempting with new session #{session_count}")
            rescheduled = reschedule_with_new_session()
            sleep(NEW_SESSION_DELAY)
            if rescheduled:
                break
    finally:
        # 程序退出前再次清理临时目录
        log_message("程序退出，清理所有临时目录...")
        cleanup_old_chrome_dirs(max_age_hours=0)
    
    gmail = GMail(f"{GMAIL_SENDER_NAME} <{GMAIL_EMAIL}>", GMAIL_APPLICATION_PWD)
    msg = Message(
        f"Rescheduler Program Exited",
        to=f"{RECEIVER_NAME} <{RECEIVER_EMAIL}>",
        text=f"The rescheduler program has exited on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."
    )
    gmail.send(msg)
    gmail.close()
