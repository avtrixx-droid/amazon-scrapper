"""
session_bootstrap.py — one-time browser session to set the batch pincode and
harvest cookies for replay over plain HTTP.

This is Phase 0 of the build plan: the whole "plain HTTP vs headless browser"
decision (spec section 4) hinges on whether the cookies this captures still
carry the delivery location when replayed by httpx with no browser attached.

*** THIS HAS NOT BEEN VALIDATED AGAINST LIVE AMAZON. ***
The sandbox this was built in has outbound network access to amazon.in
blocked at the proxy level (policy denial, confirmed via a direct CONNECT
test — not a code bug, not a timeout). `bootstrap_session()` below is
therefore unit-testable only for its control flow (see
tests/test_session_bootstrap.py), not for whether Amazon actually accepts
the cookies it produces. Run `python -m price_verifier.fetcher.session_bootstrap
<pincode> <city>` on a machine with real internet access as the literal first
validation step — see README.md "Phase 0" for the full spike procedure.

The click sequence below is not a guess: it's a direct port of this repo's
own scraper.py `set_pincode()` (scraper.py ~line 862), which IS proven
against live amazon.in across many production runs. Only the "harvest
cookies afterward" part is new.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass

logger = logging.getLogger("price_verifier.session_bootstrap")

try:
    import undetected_chromedriver as uc
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    _UC_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where UC isn't installed
    _UC_AVAILABLE = False


@dataclass
class BootstrappedSession:
    cookies: dict[str, str]
    user_agent: str
    pincode: str
    city: str
    captured_at: float


class BootstrapError(Exception):
    pass


def bootstrap_session(pincode: str, city: str, headless: bool = True, timeout: int = 20) -> BootstrappedSession:
    """Launch a real Chrome session, set the delivery pincode via the same UI
    flow scraper.py already trusts, then return the cookie jar + UA for
    replay. Raises BootstrapError on any failure (never returns a half-set
    session — a pipeline running on an unset pincode would silently produce
    wrong-location prices for every row, which is worse than failing loud
    here)."""
    if not _UC_AVAILABLE:
        raise BootstrapError(
            "undetected-chromedriver / selenium not installed. "
            "Install with: pip install -r price_verifier/requirements.txt"
        )

    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=en-IN")
    options.add_argument("--disable-blink-features=AutomationControlled")

    driver = None
    try:
        driver = uc.Chrome(options=options)
        driver.set_page_load_timeout(timeout)
        driver.get("https://www.amazon.in/")

        wait = WebDriverWait(driver, 10)
        applied = False
        for attempt in range(1, 4):
            try:
                for _ in range(2):
                    try:
                        loc_btn = wait.until(
                            EC.element_to_be_clickable((By.ID, "nav-global-location-popover-link"))
                        )
                        loc_btn.click()
                        break
                    except TimeoutException:
                        continue

                wait.until(EC.presence_of_element_located((By.ID, "GLUXZipUpdateInput")))
                inp = driver.find_element(By.ID, "GLUXZipUpdateInput")
                inp.click()
                inp.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
                inp.send_keys(Keys.BACKSPACE)
                for ch in pincode:
                    inp.send_keys(ch)
                    time.sleep(0.1)

                for sel in [
                    (By.CSS_SELECTOR, "#GLUXZipUpdate .a-button-input"),
                    (By.CSS_SELECTOR, "#GLUXZipUpdate input[type='submit']"),
                    (By.ID, "GLUXZipUpdate"),
                ]:
                    try:
                        driver.find_element(*sel).click()
                        applied = True
                        break
                    except Exception:
                        continue
                if not applied:
                    continue

                try:
                    time.sleep(0.5)
                    driver.find_element(By.CSS_SELECTOR, "#GLUXConfirmClose").click()
                except Exception:
                    pass

                time.sleep(1.5)
                nav_text = ""
                try:
                    nav_text = driver.find_element(By.ID, "glow-ingress-line2").text.strip()
                except Exception:
                    pass

                if city.lower() in nav_text.lower() or pincode in nav_text:
                    break
                applied = False
            except Exception:
                logger.exception("Pincode set attempt %d failed", attempt)
            time.sleep(2.0)

        if not applied:
            raise BootstrapError(f"Could not confirm pincode {pincode} ({city}) after 3 attempts")

        raw_cookies = driver.get_cookies()
        user_agent = driver.execute_script("return navigator.userAgent")
        cookies = {c["name"]: c["value"] for c in raw_cookies}
        return BootstrappedSession(cookies=cookies, user_agent=user_agent, pincode=pincode, city=city, captured_at=time.time())
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":  # pragma: no cover - manual spike entry point
    if len(sys.argv) < 3:
        print("Usage: python -m price_verifier.fetcher.session_bootstrap <pincode> <city>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO)
    session = bootstrap_session(sys.argv[1], sys.argv[2])
    print(f"Captured {len(session.cookies)} cookies for {session.pincode} ({session.city})")
    print(f"User-Agent: {session.user_agent}")
