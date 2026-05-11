"""
sephora_monitor.py - 세포라 품절/재입고 모니터링 → Slack 알림
"""

import csv, json, logging, os, time, argparse
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# playwright-stealth v1/v2 모두 지원
try:
    from playwright_stealth import stealth_sync
    STEALTH_MODE = "v1"
except ImportError:
    try:
        from playwright_stealth import Stealth
        STEALTH_MODE = "v2"
    except ImportError:
        STEALTH_MODE = None

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / "config.env")

SLACK_WEBHOOK_URL      = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL          = os.getenv("SLACK_CHANNEL", "#sephora-alerts")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
REQUEST_DELAY_SECONDS  = float(os.getenv("REQUEST_DELAY_SECONDS", "3"))
STATE_FILE             = BASE_DIR / os.getenv("STATE_FILE", "state.json")
LOG_FILE               = BASE_DIR / os.getenv("LOG_FILE", "monitor.log")
PRODUCTS_CSV           = BASE_DIR / "products.csv"

# ─────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logger.info(f"Stealth 모드: {STEALTH_MODE or '미사용'}")

# ─────────────────────────────────────────
# 상태 파일
# ─────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────
# CSV 로드
# ─────────────────────────────────────────
def load_products() -> list:
    products = []
    with open(PRODUCTS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            url  = row.get("url", "").strip().strip("\r\n").strip()
            name = row.get("product_name", "").strip()
            if url and name:
                products.append({"product_name": name, "url": url})
    logger.info(f"제품 {len(products)}개 로드")
    return products

# ─────────────────────────────────────────
# 품절 감지
# ─────────────────────────────────────────
def check_stock_status(page, url: str) -> tuple:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        # React 렌더링 완료 대기
        page.wait_for_timeout(5000)
    except PlaywrightTimeout:
        return "unknown", "timeout"
    except Exception as e:
        return "unknown", str(e)

    # 페이지 내용 확인
    try:
        body_text = page.inner_text("body").strip()
        body_len  = len(body_text)
        # 디버그: 페이지 내용 일부 출력
        snippet = body_text[:200].replace("\n", " ")
        logger.info(f"  페이지 길이={body_len}, 미리보기: {snippet}")

        if body_len < 200:
            return "unknown", f"empty/blocked page (len={body_len})"

        text_lower = body_text.lower()

        # ── OUT OF STOCK 텍스트 감지 ──────────────────────────────
        if "out of stock" in text_lower:
            # Add to Basket도 있는지 확인 (다른 사이즈는 재고 있을 수 있음)
            if "add to basket" in text_lower or "add to bag" in text_lower:
                # skuId URL이면 해당 사이즈가 OOS
                if "skuid" in url.lower():
                    return "out_of_stock", "OUT OF STOCK (specific sku)"
                # skuId 없는 URL: 기본 사이즈 OOS 여부 확인
                # 제목 바로 옆에 OOS 뱃지가 있는 경우
                try:
                    badge = page.query_selector("b:has-text('OUT OF STOCK'), span:has-text('OUT OF STOCK'), div:has-text('OUT OF STOCK')")
                    if badge:
                        return "out_of_stock", f"OOS badge found"
                except Exception:
                    pass
                return "in_stock", "has Add to Basket (some sizes in stock)"
            return "out_of_stock", "OUT OF STOCK (no add to basket)"

        # ── Add to Basket 버튼 확인 ──────────────────────────────
        if "add to basket" in text_lower or "add to bag" in text_lower:
            try:
                btn = page.query_selector("button:has-text('Add to Basket'), button:has-text('Add to Bag')")
                if btn:
                    disabled = btn.get_attribute("disabled")
                    if disabled is not None:
                        return "out_of_stock", "Add to Basket disabled"
                    return "in_stock", "Add to Basket enabled"
            except Exception:
                pass
            return "in_stock", "Add to Basket text found"

        # ── JSON-LD fallback ──────────────────────────────────────
        for script in page.query_selector_all("script[type='application/ld+json']"):
            try:
                data = json.loads(script.inner_text())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    avail = offers.get("availability", "")
                    if "OutOfStock" in avail:
                        return "out_of_stock", f"JSON-LD: OutOfStock"
                    if "InStock" in avail:
                        return "in_stock", f"JSON-LD: InStock"
            except Exception:
                continue

    except Exception as e:
        return "unknown", f"error: {e}"

    return "unknown", "status not detected"

# ─────────────────────────────────────────
# Slack 알림
# ─────────────────────────────────────────
def send_slack_alert(product_name: str, url: str, event: str) -> bool:
    if not SLACK_WEBHOOK_URL or "YOUR/WEBHOOK" in SLACK_WEBHOOK_URL:
        logger.error("SLACK_WEBHOOK_URL 미설정")
        return False

    emoji = "🔴" if event == "out_of_stock" else "🟢"
    label = "품절" if event == "out_of_stock" else "재입고"

    payload = {
        "channel": SLACK_CHANNEL,
        "text": f"{emoji} *세포라 재고 알림* | {label}",
        "attachments": [{
            "color": "#FF0000" if event == "out_of_stock" else "#36A64F",
            "fields": [
                {"title": "제품명",    "value": product_name, "short": True},
                {"title": "상태",      "value": f"{emoji} {label}", "short": True},
                {"title": "URL",       "value": url, "short": False},
                {"title": "감지 시각", "value": datetime.now().strftime("%Y-%m-%d %H:%M"), "short": True},
            ],
            "footer": "Sephora Monitor",
        }],
    }
    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
        logger.info(f"Slack 전송 성공: [{label}] {product_name}")
        return True
    except Exception as e:
        logger.error(f"Slack 전송 실패: {e}")
        return False

# ─────────────────────────────────────────
# 메인 체크
# ─────────────────────────────────────────
def run_check() -> None:
    logger.info("=" * 50)
    logger.info(f"모니터링 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    products = load_products()
    state    = load_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        page = context.new_page()

        # Stealth 적용
        if STEALTH_MODE == "v1":
            stealth_sync(page)
        elif STEALTH_MODE == "v2":
            Stealth().apply_stealth_sync(page)

        # 자동화 감지 숨기기
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
        """)

        for product in products:
            name = product["product_name"]
            url  = product["url"]

            logger.info(f"체크 중: {name}")
            new_status, reason = check_stock_status(page, url)
            logger.info(f"  → [{new_status}] {reason}")

            prev = state.get(url)

            if new_status == "unknown":
                pass
            elif prev is None:
                logger.info(f"  → 최초 등록 (현재: {new_status})")
            elif prev == "in_stock" and new_status == "out_of_stock":
                logger.info(f"  → 🔴 품절 감지!")
                send_slack_alert(name, url, "out_of_stock")
            elif prev == "out_of_stock" and new_status == "in_stock":
                logger.info(f"  → 🟢 재입고 감지!")
                send_slack_alert(name, url, "back_in_stock")
            else:
                logger.info(f"  → 변화 없음 ({prev})")

            if new_status != "unknown":
                state[url] = new_status

            time.sleep(REQUEST_DELAY_SECONDS)

        browser.close()

    save_state(state)
    logger.info(f"완료. 상태 저장: {STATE_FILE}")

# ─────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()

    if args.loop:
        while True:
            run_check()
            time.sleep(CHECK_INTERVAL_SECONDS)
    else:
        run_check()
