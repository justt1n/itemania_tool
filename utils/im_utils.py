import base64
import math
import os
import time
import re
from typing import Optional, List, Dict, Union, Any
from urllib.parse import parse_qs, urlparse, unquote

import phpserialize
import requests
from pydantic import BaseModel
from selenium import webdriver
from selenium.common import WebDriverException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from model.sheet_model import IM


def handle_new_tab_popup(web_driver: webdriver.Chrome):
    """
    Checks for and closes a new browser tab if it opens, then returns focus to the main tab.
    """
    print("Checking for new tab pop-ups...")
    try:
        # Get the handle of the original window
        original_window = web_driver.current_window_handle

        # Wait for a second window to appear (adjust timeout as needed)
        WebDriverWait(web_driver, 5).until(EC.number_of_windows_to_be(2))

        print("New tab detected.")
        # Loop through all window handles
        for window_handle in web_driver.window_handles:
            if window_handle != original_window:
                # Switch to the new tab
                web_driver.switch_to.window(window_handle)
                print(f"Switched to new tab with URL: {web_driver.current_url}")

                # Close the new tab
                web_driver.close()
                print("New tab closed.")
                break  # Exit loop once the new tab is found and closed

        # Switch back to the original window
        web_driver.switch_to.window(original_window)
        print("Switched back to the main tab.")

    except TimeoutException:
        # This is good, it means no new tab opened within the timeout period.
        print("No new tab pop-up appeared.")
    except Exception as e:
        print(f"An error occurred while handling new tab: {e}")


def create_selenium_driver():
    options = Options()
    prefs = {"profile.default_content_setting_values.popups": 2}  # 2 = Block, 1 = Allow
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--disable-notifications")  # Disables browser notification prompts
    options.add_experimental_option("excludeSwitches", ["enable-automation"])  # Hides "Chrome is being controlled" bar

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    print("Creating Selenium driver...")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    print("Selenium driver created successfully.")
    return driver


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


def get_list_product(sd: WebDriver, im: IM):
    try:
        url = im.IM_PRODUCT_COMPARE
        cookies = sd.get_cookies()
        session_cookies = {cookie['name']: cookie['value'] for cookie in cookies}
        session_cookies['common_search'] = build_common_search_cookie_from_url(url)
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)

        game_code = query_params.get('search_game', [''])[0]
        server_code = query_params.get('search_server', [''])[0]
        search_goods = query_params.get('search_goods', ['all'])[0]

        headers = {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9,vi;q=0.8',
            'Connection': 'keep-alive',
            'Origin': 'https://www.itemmania.com',
            'Referer': url,
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/135.0.0.0 Safari/537.36',
            'X-REQUESTED-WITH': 'XMLHttpRequest',
            'sec-ch-ua': '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Linux"'
        }

        data = {
            'game_code': game_code,
            'server_code': server_code,
            'search_goods': search_goods,
            'goods_type': '1',
            'trade_state': '1',
            'credit_type': '1',
            'pinit': '1',
        }
    except Exception as e:
        raise ValueError(f"Error parsing URL: {e}")

    try:
        response = requests.post(
            'https://www.itemmania.com/sell/ajax_list_search.php',
            headers=headers,
            cookies=session_cookies,
            data=data,
            verify=False
        )
        response.raise_for_status()
        data = response.json()
        items = extract_and_combine_trades(data, mode=im.IM_COMPARE_ALL)
        filter_items = filter_trades_by_subject(items, im)
        transformed_items = transform_trade_list(filter_items)
        transformed_items.sort(key=lambda x: int(x.get('trade_money', 0)))
        return transformed_items
    except requests.RequestException as e:
        raise ValueError(f"Error fetching data from ItemMania: {e}")


def transform_trade_list(original_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keys_to_keep = {
        'seller_id',
        'trade_money',
        'ea_trade_money',
        'trade_quantity',
        'trade_subject',
        'ea_range',
        'max_quantity',
        'min_quantity',
        'min_trade_money',
        'seller_rank',
        'str_trade_kind',
        'trade_kind'
    }

    return [{key: item.get(key) for key in keys_to_keep} for item in original_list]


def extract_and_combine_trades(raw_data_dict: Dict[str, Any], mode) -> List[Dict[str, Any]]:
    if mode == None:
        mode = 1

    data = raw_data_dict.get('data', {})

    g_list = data.get('g', [])
    p_list = data.get('p', [])
    power_data = data.get('power', {})
    power_list = list(power_data.values())
    if mode == 1:
        # Combine 'g', 'p', and 'power' lists into one
        combined_list = g_list + p_list + power_list
    else:
        # Combine 'g' and 'p' lists only
        combined_list = g_list + p_list
    return combined_list


def filter_trades_by_subject(trade_list: List[Dict[str, Any]], im: IM) -> List[Dict[str, Any]]:
    filtered_list = []

    excl_str = im.IM_EXCLUDE_KEYWORD or ''
    incl_str = im.IM_INCLUDE_KEYWORD or ''

    incl_keywords = [keyword.strip() for keyword in incl_str.split(',') if keyword.strip()]
    excl_keywords = [keyword.strip() for keyword in excl_str.split(',') if keyword.strip()]

    for item in trade_list:
        if item.get('trade_state') == 'p':
            continue
        subject = item.get('trade_subject', '')

        include_match = not incl_keywords or any(keyword in subject for keyword in incl_keywords)

        exclude_match = excl_keywords and any(keyword in subject for keyword in excl_keywords)

        if include_match and not exclude_match:
            filtered_list.append(item)

    return filtered_list


def build_common_search_cookie_from_url(url: str) -> str:
    parsed_url = urlparse(url)
    query = parse_qs(parsed_url.query)

    # Extract values
    game_code = query.get('search_game', [''])[0]
    server_code = query.get('search_server', [''])[0]
    game_name = unquote(query.get('search_game_text', [''])[0])
    server_name = unquote(query.get('search_server_text', [''])[0])
    search_goods = query.get('search_goods', ['all'])[0]
    search_word = game_name
    referer = 'ajax_list_search.php'

    # Build PHP-like dict
    data = {
        b'game_code': game_code.encode(),
        b'server_code': server_code.encode(),
        b'game_name': game_name.encode('utf-8'),
        b'server_name': server_name.encode('utf-8'),
        b'search_goods': search_goods.encode(),
        b'search_order': search_word.encode('utf-8'),
        b'trade_state': b'1',
        b'credit_type': b'1',
        b'goods_type': b'1',
        b'search_referer': referer.encode(),
        b'reg_time': b'1',
    }

    serialized = phpserialize.dumps(data)
    encoded = base64.b64encode(serialized).decode()

    return encoded


def get_im_min_price(list_product: Dict, min_price_sheet, max_price_sheet) -> Optional[PriceItem]:
    try:
        if not list_product:
            print("No product found in the list.")
            return None
        filtered_list = []
        for item in list_product:
            _item_price = int(item.get('trade_money', '0'))
            try:
                trade_quantity = item.get('trade_quantity', '0')
                if trade_quantity is None:
                    trade_quantity = '0'  # Default to '0' if None
                _item_quantity = int(trade_quantity)
                if _item_quantity <= 0:
                    raise ValueError("Quantity is zero or negative.")
            except ValueError:
                print(
                    f"Invalid quantity for item {item.get('trade_subject', 'Unknown')}: {trade_quantity}, defaulting to 1")
                _item_quantity = 1
            _price = _item_price / _item_quantity
            # _price = _item_price
            if min_price_sheet <= _price <= max_price_sheet:
                filtered_list.append(
                    PriceItem(
                        title=item.get('trade_subject', ''),
                        min_quantity=item.get('min_quantity', 1),
                        max_quantity=item.get('max_quantity', 99999),
                        price=_price,
                        info=item.get('seller_id', 'cant get seller id')
                    )
                )
        if not filtered_list:
            print("No items found within the specified price range.")
            return None
        min_price_item = min(filtered_list, key=lambda x: x.price)
        return min_price_item

    except Exception as e:
        print(f"Error when get min price: {e}")
        return None


def login_first(web_driver: WebDriver):
    try:
        print("Login...")
        ###LOGIN###
        web_driver.maximize_window()
        web_driver.get(
            "https://trade.itemmania.com/portal/user/p_login_form.html?returnUrl=https%3A%2F%2Ftrade.itemmania.com%2F")
        # click_element_by_text(web_driver, "로그인", "a")
        handle_new_tab_popup(web_driver)
        input_to_field(web_driver, os.getenv("IM_USERNAME"), "user_id")
        input_to_field(web_driver, os.getenv("IM_PASSWORD"), "user_password")
        click_element_by_text(web_driver, "로그인", "button")
        handle_new_tab_popup(web_driver)
        web_driver.minimize_window()
        return True
    except TimeoutException:
        print(f"Time out when logging in.")
        return False
    except WebDriverException as e:
        print(f"E WebDriver in: {e}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def ceil_up(number: float, base: int):
    if base <= 0 and base % 10 != 0:
        raise ValueError("Base must be in [1,10,100,1000,...]")

    result = math.ceil(number / base) * base

    return int(result)


class EditPrice(BaseModel):
    quantity_per_sell: int
    price: float
    min_quantity: int = 1
    max_quantity: int = 99999
    price_reduction: float = 0


def calc_min_quantity(price, im: IM) -> int:
    if price > im.IM_TOTAL_ORDER_MIN:
        return 1
    if im.IM_IS_UPDATE_ORDER_MIN:
        tmp_min_quantity = ceil_up(im.IM_TOTAL_ORDER_MIN / price, im.IM_HE_SO_LAM_TRON)
    else:
        tmp_min_quantity = math.ceil(im.IM_TOTAL_ORDER_MIN / price)
    return tmp_min_quantity


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


def input_to_field(web_driver: WebDriver, text: str, input_id: str):
    try:
        input_field = WebDriverWait(web_driver, 10).until(
            EC.element_to_be_clickable((By.ID, input_id))
        )

        input_field.clear()

        input_field.send_keys(text)

        print("Successfully entered 'abc' into the input field.")
    except Exception as e:
        print(f"Error when input to field {input_id} : {e}")


def click_element_by_text(web_driver: WebDriver, text: str, tag: str = '*'):
    try:
        xpath_selector = f"//{tag}[contains(text(), '{text}')]"

        clickable_element = WebDriverWait(web_driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, xpath_selector))
        )
        clickable_element.click()
        print(f"Successfully clicked on element with text: '{text}'")

    except Exception as e:
        print(f"Error when trying to click element with text '{text}': {e}")


def click_element_by_text_robust(web_driver: WebDriver, text: str, tag: str = '*'):
    """
    Finds and clicks an element robustly based on its text content,
    including text within child elements.

    Args:
        web_driver: The Selenium WebDriver instance.
        text: The visible text (or a unique partial text) to search for.
        tag: The HTML tag of the element (e.g., 'button', 'a', 'div').
             Defaults to '*' to search any tag.
    """
    try:
        # --- KEY CHANGE HERE ---
        # Using contains(., '...') instead of contains(text(), '...').
        # The dot '.' refers to the string value of the element and all its descendants,
        # making it much more powerful for complex elements.
        xpath_selector = f"//{tag}[contains(., '{text}')]"

        clickable_element = WebDriverWait(web_driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, xpath_selector))
        )
        clickable_element.click()
        print(f"Successfully clicked on element containing text: '{text}'")

    except Exception as e:
        print(f"Error when trying to click element with text '{text}': {e}")

def find_product_id_from_all_page(web_driver: WebDriver, im: IM):
    page = 1
    while True:
        try:
            url = f"https://trade.itemmania.com/myroom/sell/sell_regist.html?page={page}&strRelationType=regist"
            web_driver.get(url)
            time.sleep(3)
            # Check record number in table tb_list
            rows = web_driver.find_elements(By.CSS_SELECTOR, "table.tb_list tr")
            if len(rows) <= 1:  # chỉ còn header
                print("No more records in list.")
                break
            product_id = find_product_id_to_change_price(web_driver, im)
            if product_id:
                return product_id
            page += 1
        except Exception as e:
            print(f"Error when trying to get page: {e}")
            return None
    return None

def find_product_id_to_change_price(web_driver: WebDriver, im: IM):
    try:
        print(f"Find product id by title: {im.IM_PRODUCT_LINK}")
        xpath_selector = f"//a[contains(text(), '{im.IM_PRODUCT_LINK}')]"
        clickable_element = WebDriverWait(web_driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, xpath_selector))
        )
        href = clickable_element.get_attribute("href")
        if not href:
            print("href attribute is None")
            return None
        match = re.search(r"id=(\d+)", href)
        if not match:
            print(f"id not found in href: {href}")
            return None

        if not _verify_to_update(clickable_element, im):
            return None

        return match.group(1)
    except Exception as e:
        print(f"Error when trying to find product id: {e}")
        return None

def process_change_price(web_driver: WebDriver, im: IM, edit_object: EditPrice):
    product_id = find_product_id_from_all_page(web_driver, im)
    if product_id is None:
        print("No product id found")
        return False
    do_change_price(web_driver, edit_object, product_id)

def do_change_price(web_driver: WebDriver, edit_object: EditPrice, product_id: str):
    url = f"https://trade.itemmania.com/myroom/sell/sell_re_reg.html?id={product_id}"
    try:
        web_driver.get(url)
        time.sleep(3)
        input_to_field(web_driver, str(edit_object.min_quantity), "user_quantity_min")
        input_to_field(web_driver, str(edit_object.max_quantity), "user_quantity_max")
        input_to_field(web_driver, str(edit_object.quantity_per_sell), "user_division_unit")
        input_to_field(web_driver, str(int(edit_object.price)), "user_division_price")
        click_element_by_text(web_driver, "재등록", "button")
        click_element_by_text(web_driver, "확인", "button")
        # close alert pop up
        try:
            WebDriverWait(web_driver, 10).until(EC.alert_is_present())
            alert = web_driver.switch_to.alert
            alert.accept()
            print("Alert accepted successfully.")
        except TimeoutException:
            print("No alert found, continuing...")
        return True
    except TimeoutException:
        print(f"Time out: {url}")
        return False
    except WebDriverException as e:
        print(f"E WebDriver in {url}: {e}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def _verify_to_update(clickable_element , im: IM):
    #Verify Min Stock
    #Get parent element
    td_elem = clickable_element.find_element(By.XPATH, "./ancestor::td[contains(@class, 'left')]")
    maxStock = _get_max_quantity_from_td(td_elem);
    print(f"get max stock: {maxStock}")
    if im.IM_MINUPDATESTOCK and maxStock is not None:
        if int(im.IM_MINUPDATESTOCK) > maxStock:
            print(f"quantity lower than minimum stock")
            return False
    return True

def _parse_korean_number_string(s: str) -> Optional[int]:
    """
    bao gồm các đơn vị '조' (nghìn tỷ), '억' (trăm triệu), '만' (chục nghìn).
    Ví dụ: '99조9,999억' -> 99999900000000
    """
    if not s:
        return None

    s = s.replace(',', '').strip()

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

def _get_max_quantity_from_td(td_elem):
    # Lấy text của td element
    td_text = td_elem.text
    print(f"TD text: {td_text}")

    # Ví dụ: [67~1만9,000], [67~4,427], [1~14]
    patterns = [
        r"\[[\d,조억만]+~([\d,조억만]+)\]",  # Pattern [x~y]
    ]

    for pattern in patterns:
        match = re.search(pattern, td_text)
        if match:
            max_str = match.group(1)
            print(f"Found max_str: {max_str}")
            try:
                result = _parse_korean_number_string(max_str)
                print(f"Parsed result: {result}")
                return result
            except Exception as e:
                print(f"Cannot parse number: {max_str}, error: {e}")
                continue

    print("Not found pattern [x~y]")
    return None


def main():
    edit_obj = EditPrice(
        min_quantity=67,
        max_quantity=4427,
        quantity_per_sell=1,
        price=400
    )
    a = IM()
    sd = create_selenium_driver()
    process_change_price(sd, a, edit_obj)


if __name__ == "__main__":
    main()
