import os
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from dotenv import load_dotenv
from gspread.exceptions import APIError
from gspread.utils import a1_to_rowcol

import constants
from app.process import get_row_run_index
from decorator.retry import retry
from decorator.time_execution import time_execution
from model.payload import Row
# from utils.dd_utils import get_dd_min_price
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from utils.exceptions import PACrawlerError
from utils.ggsheet import GSheet, Sheet
from utils.im_utils import get_im_min_price
from utils.logger import setup_logging

### SETUP ###
load_dotenv("settings.env")

setup_logging()
gs = GSheet()


@dataclass
class Settings:
    spreadsheet_id: str
    sheet_name: str
    key_path: str
    main_loop_sleep_seconds: float
    row_process_sleep_seconds: float
    retry_attempts: int
    retry_delay_seconds: int


class SheetColumn(Enum):
    ERROR = "E"
    LAST_RUN_TIME = "F"
    PRICE = "I"
    TITLE = "J"
    STOCK = "K"


class ProcessStatus(Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT FOUND"
    ERROR = "ERROR"


@dataclass
class ProcessResult:
    status: ProcessStatus
    price: Optional[str] = None
    title: Optional[str] = None
    stock: Optional[str] = None


def load_settings_from_env() -> Settings:
    load_dotenv("settings.env")
    return Settings(
        spreadsheet_id=os.environ["SPREADSHEET_ID"],
        sheet_name=os.environ["SHEET_NAME"],
        key_path=os.environ["KEY_PATH"],
        main_loop_sleep_seconds=float(os.getenv("TIME_SLEEP", "60.0")),
        row_process_sleep_seconds=float(os.getenv("ROW_TIME_SLEEP", "3.0")),
        retry_attempts=5,
        retry_delay_seconds=15,
    )


### FUNCTIONS ###


@time_execution
@retry(5, delay=15, exception=PACrawlerError)
def process(
    gsheet: GSheet,
):
    print("process")
    try:
        sheet = Sheet.from_sheet_id(
            gsheet=gsheet,
            sheet_id=os.getenv("SPREADSHEET_ID"),  # type: ignore
        )
    except Exception as e:
        print(f"Error getting sheet: {e}")
        return
    try:
        worksheet = sheet.open_worksheet(os.getenv("SHEET_NAME"))  # type: ignore
    except APIError as e:
        print("Quota exceeded, sleeping for 60 seconds")
        time.sleep(60)
        return
    except Exception as e:
        print(f"Error getting worksheet: {e}")
        return
    row_indexes = get_row_run_index(worksheet=worksheet)

    for index in row_indexes:
        status = "NOT FOUND"
        print(f"Row: {index}")
        try:
            row = Row.from_row_index(worksheet, index)
        except Exception as e:
            print(f"Error getting row: {e}")
            _current_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            write_to_log_cell(worksheet, index, "Error: " + _current_time, log_type="time")
            continue
        if not isinstance(row, Row):
            continue
        try:
            sd = create_selenium_driver()
            min_price = get_im_min_price(sd, row.im)
            if min_price is None:
                print("No item info")
            else:
                print(f"Min price: {min_price[0]}")
                print(f"Title: {min_price[1]}")
                status = "FOUND"
                write_to_log_cell(worksheet, index, min_price[0], log_type="price")
                write_to_log_cell(worksheet, index, min_price[1], log_type="title")
                write_to_log_cell(worksheet, index, min_price[2], log_type="stock")
            try:
                _row_time_sleep = float(os.getenv("ROW_TIME_SLEEP"))
                print(f"Sleeping for {_row_time_sleep} seconds")
                time.sleep(_row_time_sleep)
            except Exception as e:
                print("No row time sleep, sleeping for 3 seconds by default")
                time.sleep(3)

        except Exception as e:
            print(f"Error calculating price change: {e}")
            continue
        write_to_log_cell(worksheet, index, status, log_type="status")
        _current_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        write_to_log_cell(worksheet, index, _current_time, log_type="time")
        print("Next row...")


def create_selenium_driver():
    options = Options()
    options.add_argument("--headless")  # Run in headless mode
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    print("Creating Selenium driver...")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    print("Selenium driver created successfully.")
    return driver


def write_to_log_cell(
    worksheet,
    row_index,
    log_str,
    log_type="log"
):
    try:
        r, c = None, None
        if log_type == "status":
            r, c = a1_to_rowcol(f"E{row_index}")
        if log_type == "time":
            r, c = a1_to_rowcol(f"F{row_index}")
        if log_type == "price":
            r, c = a1_to_rowcol(f"I{row_index}")
        if log_type == "title":
            r, c = a1_to_rowcol(f"J{row_index}")
        if log_type == "stock":
            r, c = a1_to_rowcol(f"K{row_index}")
        worksheet.update_cell(r, c, log_str)
    except Exception as e:
        print(f"Error writing to log cell: {e}")


### MAIN ###

if __name__ == "__main__":
    print("Starting...")
    gsheet = GSheet(constants.KEY_PATH)
    while True:
        try:
            process(gsheet)
            try:
                _time_sleep = float(os.getenv("TIME_SLEEP"))
            except Exception:
                _time_sleep = 0
            print(f"Sleeping for {_time_sleep} seconds")
            time.sleep(_time_sleep)
        except Exception as e:
            _str_error = f"Error: {e}"
            print(_str_error)
            time.sleep(60)  # Wait for 60 seconds before retrying
        print("Done")
