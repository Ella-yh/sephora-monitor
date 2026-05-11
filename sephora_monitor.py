"""
sephora_monitor.py - 세포라 품절/재입고 모니터링 → Slack 알림
Playwright Stealth 버전 (봇 감지 우회)

사용법:
  python sephora_monitor.py        # 1회 실행
  python sephora_monitor.py --loop # 반복 실행
"""

import csv, json, logging, os, time, argparse
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# ─────────────────────────────────────────
# 설정 로드
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
if not STEALTH_AVAILABLE:
    logger.warning("playwright-stealth 미설치. 'pip install playwright-stealth' 권장")

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
        reader = csv.DictReader(f)
        for row in reader:
            url  = row.get("url", "").strip().strip("\r\n").strip()
            name = row.get("product_name", "").strip()
            if url and name:
                products.append({"product_name": name, "url": url})
    logger.info(f"제품 {len(products)}개 로드 완료")
    return products

# ─────────────────────────────────────────
# 품절 감지
# ─────────────────────────────────────────
def check_stock_status(page, url: str) -> tuple:
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)
    except PlaywrightTimeout:
        # networkidle 타임아웃은 종종 정상 - domcontentloaded로 재시도
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
        except Exception as e:
            return "unknown", f"timeout: {e}"
    except Exception as e:
        return "unknown", str(e)

    # 페이지 내용 확인
    try:
        body_text = page.inner_text("body").strip()
        if len(body_text) < 100:
            return "unknown", f"empty page (len={len(body_text)})"
    except Exception:
        pass

    # ── 1순위: OUT OF STOCK 뱃지 텍스트 ──────────────────────────
    try:
        full_text = page.inner_text("body").lower()
        if "out of stock" in full_text:
            # Add to Basket도 있으면 → 다른 사이즈가 재고 있음
            # URL에 skuId가 있으면 해당 사이즈만 체크
            if "?skuid=" in url.lower() or "?skupid=" in url.lower():
                # 특정 사이즈 URL → 그 사이즈의 상태만 신뢰
                # 선택된 버튼 근처의 OOS 여부 확인
                size_area = page.query_selector("[class*='VariantPicker'], [class*='variantPicker'], [data-comp*='VariantSelect']")
                if size_area:
                    size_text = (size_area.inner_text() or "").lower()
                    if "out of stock" in size_text:
                        return "out_of_stock", "size area: out of stock"
                # skuId URL에서 페이지 상단 뱃지 확인
                badge = page.query_selector("[class*='badge'][class*='outOfStock'], [class*='Badge'][class*='OutOfStock']")
                if badge:
                    return "out_of_stock", f"badge: {badge.inner_text()[:40]}"
                # "Size: X oz | OUT OF STOCK" 패턴
                if "size:" in full_text and "out of stock" in full_text:
                    return "out_of_stock", "size label: out of stock"
            else:
                # 기본 URL → 현재 보이는 사이즈 기준
                if "size:" in full_text and "out of stock" in full_text:
                    return "out_of_stock", "size label: out of stock"
    except Exception as e:
        logger.debug(f"텍스트 탐지 오류: {e}")

    # ── 2순위: JSON-LD ────────────────────────────────────────────
    try:
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
                        return "out_of_stock", f"JSON-LD: {avail}"
                    if "InStock" in avail:
                        return "in_stock", f"JSON-LD: {avail}"
            except Exception:
                continue
    except Exception:
        pass

    # ── 3순위: Add to Basket 버튼 상태 ───────────────────────────
    try:
        add_btn = page.query_selector(
            "button:has-text('Add to Basket'), "
            "button:has-text('Add to Bag'), "
            "button:has-text('Add to Cart')"
        )
        if add_btn:
            disabled = add_btn.get_attribute("disabled")
            aria_disabled = add_btn.get_attribute("aria-disabled")
            if disabled is not None or aria_disabled == "true":
                return "out_of_stock", "Add to Basket disabled"
            return "in_stock", "Add to Basket enabled"
    except Exception:
        pass

    # ── 4순위: 전체 텍스트 fallback ──────────────────────────────
    try:
        full_text = page.inner_text("body").lower()
        if "out of stock" in full_text:
            return "out_of_stock", "page text: out of stock"
        if "add to basket" in full_text or "add to bag" in full_text:
            return "in_stock", "page text: add to basket"
    except Exception:
        pass

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
        logger.info(f"Slack 알림 전송: [{label}] {product_name}")
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
        browser = p.chromium.launch(headless=True)
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

        # stealth 적용
        if STEALTH_AVAILABLE:
            stealth_sync(page)
            logger.info("Stealth 모드 활성화")

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
                logger.info(f"  → 🔴 품절 감지! Slack 전송")
                send_slack_alert(name, url, "out_of_stock")
            elif prev == "out_of_stock" and new_status == "in_stock":
                logger.info(f"  → 🟢 재입고 감지! Slack 전송")
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
