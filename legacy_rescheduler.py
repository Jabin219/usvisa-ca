from datetime import date, datetime

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from settings import CONSULATES, TEST_MODE, TIMEOUT, USER_CONSULATE


CONSULATE_FIELD = (By.ID, "appointments_consulate_appointment_facility_id")
DATE_FIELD = (By.ID, "appointments_consulate_appointment_date")
DATE_PICKER = (By.ID, "ui-datepicker-div")
TIME_FIELD = (By.ID, "appointments_consulate_appointment_time")


class AppointmentSubmissionUnknown(RuntimeError):
    """The confirmation click may have reached the appointment service."""


def select_consulate(driver: WebDriver) -> str:
    field = WebDriverWait(driver, TIMEOUT).until(EC.element_to_be_clickable(CONSULATE_FIELD))
    consulate_id = str(CONSULATES[USER_CONSULATE])
    Select(field).select_by_value(consulate_id)
    if field.get_property("value") != consulate_id:
        raise RuntimeError(f"Could not select {USER_CONSULATE} on the appointment form")
    return consulate_id


def _displayed_month(picker) -> date:
    title = picker.find_element(By.CLASS_NAME, "ui-datepicker-title").text
    return datetime.strptime(title, "%B %Y").date()


def _select_date(driver: WebDriver, date_to_book: date) -> bool:
    wait = WebDriverWait(driver, TIMEOUT)
    if not EC.visibility_of_element_located(DATE_PICKER)(driver):
        wait.until(EC.element_to_be_clickable(
            (By.ID, "appointments_consulate_appointment_date_input")
        )).click()
    picker = wait.until(EC.visibility_of_element_located(DATE_PICKER))
    target_month = date_to_book.replace(day=1)
    month = _displayed_month(picker)
    month_offset = (target_month.year - month.year) * 12 + target_month.month - month.month
    direction = "next" if month_offset > 0 else "prev"
    target_day = (
        By.XPATH,
        f".//td[@data-year='{date_to_book.year}' and @data-month='{date_to_book.month - 1}']"
        f"/a[normalize-space()='{date_to_book.day}']",
    )

    for _ in range(abs(month_offset) + 1):
        days = picker.find_elements(*target_day)
        if days:
            days[0].click()
            return True
        if month == target_month:
            break
        controls = picker.find_elements(
            By.CSS_SELECTOR, f".ui-datepicker-{direction}:not(.ui-state-disabled)"
        )
        if not controls:
            break
        controls[0].click()
        wait.until(lambda _: _displayed_month(picker) != month)
        month = _displayed_month(picker)
    return False


def _last_available_time(driver: WebDriver):
    time_field = driver.find_element(*TIME_FIELD)
    if not time_field.is_displayed() or not time_field.is_enabled():
        return False
    options = [
        option for option in Select(time_field).options
        if option.is_enabled() and option.get_property("value")
    ]
    return options[-1] if options else False


def legacy_reschedule(driver: WebDriver, date_to_book: date) -> bool:
    """Return whether an exact slot was previewed or submitted."""
    consulate_id = select_consulate(driver)
    wait = WebDriverWait(driver, TIMEOUT)
    try:
        if not _select_date(driver, date_to_book):
            return False
        time_option = wait.until(_last_available_time)
        appointment_time = time_option.text
        time_value = time_option.get_property("value")
        time_option.click()
    except TimeoutException:
        return False

    selected_city = driver.find_element(*CONSULATE_FIELD).get_property("value")
    selected_date = driver.find_element(*DATE_FIELD).get_property("value")
    selected_time = driver.find_element(*TIME_FIELD).get_property("value")
    if selected_city != consulate_id or selected_date != date_to_book.isoformat() or selected_time != time_value:
        return False

    if TEST_MODE:
        print(f"Test mode: selected {USER_CONSULATE} on {date_to_book} at {appointment_time}")
        return True

    submit = wait.until(EC.element_to_be_clickable((By.ID, "appointments_submit")))
    try:
        submit.click()
        confirm = wait.until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[6]/div/div/a[2]")))
        confirm.click()
    except WebDriverException as error:
        raise AppointmentSubmissionUnknown(
            "Confirmation status is unknown. Check your AIS account before running again."
        ) from error
    return True
