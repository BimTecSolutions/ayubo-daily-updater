"""
Ayubo (NLB) — GitHub Actions updater (built for a 5-minute schedule).

Firestore cost per run:
  * Nothing new published : 1 read, 0 writes
  * Each new draw         : 1 write (create(); never overwrites existing data)

Efficiency:
  * No meta document and no "exists?" reads: the latest draw number comes from
    ONE ordered query (limit 1), which can never go stale.
  * The URL style (host + padded/unpadded) is copied from the latest stored
    document, so a normal run requests ONE page per draw number.
  * Plain HTTP first. A clean 404, or a page that shows a different draw
    number (= this draw is not published yet), never starts Chrome.
    Chrome is capped at MAX_SELENIUM_FETCHES page loads per run.
  * Always starts at latest+1, so a draw can never be skipped. Stops after
    MAX_CONSECUTIVE_MISSES unpublished draws in a row (default 1).

Environment variables (all optional):
  GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_CREDENTIALS   service account JSON path
  START_DRAW_FALLBACK      first draw if Firestore is empty. Default: 1
  MAX_CONSECUTIVE_MISSES   Default: 1
  MAX_NEW_DRAWS_PER_RUN    Default: 20
  MAX_SELENIUM_FETCHES     Default: 3
  ALL_URL_VARIANTS         1 = try every URL variant (slower). Default: 0
  DRY_RUN                  1 = parse and print, but do not write to Firestore
  DEBUG                    1 = print page text when parsing fails
"""

import os
import re
import time
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import AlreadyExists


class AyuboUpdater:
    BASE_URLS = ["https://www.nlb.lk", "https://r.nlb.lk"]

    LOTTERY_SLUG = "ayubo"
    LOTTERY_NAME = "Ayubo"

    FIRESTORE_COLLECTION = "NLB_Results_All"
    FIRESTORE_DOCUMENT = "ayubo"
    FIRESTORE_SUBCOLLECTION = "results"

    HTTP_TIMEOUT = 20
    SELENIUM_MAX_WAIT = 12  # seconds to wait for the result text to render

    def __init__(
        self,
        creds_path: str,
        debug: bool = False,
        dry_run: bool = False,
        max_selenium_fetches: int = 3,
        all_url_variants: bool = False,
    ):
        self.debug = debug
        self.dry_run = dry_run
        self.max_selenium_fetches = max_selenium_fetches
        self.all_url_variants = all_url_variants
        self.selenium_fetches = 0
        self.driver = None
        self.url_base: Optional[str] = None      # learned from latest stored doc
        self.url_padded: bool = True

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nlb.lk/",
            "Connection": "keep-alive",
        })

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(creds_path))
        self.db = firestore.client()
        print("✅ Firebase initialized." + ("  (DRY RUN: no writes)" if dry_run else ""))

    # ------------------------------------------------------------
    # FIREBASE
    # ------------------------------------------------------------
    def results_collection(self):
        return (
            self.db.collection(self.FIRESTORE_COLLECTION)
            .document(self.FIRESTORE_DOCUMENT)
            .collection(self.FIRESTORE_SUBCOLLECTION)
        )

    def get_latest_draw_number(self) -> Optional[int]:
        """ONE read: the stored document with the highest draw_number."""
        docs = list(
            self.results_collection()
            .order_by("draw_number", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        if not docs:
            return None

        data = docs[0].to_dict() or {}
        self._learn_url_style(data.get("url"))

        latest = data.get("draw_number")
        if isinstance(latest, int):
            return latest
        try:
            return int(latest)
        except (TypeError, ValueError):
            return None

    def _learn_url_style(self, url: Optional[str]):
        """Remember host + padded/unpadded style from the latest stored URL."""
        if not url or f"/results/{self.LOTTERY_SLUG}/" not in url:
            return
        base, _, last = url.rstrip("/").rpartition("/")
        if not last.isdigit():
            return
        self.url_base = base
        self.url_padded = len(last) >= 4 and last.startswith("0")

    def upload_new_result(self, result: Dict) -> str:
        """create() = 1 write; fails if the document already exists.
        Returns 'created', 'exists' or 'error'."""
        doc_id = f"{self.LOTTERY_SLUG}_{result['draw_number_str']}"
        if self.dry_run:
            print(f"   🧪 (dry run) would upload {doc_id}")
            return "created"
        try:
            self.results_collection().document(doc_id).create(result)
            print(f"   ☁️ Uploaded: {doc_id}")
            return "created"
        except AlreadyExists:
            print(f"   ℹ️ {doc_id} already exists — left unchanged.")
            return "exists"
        except Exception as e:
            print(f"   ❌ Firestore upload failed: {e}")
            return "error"

    # ------------------------------------------------------------
    # FETCHING (cheap HTTP first, Chrome only when needed)
    # ------------------------------------------------------------
    def fetch_with_requests(self, url: str) -> Tuple[Optional[str], Optional[int]]:
        for attempt in (1, 2):
            try:
                resp = self.session.get(url, timeout=self.HTTP_TIMEOUT, allow_redirects=True)
                if resp.status_code == 200 and len(resp.text) > 2000:
                    return resp.text, 200
                return None, resp.status_code
            except Exception as e:
                print(f"   ⚠️ Request error: {e}")
                if attempt == 1:
                    time.sleep(2)
        return None, None

    def init_selenium(self):
        if self.driver:
            return self.driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(45)
            print("✅ Selenium Chrome initialized.")
            return self.driver
        except Exception as e:
            print(f"❌ Selenium initialization failed: {e}")
            return None

    def fetch_with_selenium(self, url: str) -> Optional[str]:
        if self.selenium_fetches >= self.max_selenium_fetches:
            print("   ⏭️ Chrome fetch limit for this run reached.")
            return None

        driver = self.init_selenium()
        if not driver:
            return None

        self.selenium_fetches += 1
        try:
            driver.get(url)
            deadline = time.time() + self.SELENIUM_MAX_WAIT
            html = ""
            while time.time() < deadline:
                html = driver.page_source
                if re.search(r"Draw\s*No", html, re.IGNORECASE) and "Date" in html:
                    break
                time.sleep(1)
            if len(html) > 2000:
                return html
        except Exception as e:
            print(f"   ⚠️ Selenium error: {e}")
        return None

    # ------------------------------------------------------------
    # TEXT / DATE HELPERS
    # ------------------------------------------------------------
    @staticmethod
    def normalize_text(text: str) -> str:
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def get_page_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return self.normalize_text(soup.get_text(separator=" ", strip=True))

    @staticmethod
    def _parse_date_value(date_string: Optional[str]) -> Optional[datetime]:
        if not date_string:
            return None
        cleaned = re.sub(r"\s+", " ", date_string.strip())
        cleaned = re.sub(
            r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+",
            "", cleaned, flags=re.IGNORECASE,
        )
        # Only the standalone word "Sept" is rewritten ("September" stays intact).
        cleaned = re.sub(r"\bSept\b", "Sep", cleaned).replace(".", "")
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
        return None

    def extract_date(self, text: str) -> Optional[str]:
        # Matches "Date: Monday November 09, 2026"
        m = re.search(
            r"(?<![A-Za-z])Date\s*[:\-]\s*"
            r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?"
            r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})",
            text,
        )
        if m:
            parsed = self._parse_date_value(m.group(1))
            if parsed:
                return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
        return None

    def parse_date(self, date_string: Optional[str]) -> Optional[str]:
        parsed = self._parse_date_value(date_string)
        return parsed.strftime("%Y-%m-%dT00:00:00") if parsed else None

    # ------------------------------------------------------------
    # RESULT PARSING:  <Capital letter> <d> <d> <d> <d>
    # ------------------------------------------------------------
    @staticmethod
    def extract_draw_number(text: str) -> Optional[int]:
        m = re.search(r"Draw\s*No\.?\s*:?\s*0*(\d+)", text, re.IGNORECASE)
        return int(m.group(1)) if m else None

    @staticmethod
    def extract_main_result(text: str) -> Optional[Dict]:
        """Only looks between the first 'Draw No' label and 'Prize Structure',
        so the site menu and prize tables are never mistaken for the result."""
        start = re.search(r"Draw\s*No", text, re.IGNORECASE)
        if not start:
            return None
        block = text[start.start(): start.start() + 400]
        end = re.search(r"Prize\s*Structure", block, re.IGNORECASE)
        if end:
            block = block[: end.start()]

        m = re.search(r"(?<![A-Za-z])([A-Z])\s*(\d)\s*(\d)\s*(\d)\s*(\d)(?!\d)", block)
        if not m:
            return None
        digits = [m.group(i) for i in range(2, 6)]
        return {
            "letter": m.group(1),
            "numbers": digits,
            "numbers_int": [int(d) for d in digits],
        }

    # ------------------------------------------------------------
    # SCRAPE ONE DRAW
    # ------------------------------------------------------------
    def try_parse(self, html: str, url: str, draw_number: int) -> Tuple[Optional[Dict], str, bool]:
        """Returns (result, page_text, other_draw).
        other_draw=True means the page is valid but shows a DIFFERENT draw,
        i.e. the requested draw is not published yet (no Chrome retry needed)."""
        text = self.get_page_text(html)
        if len(text) < 300:
            return None, text, False

        page_draw = self.extract_draw_number(text)
        if page_draw is not None and page_draw != draw_number:
            return None, text, True

        main_result = self.extract_main_result(text)
        if not main_result:
            return None, text, False

        date = self.extract_date(text)
        date_iso = self.parse_date(date)
        if not date or not date_iso:
            print(f"   ⚠️ Draw {draw_number:04d}: explicit draw date not found; not uploading.")
            return None, text, False

        result = {
            "lottery": self.LOTTERY_NAME,
            "lottery_slug": self.LOTTERY_SLUG,
            "draw_number": draw_number,
            "draw_number_str": f"{draw_number:04d}",
            "date": date,
            "date_iso": date_iso,
            "main_result": main_result,
            "url": url,
            "scraped_at": datetime.now().isoformat(),
            "source": "National Lotteries Board, Sri Lanka",
        }
        return result, text, False

    def candidate_urls(self, draw_number: int) -> List[str]:
        padded, plain = f"{draw_number:04d}", str(draw_number)

        # Normal case: exactly one URL, in the style the latest doc used.
        if self.url_base and not self.all_url_variants:
            return [f"{self.url_base}/{padded if self.url_padded else plain}"]

        urls = []
        for base in self.BASE_URLS:
            for value in (padded, plain):
                urls.append(f"{base}/results/{self.LOTTERY_SLUG}/{value}")
        return list(dict.fromkeys(urls))

    def scrape_draw(self, draw_number: int) -> Optional[Dict]:
        draw_padded = f"{draw_number:04d}"
        print(f"\n🔎 Checking draw {draw_padded}...")

        last_text = ""

        for url in self.candidate_urls(draw_number):
            html, status = self.fetch_with_requests(url)

            if status == 404:          # clean "not published" — skip Chrome
                continue

            if html:
                result, text, other_draw = self.try_parse(html, url, draw_number)
                last_text = text or last_text
                if result:
                    return self._found(result)
                if other_draw:         # page shows another draw — not published yet
                    continue

            html = self.fetch_with_selenium(url)
            if html:
                result, text, other_draw = self.try_parse(html, url, draw_number)
                last_text = text or last_text
                if result:
                    return self._found(result)

        print(f"   ⛔ Draw {draw_padded} not available (or could not be parsed).")
        if self.debug and last_text:
            print("   --- DEBUG page text (first 3000 chars) ---")
            print(last_text[:3000])
            print("   --- END DEBUG ---")
        return None

    @staticmethod
    def _found(result: Dict) -> Dict:
        m = result["main_result"]
        print(f"   ✅ Found draw {result['draw_number_str']}: "
              f"{m['letter']} {' '.join(m['numbers'])}  ({result['date']})")
        return result

    # ------------------------------------------------------------
    # INCREMENTAL UPDATE
    # ------------------------------------------------------------
    def run(self, start_fallback: int, max_misses: int, max_new: int) -> Tuple[int, int]:
        latest = self.get_latest_draw_number()

        if latest is None:
            print(f"📭 No results stored. Starting from {start_fallback:04d}.")
            next_draw = start_fallback
        else:
            print(f"📌 Latest draw in Firestore: {latest:04d}")
            next_draw = latest + 1

        uploaded = errors = misses = attempts = 0

        while misses < max_misses and attempts < max_new:
            attempts += 1
            result = self.scrape_draw(next_draw)

            if result:
                outcome = self.upload_new_result(result)
                if outcome == "created":
                    uploaded += 1
                elif outcome == "error":
                    errors += 1
                    break          # don't skip past a draw that failed to save
                misses = 0
            else:
                misses += 1

            next_draw += 1

        if uploaded == 0 and errors == 0:
            print("\n✅ No new draws — already up to date.")
        elif uploaded:
            print(f"\n🎉 Uploaded {uploaded} new draw(s).")
        return uploaded, errors

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
                print("🛑 Selenium Chrome closed.")
            except Exception:
                pass
            self.driver = None


def main():
    creds_path = (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("FIREBASE_CREDENTIALS")
        or "serviceAccountKey.json"
    )
    if not os.path.exists(creds_path):
        print(f"❌ Credentials file not found: {creds_path}")
        raise SystemExit(1)

    updater = None
    errors = 0
    try:
        updater = AyuboUpdater(
            creds_path,
            debug=os.environ.get("DEBUG", "0") == "1",
            dry_run=os.environ.get("DRY_RUN", "0") == "1",
            max_selenium_fetches=int(os.environ.get("MAX_SELENIUM_FETCHES", "3")),
            all_url_variants=os.environ.get("ALL_URL_VARIANTS", "0") == "1",
        )
        _, errors = updater.run(
            start_fallback=int(os.environ.get("START_DRAW_FALLBACK", "1")),
            max_misses=int(os.environ.get("MAX_CONSECUTIVE_MISSES", "1")),
            max_new=int(os.environ.get("MAX_NEW_DRAWS_PER_RUN", "20")),
        )
    finally:
        if updater:
            updater.close()

    if errors:
        raise SystemExit(1)   # makes the GitHub Actions run show as failed


if __name__ == "__main__":
    main()
