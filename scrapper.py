from __future__ import annotations

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from webdriver_manager.chrome import ChromeDriverManager

from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

import pandas as pd
import time
import random
import re
import sys
import os
import logging

# =============================
# Setup
# =============================
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "emlakjet.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True
)

logger = logging.getLogger(__name__)
os.environ["WDM_LOG"] = "0"

# =============================
# Utilities
# =============================
def clean_price(text: Optional[str]) -> Optional[float]:
    """Extract numeric price from text."""
    if not text:
        return None
    txt = re.sub(r"[^\d.,]", "", text)
    
    if "," in txt and "." in txt:
        if txt.rfind(",") > txt.rfind("."):
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", "")
    elif "," in txt:
        txt = txt.replace(",", ".")
    
    try:
        return float(txt)
    except ValueError:
        return None


def extract_property_details(text: str) -> Dict[str, Optional[str]]:
    """Extract room count, floor, and area from property details text."""
    details = {
        "Oda_Sayisi": None,
        "Kat": None,
        "Brut_Alan_m2": None
    }
    
    if not text:
        return details
    
    text_lower = text.lower()
    
    # Extract room count (e.g., "3+1", "2+1", "1+0")
    room_match = re.search(r"(\d+)\s*\+\s*(\d+)", text_lower)
    if room_match:
        details["Oda_Sayisi"] = f"{room_match.group(1)}+{room_match.group(2)}"
    
    # Extract area (e.g., "120 m²", "85m2", "150 m2")
    area_match = re.search(r"(\d{2,4})\s*m[²2]", text_lower)
    if area_match:
        details["Brut_Alan_m2"] = area_match.group(1)
    
    # Extract floor (e.g., "5. Kat", "Zemin Kat", "3.Kat")
    floor_match = re.search(r"(\d+)\s*\.?\s*kat", text_lower)
    if floor_match:
        details["Kat"] = floor_match.group(1)
    elif any(word in text_lower for word in ["zemin", "giriş"]):
        details["Kat"] = "Zemin"
    elif "çatı" in text_lower or "tavan" in text_lower:
        details["Kat"] = "Çatı"
    
    return details


@contextmanager
def get_driver(headless: bool = True):
    """Create and manage Chrome WebDriver."""
    options = Options()
    
    if headless:
        options.add_argument("--headless=new")
    
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
    
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    
    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.images": 2,
    }
    options.add_experimental_option("prefs", prefs)
    
    driver = None
    try:
        print("🔧 Initializing Chrome...", flush=True)
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        
        # Anti-detection
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        
        print("✅ Chrome ready\n", flush=True)
        yield driver
        
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass


# =============================
# Scraper
# =============================
class EmlakjetScraper:
    
    BASE_URL = "https://www.emlakjet.com"
    START_PATH = "/kiralik-daire/istanbul/"
    
    def __init__(self, max_pages: int = 10, headless: bool = True, debug: bool = False):
        self.max_pages = max_pages
        self.headless = headless
        self.debug = debug
        self.data: List[Dict[str, Any]] = []
    
    def find_selector(self, driver, selectors: List[str], description: str = "elements"):
        """Try multiple selectors and return the one that works."""
        for sel in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                if elements:
                    print(f"   ✅ Found {len(elements)} {description} using: {sel}", flush=True)
                    return sel, elements
            except:
                continue
        
        print(f"   ❌ No {description} found with any selector", flush=True)
        return None, []
    
    def debug_page(self, driver):
        """Debug mode: inspect page structure and save screenshot."""
        print("\n🔍 DEBUG MODE - Analyzing page structure...\n", flush=True)
        
        # Save page source
        with open("page_source.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print("💾 Page source saved to page_source.html", flush=True)
        
        # Save screenshot
        driver.save_screenshot("page_screenshot.png")
        print("📸 Screenshot saved to page_screenshot.png", flush=True)
        
        # Try to find ANY divs or articles
        print("\n📊 Looking for common elements:", flush=True)
        
        for tag in ["div", "article", "section", "li"]:
            elements = driver.find_elements(By.TAG_NAME, tag)
            print(f"   {tag}: {len(elements)} found", flush=True)
        
        # Look for data-testid attributes
        testid_els = driver.find_elements(By.CSS_SELECTOR, "[data-testid]")
        if testid_els:
            print(f"\n🏷️  Found {len(testid_els)} elements with data-testid:", flush=True)
            testids = set()
            for el in testid_els[:20]:
                tid = el.get_attribute("data-testid")
                if tid:
                    testids.add(tid)
            for tid in sorted(testids)[:10]:
                print(f"   - {tid}", flush=True)
        
        # Look for class names containing common keywords
        print("\n🎨 Looking for class names with keywords:", flush=True)
        keywords = ["card", "listing", "property", "item", "price", "location"]
        for keyword in keywords:
            els = driver.find_elements(By.CSS_SELECTOR, f"[class*='{keyword}']")
            if els:
                print(f"   {keyword}: {len(els)} elements", flush=True)
        
        print("\n⏸️  Check the saved files and press ENTER to continue...", flush=True)
        input()
    
    def scrape_page(self, driver, page_num: int):
        """Scrape a single page."""
        url = f"{self.BASE_URL}{self.START_PATH}{page_num}"
        
        print(f"\n{'='*60}", flush=True)
        print(f"📄 PAGE {page_num} - {url}", flush=True)
        print(f"{'='*60}", flush=True)
        
        try:
            print("   ⏳ Loading page...", flush=True)
            driver.get(url)
            time.sleep(3 + random.uniform(1, 2))
            
            print(f"   📌 Current URL: {driver.current_url}", flush=True)
            print(f"   📌 Page title: {driver.title}", flush=True)
            
            # Debug mode on first page
            if self.debug and page_num == 1:
                self.debug_page(driver)
            
            # Wait for page to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # Try multiple selectors for property cards
            card_selectors = [
                "div[data-testid='listing-card']",
                "article[data-testid='listing-item']",
                "div[class*='ListingCard']",
                "div[class*='PropertyCard']",
                "div[class*='listing']",
                "article[class*='property']",
                "div[class*='card']",
                "article",
                "li[class*='item']",
            ]
            
            print("   🔍 Searching for property cards...", flush=True)
            card_sel, cards = self.find_selector(driver, card_selectors, "property cards")
            
            if not cards or len(cards) < 3:
                print(f"   ⚠️  Only found {len(cards)} cards, trying scroll...", flush=True)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                time.sleep(2)
                _, cards = self.find_selector(driver, card_selectors, "property cards after scroll")
            
            if not cards:
                print("   ❌ No property cards found on this page", flush=True)
                return 0
            
            print(f"   ✅ Processing {len(cards)} cards...", flush=True)
            
            # Process each card
            count = 0
            for i, card in enumerate(cards):
                try:
                    # Try to find price
                    price_selectors = [
                        "[data-testid='price']",
                        ".price",
                        "[class*='price']",
                        "[class*='Price']",
                    ]
                    
                    price_text = None
                    for sel in price_selectors:
                        try:
                            price_el = card.find_element(By.CSS_SELECTOR, sel)
                            if price_el and price_el.text.strip():
                                price_text = price_el.text.strip()
                                break
                        except:
                            continue
                    
                    if not price_text:
                        continue
                    
                    # Try to find location
                    loc_selectors = [
                        "[data-testid='location']",
                        ".location",
                        "[class*='location']",
                        "[class*='address']",
                    ]
                    
                    location = ""
                    for sel in loc_selectors:
                        try:
                            loc_el = card.find_element(By.CSS_SELECTOR, sel)
                            if loc_el and loc_el.text.strip():
                                location = loc_el.text.strip()
                                break
                        except:
                            continue
                    
                    # Try to find details (room, floor, area)
                    details_text = ""
                    try:
                        # Try to find details container
                        details_selectors = [
                            "[data-testid='property-features']",
                            "ul",
                            "[class*='features']",
                            "[class*='details']",
                        ]
                        
                        for sel in details_selectors:
                            try:
                                details_el = card.find_element(By.CSS_SELECTOR, sel)
                                if details_el and details_el.text.strip():
                                    # Try to get individual list items
                                    try:
                                        items = details_el.find_elements(By.TAG_NAME, "li")
                                        if items:
                                            details_text = " | ".join([item.text.strip() for item in items if item.text.strip()])
                                        else:
                                            details_text = details_el.text.strip()
                                    except:
                                        details_text = details_el.text.strip()
                                    break
                            except:
                                continue
                        
                        # If no details found in ul/li, try to get all text from card
                        if not details_text:
                            details_text = card.text
                    except:
                        details_text = ""
                    
                    # Extract structured details from text
                    property_details = extract_property_details(details_text)
                    
                    # Try to find URL
                    url_link = ""
                    try:
                        link = card.find_element(By.TAG_NAME, "a")
                        url_link = link.get_attribute("href") or ""
                    except:
                        pass
                    
                    # Split location into district and neighborhood
                    semt = ""
                    mahalle = ""
                    if location:
                        parts = [p.strip() for p in location.split(",")]
                        if len(parts) >= 2:
                            semt = parts[0]
                            mahalle = parts[1]
                        elif len(parts) == 1:
                            semt = parts[0]
                    
                    data = {
                        "Tarih": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "Fiyat": price_text,
                        "Fiyat_Sayisal": clean_price(price_text),
                        "Konum": location,
                        "Semt": semt,
                        "Mahalle": mahalle,
                        "Oda_Sayisi": property_details["Oda_Sayisi"],
                        "Kat": property_details["Kat"],
                        "Brut_Alan_m2": property_details["Brut_Alan_m2"],
                        "Detaylar": details_text,
                        "URL": url_link,
                    }
                    
                    self.data.append(data)
                    count += 1
                    
                    if (i + 1) % 10 == 0:
                        print(f"   ⚙️  Processed {i+1}/{len(cards)}...", flush=True)
                
                except Exception as e:
                    if self.debug:
                        print(f"   ⚠️  Error on card {i}: {e}", flush=True)
                    continue
            
            print(f"   ✅ Successfully extracted {count} listings", flush=True)
            return count
            
        except TimeoutException:
            print("   ❌ Page load timeout", flush=True)
            return 0
        except Exception as e:
            print(f"   ❌ Error: {e}", flush=True)
            if self.debug:
                import traceback
                traceback.print_exc()
            return 0
    
    def run(self):
        """Main scraping loop."""
        print("\n" + "="*60, flush=True)
        print("🚀 EMLAKJET SCRAPER", flush=True)
        print("="*60, flush=True)
        print(f"📊 Configuration:", flush=True)
        print(f"   Pages: {self.max_pages}", flush=True)
        print(f"   Mode: {'VISIBLE' if not self.headless else 'HEADLESS'}", flush=True)
        print(f"   Debug: {'ON' if self.debug else 'OFF'}", flush=True)
        print("="*60 + "\n", flush=True)
        
        with get_driver(self.headless) as driver:
            total = 0
            for page in range(1, self.max_pages + 1):
                count = self.scrape_page(driver, page)
                total += count
                print(f"\n📈 Total so far: {total} listings\n", flush=True)
                time.sleep(random.uniform(2, 4))
        
        # Save results
        if self.data:
            df = pd.DataFrame(self.data)
            
            # Convert Brut_Alan_m2 to numeric
            df["Brut_Alan_m2_Numeric"] = pd.to_numeric(df["Brut_Alan_m2"], errors="coerce")
            
            # Calculate price per m2
            df["Fiyat_Per_m2"] = df.apply(
                lambda row: round(row["Fiyat_Sayisal"] / row["Brut_Alan_m2_Numeric"], 2)
                if pd.notna(row["Fiyat_Sayisal"]) and pd.notna(row["Brut_Alan_m2_Numeric"]) and row["Brut_Alan_m2_Numeric"] > 0
                else None,
                axis=1
            )
            
            # Drop duplicates
            df = df.drop_duplicates(subset=["Fiyat", "Konum", "Oda_Sayisi"], keep="first")
            
            # Reorder columns for better readability
            column_order = [
                "Tarih", "Fiyat", "Fiyat_Sayisal", "Fiyat_Per_m2",
                "Semt", "Mahalle", "Konum",
                "Oda_Sayisi", "Brut_Alan_m2", "Brut_Alan_m2_Numeric", "Kat",
                "Detaylar", "URL"
            ]
            existing_cols = [col for col in column_order if col in df.columns]
            other_cols = [col for col in df.columns if col not in column_order]
            df = df[existing_cols + other_cols]
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"emlakjet_{timestamp}.csv"
            df.to_csv(filename, index=False, encoding="utf-8-sig")
            
            print("\n" + "="*60, flush=True)
            print("✅ SCRAPING COMPLETE", flush=True)
            print("="*60, flush=True)
            print(f"📊 Total listings: {len(df)}", flush=True)
            print(f"🏠 Unique listings: {df.drop_duplicates(subset=['URL']).shape[0] if 'URL' in df.columns else 'N/A'}", flush=True)
            
            if "Fiyat_Sayisal" in df.columns:
                valid_prices = df["Fiyat_Sayisal"].dropna()
                if len(valid_prices) > 0:
                    print(f"💰 Price range: {valid_prices.min():,.0f} - {valid_prices.max():,.0f} TL", flush=True)
                    print(f"💰 Average: {valid_prices.mean():,.0f} TL", flush=True)
                    print(f"💰 Median: {valid_prices.median():,.0f} TL", flush=True)
            
            if "Brut_Alan_m2_Numeric" in df.columns:
                valid_area = df["Brut_Alan_m2_Numeric"].dropna()
                if len(valid_area) > 0:
                    print(f"📐 Area range: {valid_area.min():.0f} - {valid_area.max():.0f} m²", flush=True)
                    print(f"📐 Average area: {valid_area.mean():.0f} m²", flush=True)
            
            if "Fiyat_Per_m2" in df.columns:
                valid_per_m2 = df["Fiyat_Per_m2"].dropna()
                if len(valid_per_m2) > 0:
                    print(f"💵 Average price/m²: {valid_per_m2.mean():,.0f} TL/m²", flush=True)
            
            if "Oda_Sayisi" in df.columns:
                room_counts = df["Oda_Sayisi"].value_counts().head(5)
                if len(room_counts) > 0:
                    print(f"🚪 Room distribution: {', '.join([f'{k}: {v}' for k, v in room_counts.items()])}", flush=True)
            
            if "Kat" in df.columns:
                floor_counts = df["Kat"].value_counts().head(5)
                if len(floor_counts) > 0:
                    print(f"🏢 Floor distribution: {', '.join([f'{k}: {v}' for k, v in floor_counts.items()])}", flush=True)
            
            if "Semt" in df.columns:
                district_counts = df["Semt"].value_counts().head(5)
                if len(district_counts) > 0:
                    print(f"📍 Top districts: {', '.join([f'{k} ({v})' for k, v in district_counts.items()])}", flush=True)
            
            print(f"💾 Saved to: {filename}", flush=True)
            print("="*60 + "\n", flush=True)
        else:
            print("\n❌ No data collected!\n", flush=True)


# =============================
# CLI
# =============================
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Emlakjet Property Scraper")
    parser.add_argument("--pages", "-p", type=int, default=10, help="Number of pages (default: 10)")
    parser.add_argument("--visible", "-v", action="store_true", help="Show browser (not headless)")
    parser.add_argument("--debug", "-d", action="store_true", help="Debug mode (analyze page structure)")
    parser.add_argument("--test", "-t", action="store_true", help="Test mode (1 page, visible, debug)")
    
    args = parser.parse_args()
    
    # Test mode overrides
    if args.test:
        print("\n🧪 TEST MODE ENABLED\n", flush=True)
        args.pages = 1
        args.visible = True
        args.debug = True
    
    scraper = EmlakjetScraper(
        max_pages=args.pages,
        headless=not args.visible,
        debug=args.debug
    )
    
    try:
        scraper.run()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user\n", flush=True)
        if scraper.data:
            df = pd.DataFrame(scraper.data)
            filename = f"emlakjet_partial_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df.to_csv(filename, index=False, encoding="utf-8-sig")
            print(f"💾 Partial data saved to: {filename}\n", flush=True)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}\n", flush=True)
        import traceback
        traceback.print_exc()