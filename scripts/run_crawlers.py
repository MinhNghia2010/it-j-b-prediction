"""Chạy crawlers → data/raw/{source}_{date}.csv

Usage:
    python scripts/run_crawlers.py --all --max-pages 5
    python scripts/run_crawlers.py --source topcv --max-pages 3
    python scripts/run_crawlers.py --source linkedin --max-pages 10
    python scripts/run_crawlers.py --source topcv --start-page 31 --max-pages 60

TopCV skips jobs whose URL is already in any data/raw/topcv_*.csv and merges
the new jobs into today's file instead of overwriting it, so an interrupted or
partial run can be resumed with --start-page.
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.logger import get_logger

logger = get_logger("run_crawlers")

# Old raw files hold job descriptions longer than csv's 128 KB default;
# 2**31-1 because sys.maxsize overflows the C long on Windows.
csv.field_size_limit(2**31 - 1)


def _read_raw_rows(path):
    """Rows of a raw CSV, dropping failed scrapes (no title) so they get retried."""
    with open(path, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("job_title") or "").strip()]


def run_topcv(max_pages=None, start_page=1):
    from scraping.topcv_crawler import TopCVCrawler
    logger.info("=== Crawling TopCV ===")
    raw_dir = PROJECT_ROOT / "data" / "raw"
    known_urls = set()
    for path in raw_dir.glob("topcv_*.csv"):
        known_urls.update(r["url"] for r in _read_raw_rows(path) if r.get("url"))
    today_file = raw_dir / f"topcv_{datetime.now():%Y-%m-%d}.csv"
    existing = _read_raw_rows(today_file) if today_file.exists() else []
    logger.info("Skipping %d URLs already in data/raw; %d rows in %s will be kept",
                len(known_urls), len(existing), today_file.name)

    # headless=False để bypass Cloudflare (headless bị detect)
    crawler = TopCVCrawler(headless=False, delay=7.0)
    records = crawler.crawl(max_pages=max_pages, start_page=start_page, skip_urls=known_urls)

    merged, seen = [], set()
    for row in existing + records:
        key = row.get("url") or id(row)
        if key not in seen:
            seen.add(key)
            merged.append(row)
    if merged:
        crawler.save_raw(merged)
    logger.info("TopCV: %d new records, %d total in %s", len(records), len(merged), today_file.name)
    report = crawler.generate_report()
    logger.info("TopCV: %d records, %d errors, %.1fs", report.total_records, report.total_errors, report.duration_seconds)
    return len(records)

def run_itviec(max_pages=None):
    from scraping.itviec_crawler import ITviecCrawler
    logger.info("=== Crawling ITviec ===")
    crawler = ITviecCrawler()
    # Bypass robots.txt (itviec blocks crawlers via robots.txt)
    crawler.check_robots_txt = lambda url: True
    records = crawler.crawl(max_pages=max_pages)
    crawler.save_raw(records)
    report = crawler.generate_report()
    logger.info("ITviec: %d records, %d errors, %.1fs",report.total_records, report.total_errors, report.duration_seconds)
    return len(records)


def run_linkedin(max_pages=None):
    from scraping.linkedin_crawler import LinkedInCrawler
    logger.info("=== Crawling LinkedIn ===")
    crawler = LinkedInCrawler(keywords="IT", geo_id="104195383", location="Vietnam")
    records = crawler.crawl(max_pages=max_pages)
    crawler.save_raw(records)
    report = crawler.generate_report()
    logger.info("LinkedIn: %d records, %d errors, %.1fs",report.total_records, report.total_errors, report.duration_seconds)
    return len(records)


def run_careerviet(max_pages=None):
    from scraping.careerviet_crawler import CareerVietCrawler
    logger.info("=== Crawling CareerViet ===")
    crawler = CareerVietCrawler()
    records = crawler.crawl(max_pages=max_pages)
    crawler.save_raw(records)
    report = crawler.generate_report()
    logger.info("CareerViet: %d records, %d errors, %.1fs",report.total_records, report.total_errors, report.duration_seconds)
    return len(records)

CRAWLERS = {
    "topcv": run_topcv,
    "itviec": run_itviec,
    "linkedin": run_linkedin,
    "careerviet": run_careerviet,
}

def main():
    parser = argparse.ArgumentParser(description="Crawl job data -> data/raw/")
    parser.add_argument("--source", choices=list(CRAWLERS.keys()))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="last listing page to crawl (inclusive)")
    parser.add_argument("--start-page", type=int, default=1,
                        help="first listing page (TopCV only), to resume a run")
    args = parser.parse_args()

    if not args.source and not args.all:
        parser.print_help()
        sys.exit(1)

    total = 0
    if args.source:
        kwargs = {"start_page": args.start_page} if args.source == "topcv" else {}
        total += CRAWLERS[args.source](max_pages=args.max_pages, **kwargs)
    elif args.all:
        for name, fn in CRAWLERS.items():
            try:
                total += fn(max_pages=args.max_pages)
            except Exception as exc:
                logger.error("Crawler '%s' failed: %s", name, exc)
    
    logger.info("TOTAL: %d records saved to data/raw/", total)

if __name__ == "__main__":
    main()