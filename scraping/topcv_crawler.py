"""TopCV crawler"""

import json
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from scraping.base_crawler import BaseCrawler
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Default base URL and listing path for TopCV
BASE_URL = "https://www.topcv.vn"
LISTING_PATH = "/tim-viec-lam-cong-nghe-thong-tin-cr257"


class TopCVCrawler(BaseCrawler):
    """Crawler for TopCV (topcv.vn) IT job listings.

    Uses Selenium with a real browser to bypass Cloudflare.
    Tries Edge first (available on all Windows machines), then Chrome.

    Parameters
    ----------
    base_url : str
        Root URL of the TopCV site.
    delay : float
        Minimum seconds between requests to the same domain.
    max_retries : int
        Maximum retry attempts for transient network errors.
    headless : bool
        Run browser in headless mode (no visible window).
        Set to False if Cloudflare still blocks headless mode.
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        delay: float = 3.0,
        max_retries: int = 5,
        headless: bool = True,
    ):
        super().__init__(source_name="topcv", delay=delay, max_retries=max_retries)
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self._driver = None

    # ------------------------------------------------------------------
    # Browser management
    # ------------------------------------------------------------------

    def _init_driver(self):
        if self._driver is not None:
            return

        # Try nodriver
        try:
            import nodriver
            import asyncio

            async def _start_browser():
                browser = await nodriver.start(headless=self.headless)
                return browser

            # Store reference to event loop and browser
            self._use_nodriver = True
            self._loop = asyncio.new_event_loop()
            self._browser = self._loop.run_until_complete(_start_browser())
            self._driver = True 
            logger.info("nodriver browser initialized (headless=%s)", self.headless)
            return
        except Exception as exc:
            logger.warning("nodriver failed: %s — trying Selenium Edge", exc)
            self._use_nodriver = False

        # Fallback to Selenium Edge
        try:
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options as EdgeOptions

            options = EdgeOptions()
            if self.headless:
                options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--window-size=1920,1080")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            self._driver = webdriver.Edge(options=options)
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
            self._use_nodriver = False
            logger.info("Selenium Edge driver initialized (headless=%s)", self.headless)
        except Exception as exc:
            logger.error("All browser init methods failed: %s", exc)
            raise RuntimeError("Could not initialize any browser driver.") from exc

    def _quit_driver(self):
        """Safely close the browser."""
        if self._use_nodriver and hasattr(self, '_browser') and self._browser:
            try:
                import asyncio
                self._loop.run_until_complete(self._browser.stop())
                self._loop.close()
            except Exception:
                pass
            self._browser = None
        elif self._driver and not self._use_nodriver:
            try:
                self._driver.quit()
            except Exception:
                pass
        self._driver = None

    def _fetch(self, url: str, wait_for_js: bool = True) -> Optional[str]:
        """Fetch a URL using the browser."""
        self._wait_for_delay(url)

        if self._use_nodriver:
            return self._fetch_nodriver(url)
        else:
            return self._fetch_selenium(url)

    def _fetch_nodriver(self, url: str) -> Optional[str]:
        """Fetch using nodriver."""
        import asyncio

        async def _get_page(url):
            tab = await self._browser.get(url)
            # Wait for page to fully load
            await asyncio.sleep(5)
            # Get page source
            source = await tab.get_content()
            return source

        for attempt in range(1, self.max_retries + 1):
            try:
                page_source = self._loop.run_until_complete(_get_page(url))

                if not page_source:
                    continue

                if "Just a moment" in page_source or "Attention Required" in page_source:
                    logger.warning("Cloudflare challenge for %s — waiting", url)
                    import asyncio as aio
                    self._loop.run_until_complete(aio.sleep(10))
                    page_source = self._loop.run_until_complete(_get_page(url))

                return page_source

            except Exception as exc:
                logger.error("nodriver error (attempt %d/%d) for %s: %s",
                             attempt, self.max_retries, url, str(exc)[:200])
                if attempt < self.max_retries:
                    time.sleep(3)

        return None

    def _fetch_selenium(self, url: str) -> Optional[str]:
        """Fetch using Selenium with session recovery."""
        for attempt in range(1, self.max_retries + 1):
            try:
                # Check if driver is still alive
                try:
                    _ = self._driver.title
                except Exception:
                    logger.warning("Driver session dead — reinitializing")
                    self._quit_driver()
                    self._init_driver()
                    time.sleep(2)

                self._driver.get(url)
                time.sleep(4)

                page_source = self._driver.page_source
                if "Attention Required" in page_source or "Just a moment" in page_source:
                    logger.warning("Cloudflare challenge for %s — waiting", url)
                    time.sleep(12)
                    page_source = self._driver.page_source
                    if "Attention Required" in page_source or "Just a moment" in page_source:
                        if attempt < self.max_retries:
                            continue
                        return None

                # Wait for JS content
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                try:
                    WebDriverWait(self._driver, 10).until(
                        lambda d: (
                            d.find_elements(By.CSS_SELECTOR, "a[href*='/viec-lam/']")
                            or d.find_elements(By.CSS_SELECTOR, ".job-detail__info--title")
                        )
                    )
                except Exception:
                    pass

                return self._driver.page_source

            except Exception as exc:
                exc_msg = str(exc)
                if "invalid session id" in exc_msg or "session deleted" in exc_msg:
                    logger.warning("Session crashed — reinitializing")
                    self._quit_driver()
                    self._init_driver()
                    time.sleep(3)
                    continue
                else:
                    logger.error("Browser error (attempt %d/%d): %s",
                                 attempt, self.max_retries, exc_msg[:200])
                    if attempt < self.max_retries:
                        time.sleep(2 ** attempt)

        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(
        self,
        max_pages: Optional[int] = None,
        start_page: int = 1,
        skip_urls: Optional[set] = None,
    ) -> List[dict]:
        """Crawl TopCV IT job listings.

        Parameters
        ----------
        max_pages : int | None
            Last listing page to crawl (inclusive). None means
            crawl until no more pages are found.
        start_page : int
            First listing page to crawl, for resuming an earlier run.
        skip_urls : set | None
            Detail URLs already saved by an earlier run; they are not
            fetched again.

        Returns
        -------
        list[dict]
            List of Job_Record dictionaries with all 16 fields.
        """
        self._start_session()
        records: List[dict] = []

        try:
            self._init_driver()
        except RuntimeError as exc:
            logger.error("Cannot start browser: %s", exc)
            self._end_session()
            return records

        page = max(1, start_page)
        skip_urls = set(skip_urls or ())
        seen_urls: set = set()
        stale_pages = 0
        try:
            while max_pages is None or page <= max_pages:
                page_url = f"{self.base_url}{LISTING_PATH}?page={page}"
                logger.info("Crawling TopCV listing page %d: %s", page, page_url)

                html = self._fetch(page_url)
                if html is None:
                    logger.warning("Failed to fetch listing page %d — stopping", page)
                    break

                job_urls = self._parse_listing_page(html)
                if not job_urls:
                    logger.info("No job links found on page %d — finished", page)
                    break

                # Listing pages repeat featured jobs on every page; skip URLs
                # already seen this session instead of re-fetching them.
                # "Stale" means nothing unseen in this session, so URLs from an
                # earlier run (skip_urls) do not end the crawl early.
                new_urls = [u for u in job_urls if u not in seen_urls]
                seen_urls.update(new_urls)
                to_fetch = [u for u in new_urls if u not in skip_urls]
                self._total_pages += 1
                logger.info("Page %d: found %d job links (%d new, %d already crawled, %d saved earlier)",
                            page, len(job_urls), len(to_fetch), len(job_urls) - len(new_urls),
                            len(new_urls) - len(to_fetch))
                if not new_urls:
                    stale_pages += 1
                    if stale_pages >= 2:
                        logger.info("Two consecutive pages without new jobs — finished")
                        break
                    page += 1
                    continue
                stale_pages = 0

                for job_url in to_fetch:
                    record = self._crawl_detail_page(job_url)
                    if record is not None:
                        records.append(record)
                        self._total_records += 1

                page += 1
        finally:
            self._quit_driver()

        self._end_session()
        logger.info("TopCV crawl complete: %d records collected", len(records))
        return records

    # ------------------------------------------------------------------
    # Listing page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> List[str]:
        """Extract job detail URLs from a TopCV listing page.

        TopCV listing pages contain job cards with links matching the
        pattern: /viec-lam/{slug}/{id}.html

        Parameters
        ----------
        html : str
            Raw HTML of the listing page.

        Returns
        -------
        list[str]
            Absolute URLs to individual job detail pages.
        """
        soup = BeautifulSoup(html, "html.parser")
        urls: List[str] = []
        seen: set = set()

        # /viec-lam/{slug}/{numeric_id}.html
        job_url_pattern = re.compile(r"/viec-lam/[^/]+/\d+\.html")

        for link in soup.find_all("a", href=True):
            href = link["href"]
            if job_url_pattern.search(href):
                # Remove tracking query params for deduplication
                clean_url = href.split("?")[0]
                absolute_url = clean_url if clean_url.startswith("http") else f"{self.base_url}{clean_url}"
                if absolute_url not in seen:
                    seen.add(absolute_url)
                    urls.append(absolute_url)

        return urls

    # ------------------------------------------------------------------
    # Detail page crawling and parsing
    # ------------------------------------------------------------------

    def _crawl_detail_page(self, url: str) -> Optional[dict]:
        """Fetch and parse a single TopCV job detail page."""
        time.sleep(3)

        html = self._fetch(url)
        if html is None:
            return None

        try:
            return self._parse_detail_page(html, url)
        except Exception as exc:
            logger.error("Error parsing TopCV detail page %s: %s", url, exc)
            return None

    def _parse_detail_page(self, html: str, url: str) -> dict:
        """Parse a TopCV job detail page into a Job_Record dict.

        Layout as of 11/2026: the page embeds a schema.org ``JobPosting``
        JSON-LD block that carries salary, posting date, deadline and the
        full description in a stable machine-readable form, so it is the
        primary source. DOM selectors are used only for fields missing
        from JSON-LD (job level, company size, specialization tags) and as
        fallbacks. Labels are matched in both Vietnamese and English
        because TopCV serves either UI language.
        """
        soup = BeautifulSoup(html, "html.parser")
        jp = self._job_posting_json(soup)
        header = self._label_values(soup, ".box-header-job-list-info__item", ".list-info__content__title")
        general = self._label_values(soup, ".box-job-information-general-info-list__item", ".box-job-information-general-info-list__item--content-title")
        company_info = self._label_values(soup, ".box-company-info-detail__list--item", ".box-company-info-detail__list--item-label")

        # --- Title / company ---
        job_title = jp.get("title") or self._text(soup, ".job-detail__info--title") or self._text(soup, "h1")
        company_name = (jp.get("hiringOrganization") or {}).get("name") or self._text(soup, ".company-name-label")

        # --- Salary: JSON-LD baseSalary, else header text ---
        salary_min, salary_max, salary_currency = self._salary_from_json(jp.get("baseSalary"))
        if salary_min is None and salary_max is None:
            salary_text = self._text(soup, ".box-header-job__salary") or self._pick(header, "mức lương", "thu nhập", "salary")
            salary_min, salary_max, salary_currency = self._parse_salary(salary_text)

        # --- Location: JSON-LD region(s), else header ---
        location = self._location_from_json(jp.get("jobLocation")) or self._pick(header, "địa điểm", "location") or ""

        # --- Experience (years) ---
        experience_required = None
        exp_req = jp.get("experienceRequirements")
        if isinstance(exp_req, dict) and exp_req.get("monthsOfExperience") is not None:
            try:
                experience_required = round(float(exp_req["monthsOfExperience"]) / 12, 2)
            except (TypeError, ValueError):
                experience_required = None
        if experience_required is None:
            experience_required = self._parse_experience(self._pick(header, "kinh nghiệm", "experience"))

        # --- Level / job type / company size ---
        job_level = self._pick(general, "cấp bậc", "level") or jp.get("occupationalCategory") or ""
        # English labels keep the schema of older crawls (cleaner expects them).
        job_type = self._employment_type(jp.get("employmentType")) or self._employment_type_vi(
            self._pick(general, "loại hình", "employment type", "job type"))
        work_mode = self._pick(general, "hình thức", "working form", "work form")
        if work_mode and re.search(r"remote|từ xa", work_mode, re.IGNORECASE):
            job_type = "Remote"
        company_size = (self._pick(company_info, "quy mô", "scale", "size") or "").replace("nhân viên", "employees").strip()

        # --- Dates (None rather than a fake "today" when missing) ---
        posted_date = self._parse_date(str(jp["datePosted"])[:10]) if jp.get("datePosted") else None
        deadline = self._parse_date(str(jp["validThrough"])[:10]) if jp.get("validThrough") else None
        if deadline is None:
            m = re.search(r"\d{2}/\d{2}/\d{4}", self._pick(header, "hạn ứng tuyển", "deadline") or "")
            deadline = self._parse_date(m.group()) if m else None

        # --- Description / benefits ---
        jd_text, benefits_text = self._split_description(jp.get("description") or "")
        if jp.get("jobBenefits"):
            benefits_text = self._html_to_text(jp["jobBenefits"])
        if not jd_text:
            jd_text, dom_benefits = self._description_from_dom(soup)
            benefits_text = benefits_text or dom_benefits

        return {
            "job_title": (job_title or "").strip(),
            "company_name": (company_name or "").strip(),
            "company_size": company_size,
            "location": location,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": salary_currency,
            "experience_required": experience_required,
            "skills": ", ".join(self._extract_skills(soup)),
            "job_type": job_type or "",
            "job_level": job_level,
            "posted_date": posted_date,
            "deadline": deadline,
            "job_description": jd_text,
            "benefits": benefits_text,
            "source": "topcv",
            "url": self._canonical_url(soup) or url,
        }

    # ------------------------------------------------------------------
    # Parsing utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _text(soup: BeautifulSoup, selector: str) -> Optional[str]:
        """Return stripped text of the first element matching selector."""
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else None

    @staticmethod
    def _job_posting_json(soup: BeautifulSoup) -> dict:
        """Return the schema.org JobPosting JSON-LD object, or {} if absent."""
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("@graph", [data])
            else:
                items = []
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
        return {}

    @staticmethod
    def _label_values(soup: BeautifulSoup, item_selector: str, label_selector: str) -> Dict[str, str]:
        """Map lower-cased label -> value text for label/value info rows."""
        result: Dict[str, str] = {}
        for item in soup.select(item_selector):
            label_el = item.select_one(label_selector)
            if not label_el:
                continue
            label = label_el.get_text(" ", strip=True)
            full = re.sub(r"\s+", " ", item.get_text(" ", strip=True))
            value = full[len(label):] if full.startswith(label) else full.replace(label, "", 1)
            result[label.rstrip(":").strip().lower()] = value.strip(" :")
        return result

    @staticmethod
    def _pick(values: Dict[str, str], *keys: str) -> Optional[str]:
        """Return the first value whose label contains any of *keys*."""
        for label, value in values.items():
            if any(key in label for key in keys):
                return value
        return None

    @staticmethod
    def _salary_from_json(base_salary) -> Tuple[Optional[float], Optional[float], str]:
        """Read (min, max, currency) from a JSON-LD baseSalary, monthly-normalized."""
        if not isinstance(base_salary, dict):
            return None, None, "VND"
        currency = (base_salary.get("currency") or "VND").upper()
        value = base_salary.get("value")
        if not isinstance(value, dict):
            try:
                amount = float(value)
            except (TypeError, ValueError):
                return None, None, currency
            value = {"minValue": amount, "maxValue": amount}

        def num(v):
            try:
                f = float(v)
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None

        lo = num(value.get("minValue", value.get("value")))
        hi = num(value.get("maxValue", value.get("value")))
        unit = str(value.get("unitText") or "MONTH").upper()
        if unit == "YEAR":
            lo, hi = (lo / 12 if lo else None), (hi / 12 if hi else None)
        elif unit != "MONTH":
            return None, None, currency  # hourly/daily pay is not comparable
        return lo, hi, currency

    @staticmethod
    def _location_from_json(job_location) -> Optional[str]:
        """Join addressRegion values of one or more JSON-LD Place objects."""
        places = job_location if isinstance(job_location, list) else [job_location]
        regions: List[str] = []
        for place in places:
            if not isinstance(place, dict):
                continue
            address = place.get("address") or {}
            addresses = address if isinstance(address, list) else [address]
            for addr in addresses:
                region = addr.get("addressRegion") if isinstance(addr, dict) else None
                region = region.strip() if isinstance(region, str) else None
                if region and region not in regions:
                    regions.append(region)
        return ", ".join(regions) or None

    @staticmethod
    def _employment_type(value) -> str:
        mapping = {"FULL_TIME": "Full-time", "PART_TIME": "Part-time", "INTERN": "Internship",
                   "CONTRACTOR": "Contract", "TEMPORARY": "Contract"}
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, str) and v.upper() in mapping:
                return mapping[v.upper()]
        return ""

    @staticmethod
    def _employment_type_vi(text: Optional[str]) -> str:
        if not text:
            return ""
        low = text.lower()
        for pattern, label in [(r"toàn thời gian|full", "Full-time"), (r"bán thời gian|part", "Part-time"),
                               (r"thực tập|intern", "Internship"), (r"hợp đồng|thời vụ|contract|freelance", "Contract")]:
            if re.search(pattern, low):
                return label
        return text

    @staticmethod
    def _html_to_text(html: str) -> str:
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return re.sub(r"\n{2,}", "\n", text.replace("\xa0", " ")).strip()

    @staticmethod
    def _split_description(html: str) -> Tuple[str, str]:
        """Split JSON-LD description into (description+requirements, benefits).

        The description is a sequence of <h2> sections such as "Mô tả công
        việc", "Yêu cầu ứng viên", "Quyền lợi được hưởng". Requirements are
        kept with the description because that is where skills are listed.
        """
        if not html:
            return "", ""
        soup = BeautifulSoup(html, "html.parser")
        sections: List[Tuple[str, List[str]]] = [("", [])]
        for node in soup.contents:
            if getattr(node, "name", None) == "h2":
                sections.append((node.get_text(" ", strip=True), []))
                continue
            text = node.get_text("\n", strip=True) if hasattr(node, "get_text") else str(node).strip()
            if text:
                sections[-1][1].append(text)
        benefit_re = re.compile(r"quyền lợi|phúc lợi|benefit", re.IGNORECASE)
        jd_parts: List[str] = []
        benefit_parts: List[str] = []
        for title, parts in sections:
            if not parts:
                continue
            target = benefit_parts if benefit_re.search(title) else jd_parts
            target.append("\n".join(([title] if title else []) + parts))

        def clean(parts: List[str]) -> str:
            return re.sub(r"\n{2,}", "\n", "\n".join(parts).replace("\xa0", " ")).strip()

        return clean(jd_parts), clean(benefit_parts)

    @staticmethod
    def _description_from_dom(soup: BeautifulSoup) -> Tuple[str, str]:
        """Fallback: read description sections from the rendered page."""
        skip = re.compile(r"địa điểm|address|việc làm liên quan|thông tin chung|tổng quan|job details", re.IGNORECASE)
        jd_parts: List[str] = []
        benefit_parts: List[str] = []
        for item in soup.select(".box-job-information-detail-item"):
            title_el = item.select_one(".box-job-information-detail-item__title--title")
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if skip.search(title):
                continue
            target = benefit_parts if re.search(r"quyền lợi|benefit", title, re.IGNORECASE) else jd_parts
            target.append(item.get_text("\n", strip=True))
        return "\n".join(jd_parts).strip(), "\n".join(benefit_parts).strip()

    @staticmethod
    def _canonical_url(soup: BeautifulSoup) -> Optional[str]:
        link = soup.find("link", rel="canonical")
        return link.get("href") if link else None

    @staticmethod
    def _parse_salary(text: Optional[str]) -> Tuple[Optional[float], Optional[float], str]:
        """Extract (salary_min, salary_max, currency) from a salary string.

        Handles "15 - 25 triệu", "Tới 30 triệu", "Từ 1,000 USD", "Thỏa thuận".
        """
        if not text:
            return None, None, "VND"
        text_lower = text.lower()
        if re.search(r"th[oỏ]a thu[aậ]n|negotiable|cạnh tranh|competitive", text_lower):
            return None, None, "VND"
        currency = "USD" if ("usd" in text_lower or "$" in text) else "VND"
        nums = [float(n) for n in re.findall(r"\d+", text.replace(",", "").replace(".", ""))]
        if not nums:
            return None, None, currency
        if currency == "VND" and re.search(r"triệu|\d\s*(m|tr)\b", text_lower):
            nums = [n * 1_000_000 for n in nums]
        if len(nums) >= 2:
            return min(nums), max(nums), currency
        val = nums[0]
        if re.search(r"tới|đến|up to|upto", text_lower):
            return None, val, currency
        if re.search(r"từ|from", text_lower):
            return val, None, currency
        return val, val, currency

    @staticmethod
    def _parse_experience(text: Optional[str]) -> Optional[float]:
        """Extract numeric years of experience from text."""
        if not text:
            return None
        text_lower = text.lower()
        if re.search(r"không|chưa|no experience|not required", text_lower):
            return 0.0
        if re.search(r"dưới 1|less than 1|under 1", text_lower):
            return 0.5
        match = re.search(r"(\d+)", text)
        return float(match.group(1)) if match else None

    @staticmethod
    def _parse_date(text: Optional[str]) -> Optional[str]:
        """Parse a date string into ISO format (YYYY-MM-DD)."""
        if not text:
            return None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_skills(soup: BeautifulSoup) -> List[str]:
        """Extract specialization/skill tags from the detail page.

        Primary source is the "Chuyên môn / Specialization" tag group (same
        meaning as the comma-separated tags in older crawls). Falls back to
        the category breadcrumb tags, dropping generic and location tags.
        """
        skills: List[str] = []
        for group in soup.select(".job-tags__group"):
            name_el = group.select_one(".job-tags__group-name")
            name = name_el.get_text(strip=True) if name_el else ""
            if not re.search(r"chuyên môn|specialization|kỹ năng|skill", name, re.IGNORECASE):
                continue
            for tag in group.find_all(["a", "span"]):
                text = tag.get_text(strip=True)
                if text and text != name and text not in skills:
                    skills.append(text)
        if skills:
            return skills

        generic = {"công nghệ thông tin", "việc làm it", "information technology"}
        for tag in soup.select(".box-category-tag"):
            text = tag.get_text(strip=True)
            low = text.lower()
            if not text or low in generic or low.startswith(("việc làm", "jobs ")) or " - " in text:
                continue
            if text not in skills:
                skills.append(text)
        # The breadcrumb ends with location tags; keep only the leading category tags.
        return skills[:3]
