import os
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from gspread.exceptions import APIError
from gspread.utils import a1_to_rowcol
from selenium.webdriver.chrome.webdriver import WebDriver

import constants
from app.process import get_row_run_index
from decorator.retry import retry
from decorator.time_execution import time_execution
from model.payload import Row
from model.sheet_model import IM
from utils.exceptions import PACrawlerError
from utils.ggsheet import GSheet, Sheet
from utils.im_utils import get_im_min_price, EditPrice, calc_min_quantity, process_change_price, login_first, \
    create_selenium_driver, get_list_product, PriceItem
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
    browser: WebDriver
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
            prod_list = get_list_product(browser, row.im)
            min_price_sheet = row.im.get_im_min_price()
            max_price_sheet = row.im.get_im_max_price()
            competitor_item = get_im_min_price(prod_list, min_price_sheet, max_price_sheet)
            max_stock = row.im.get_im_stock()

            # Xác định giá bán mới của bạn
            if competitor_item is None:
                # Nếu không có đối thủ, đặt giá bằng giá tối thiểu trong sheet
                print("No offer in range, set to min")
                final_price = min_price_sheet
            else:
                # Nếu có đối thủ, tính toán giá mới để cạnh tranh
                final_price = calculate_final_price(competitor_item, row.im, min_price_sheet, max_price_sheet)
            final_price = final_price * row.im.IM_QUANTITY_GET_PRICE
            # Tạo đối tượng chứa thông tin giá sẽ được cập nhật
            edit_object = EditPrice(
                price=final_price,
                quantity_per_sell=row.im.IM_QUANTITY_GET_PRICE,
                min_quantity=calc_min_quantity(final_price, row.im),
                max_quantity=max_stock,
                price_reduction=row.im.IM_DONGIA_GIAM_MIN,
            )

            print(edit_object)
            process_change_price(browser, row.im, edit_object)

            # In giá của đối thủ ra console (nếu có)
            if competitor_item:
                print(f"Competitor price: {competitor_item.price}")

            # **Gọi hàm tạo log với đầy đủ tham số mới**
            price_log_str = _create_log_price(edit_object, prod_list, min_price_sheet, max_price_sheet, competitor_item)
            write_to_log_cell(worksheet, index, price_log_str, log_type="price")
            try:
                _row_time_sleep = float(os.getenv("SLEEP_TIME_EACH_ROUND"))
                print(f"Sleeping for {_row_time_sleep} seconds")
                time.sleep(_row_time_sleep)
            except Exception as e:
                print("No row time sleep, sleeping for 3 seconds by default")
                time.sleep(3)

        except Exception as e:
            print(f"Error calculating price change: {e}")
            continue
        _current_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        write_to_log_cell(worksheet, index, _current_time, log_type="time")
        print("Next row...")


# def create_selenium_driver():
#     options = Options()
#     options.add_argument("--headless")  # Run in headless mode
#     options.add_argument("--no-sandbox")
#     options.add_argument("--disable-dev-shm-usage")
#     print("Creating Selenium driver...")
#     driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
#     print("Selenium driver created successfully.")
#     return driver

def calculate_final_price(
    min_price: EditPrice,
    im: IM,
    min: float,
    max: float
) -> float:
    quantity_per_sell = im.IM_QUANTITY_GET_PRICE
    if quantity_per_sell and quantity_per_sell <= 0:
        quantity_per_sell = 1

    price_step = im.IM_DONGIA_GIAM_MIN / quantity_per_sell

    competitor_price = min_price.price

    proposed_price = competitor_price - price_step

    if proposed_price < min:
        final_price = min
    elif proposed_price > max:
        final_price = max
    else:
        final_price = proposed_price

    return final_price


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
            r, c = a1_to_rowcol(f"E{row_index}")
        if log_type == "price":
            r, c = a1_to_rowcol(f"D{row_index}")
        worksheet.update_cell(r, c, log_str)
    except Exception as e:
        print(f"Error writing to log cell: {e}")


def _create_log_price(
    listing_details: EditPrice,
    competitor_offers: List[Dict[str, Any]],
    sheet_min_price: float,
    sheet_max_price: float,
    comparison_item: Optional[PriceItem]
) -> str:
    """
    Tạo chuỗi log chi tiết cho việc cập nhật giá.

    Args:
        listing_details: Đối tượng EditPrice chứa thông tin giá và số lượng sẽ được cập nhật.
        competitor_offers: Danh sách đầy đủ các offer của đối thủ.
        sheet_min_price: Giá tối thiểu được cấu hình trong sheet.
        sheet_max_price: Giá tối đa được cấu hình trong sheet.
        comparison_item: Đối tượng PriceItem của đối thủ được dùng để so sánh giá.
                         Là None nếu không có đối thủ nào hợp lệ.

    Returns:
        Một chuỗi log đã được định dạng.
    """
    # Dòng tiêu đề và thông tin cơ bản
    log_header = (
        f"Cập nhật thành công lúc {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}, "
        f"Price = {int(listing_details.price)} ; QUANTITY_GET_PRICE = {listing_details.quantity_per_sell}\n"
        f"Stock min = {listing_details.min_quantity}; Stock max = {listing_details.max_quantity};\n"
        f"PriceMin = {int(sheet_min_price)}, PriceMax = {int(sheet_max_price)};"
    )

    # Dòng thông tin về đối thủ được dùng để so sánh
    if comparison_item:
        competitor_line = f"GiaSoSanh={int(comparison_item.price)}, Seller {comparison_item.info}\n"
        competitor_line += f"Title: {comparison_item.title or 'N/A'}"
    else:
        competitor_line = "GiaSoSanh=N/A, Seller N/A" # Trường hợp không tìm thấy đối thủ

    # Tiêu đề cho danh sách các offer
    offers_title = "\nCác offer có giá thấp hơn:"

    # Chi tiết danh sách các offer
    offer_details = ""
    if not competitor_offers:
        offer_details = "\nKhông có offer nào thấp hơn."
    else:
        offer_lines = [
            f"{index}/ {item.get('trade_subject', 'N/A')} price = {item.get('trade_money', 'N/A')};"
            for index, item in enumerate(competitor_offers[:5], start=1)
        ]
        offer_details = "\n" + "\n".join(offer_lines)

    # Kết hợp tất cả các phần thành chuỗi log cuối cùng
    return f"{log_header}\n{competitor_line}{offers_title}{offer_details}"



### MAIN ###
if __name__ == "__main__":
    print("Starting...")
    gsheet = GSheet(constants.KEY_PATH)
    sd = create_selenium_driver()
    login_first(sd)
    while True:
        try:
            process(gsheet, sd)
            try:
                _time_sleep = float(os.getenv("SLEEP_TIME"))
            except Exception:
                _time_sleep = 0
            print(f"Finished processing, sleeping for {_time_sleep} seconds")
            time.sleep(_time_sleep)
        except Exception as e:
            _str_error = f"Error: {e}"
            print(_str_error)
            time.sleep(60)  # Wait for 60 seconds before retrying
        print("Done")
