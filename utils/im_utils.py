import math
import re
import time
from typing import Optional, List, Dict, Union

from bs4 import BeautifulSoup
from pydantic import BaseModel
from selenium import webdriver
from selenium.common import WebDriverException, TimeoutException
from selenium.webdriver import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support import expected_conditions as EC

from model.sheet_model import IM


class QuantityItem(BaseModel):
    min: int
    max: int


class PriceItem(BaseModel):
    title: str
    min_quantity: float
    max_quantity: float
    price: float
    info: str

    class Config:
        arbitrary_types_allowed = True


def _extract_quantity_from_text(text: str) -> Optional[QuantityItem]:
    if not text:
        return None
    s = text.strip()
    s = s.split("~")
    if len(s) == 1:
        quantity = _parse_korean_number_string(s[0].strip())
        return QuantityItem(
            min=quantity,
            max=quantity
        )
    elif len(s) == 2:
        _min = _parse_korean_number_string(s[0].strip())
        _max = _parse_korean_number_string(s[1].strip())
        return QuantityItem(
            min=_min,
            max=_max
        )
    else:
        print(f"Error: Wrong format for quantity string: {text}")
        return None


def get_page_source_by_url_by_selenium(sd: WebDriver, url: str, im: IM) -> Optional[str]:
    print(f"Đang điều hướng tới URL: {url}")
    try:
        sd.get(url)

        try:
            search_box = WebDriverWait(sd, 10).until(
                EC.element_to_be_clickable((By.ID, "word"))
            )

            search_box.clear()  # Clear any default text
            search_box.send_keys(im.IM_INCLUDE_KEYWORD)  # Type your desired search term

            search_box.send_keys(Keys.RETURN)  # Simulate pressing the Enter key

            time.sleep(2)

        except Exception as e:
            print(f"Đã xảy ra lỗi: {e}")

        WebDriverWait(sd, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'ul.search_list_premium > li, ul.search_list_normal > li'))
        )

        return sd.page_source

    except TimeoutException:
        print(f"Time out: {url}")
        return None
    except WebDriverException as e:
        print(f"E WebDriver in {url}: {e}")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None


# --- HÀM LÕI MỚI ---
def _parse_korean_number_string(s: str) -> Optional[int]:
    """
    Hàm lõi, có khả năng phân tích một chuỗi số phức tạp của Hàn Quốc
    bao gồm các đơn vị '조' (nghìn tỷ), '억' (trăm triệu), '만' (chục nghìn).
    Ví dụ: '99조9,999억' -> 99999900000000
    """
    if not s:
        return None

    s = s.replace(',', '').strip()

    # Định nghĩa các đơn vị và giá trị tương ứng
    units = {'조': 10 ** 12, '억': 10 ** 8, '만': 10 ** 4}
    total_value = 0
    current_number_str = ""

    for char in s:
        if char.isdigit() or char == '.':
            current_number_str += char
        elif char in units:
            if not current_number_str:
                current_number_str = "1"  # Xử lý trường hợp "만" -> 1만
            total_value += float(current_number_str) * units[char]
            current_number_str = ""
        # Bỏ qua các ký tự khác như '개'

    # Cộng phần số còn lại (nếu có)
    if current_number_str:
        total_value += float(current_number_str)

    return int(total_value) if total_value > 0 else None


def _parse_unit_price(price_str: str) -> Optional[float]:
    """
    Sử dụng Regex (phiên bản non-greedy) để trích xuất đơn giá một cách chính xác
    từ chuỗi text có thể bị dính liền.
    Ví dụ: '1당 100원최소 3,000원' -> 100
    """
    if not price_str:
        return None

    # Regex mới với quantifier non-greedy (+?)
    # Nó sẽ tìm con số ngắn nhất có thể đứng trước chữ '원'
    # Điều này giải quyết vấn đề chuỗi bị dính liền '...원최소...'
    match = re.search(r'([^당]+)당\s*([\d,]+?)\s*원', price_str)

    if match:
        try:
            unit_block_str = match.group(1)
            price_for_block_str = match.group(2)

            # Hàm _parse_korean_number_string bạn đã có
            unit_block_size = _parse_korean_number_string(unit_block_str)
            price_for_block = float(price_for_block_str.replace(',', ''))

            if unit_block_size and unit_block_size > 0:
                true_unit_price = price_for_block / unit_block_size
                return true_unit_price
        except Exception as e:
            print(f"Error while parsing unit price from '{price_str}': {e}")
            return None
    else:
        return _parse_korean_number_string(price_str)


def get_im_min_price_in_source(page_source: str, im: IM, min_price_sheet, max_price_sheet) -> Optional[PriceItem]:
    """
    Phân tích page source HTML từ itemmania, trích xuất tất cả sản phẩm từ
    cả list NORMAL và PREMIUM, và trả về sản phẩm có giá thấp nhất.
    """
    if not page_source:
        print("Warning: page_source trống, không thể phân tích.")
        return None

    soup = BeautifulSoup(page_source, 'html.parser')
    all_items: List[PriceItem] = []
    # Selector này lấy TẤT CẢ các thẻ <li> từ cả hai danh sách premium và normal trong một lần.
    # Dấu phẩy (,) hoạt động như một toán tử "OR".
    product_rows = soup.select('ul.search_list_premium > li, ul.search_list_normal > li')

    print(f"Tìm thấy tổng cộng {len(product_rows)} dòng sản phẩm tiềm năng từ tất cả các danh sách.")

    for row in product_rows:
        # Bỏ qua dòng tiêu đề (header)
        if 'list_head' in row.get('class', []):
            continue

        try:
            title_element = row.select_one('a.subject')
            quantity_element = row.select_one('div.col.quantity')
            price_element = row.select_one('div.col.price')
            info_element = row.select_one('div.col.info')

            title = title_element.get_text(strip=True) if title_element else "N/A"
            info = info_element.get_text(separator=' ', strip=True) if info_element else "N/A"

            quantity_raw = quantity_element.get_text(strip=True) if quantity_element else None
            price_raw = price_element.get_text(strip=True) if price_element else None

            quantity = _extract_quantity_from_text(quantity_raw)
            price = _parse_unit_price(price_raw)

            if price is not None and quantity is not None:
                if price < min_price_sheet:
                    continue
                if price > max_price_sheet:
                    continue
                all_items.append(
                    PriceItem(
                        title=title,
                        min_quantity=quantity.min,
                        max_quantity=quantity.max,
                        price=price,
                        info=info
                    )
                )
        except Exception as e:
            print(f"Warning: Bỏ qua một dòng do lỗi khi phân tích: {e}")
            continue

    if not all_items:
        print("Warning: Không trích xuất được sản phẩm hợp lệ nào từ page source.")
        return None

    if im.IM_INCLUDE_KEYWORD:
        all_items = [item for item in all_items if im.IM_INCLUDE_KEYWORD.lower() in item.title.lower()]
    if im.IM_EXCLUDE_KEYWORD:
        all_items = [item for item in all_items if im.IM_EXCLUDE_KEYWORD.lower() not in item.title.lower()]

    try:
        min_price_item = min(all_items, key=lambda item: item.price)
        print(f"Success: Đã phân tích {len(all_items)} sản phẩm. Giá thấp nhất là: {min_price_item.price:,.0f}")
        return min_price_item
    except (ValueError, TypeError):
        print("Error: Không thể tìm thấy giá thấp nhất.")
        return None


def get_im_min_price(sd: WebDriver, im: IM) -> Optional[PriceItem]:
    """Get the minimum price from the IM page."""
    try:
        url = im.IM_PRODUCT_COMPARE
        if not url:
            print("Không có URL để so sánh sản phẩm.")
            return None
        page_source = get_page_source_by_url_by_selenium(sd, url, im)
        if not page_source:
            print("Cant get page source.")
            return None
        min_price_sheet = im.get_im_min_price()
        max_price_sheet = im.get_im_max_price()
        min_price = get_im_min_price_in_source(page_source, im, min_price_sheet, max_price_sheet)

        return min_price

    except Exception as e:
        print(f"Lỗi khi lấy giá thấp nhất: {e}")
        return None


def calculate_new_price_and_quantity(
        im_config: IM,
        competitor_item: PriceItem
) -> Optional[Dict[str, Union[float, int]]]:
    """
    Tính toán giá bán mới và số lượng tối thiểu dựa trên giá của đối thủ và các quy tắc trong model IM.

    Args:
        im_config (IM): Đối tượng chứa các quy tắc cấu hình từ Google Sheet.
        competitor_item (PriceItem): Đối tượng chứa thông tin sản phẩm giá rẻ nhất của đối thủ.

    Returns:
        Một dictionary chứa 'new_price' và 'new_min_quantity' nếu thành công,
        ngược lại trả về None.
    """
    pass
