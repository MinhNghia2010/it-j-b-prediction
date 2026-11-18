"""Parser tests for the TopCV detail page layout of 11/2026.

The HTML fixtures are pages saved manually from a browser into
data/external/ (TopCV blocks automated fetching behind Cloudflare);
tests that need them are skipped when the files are absent.
"""

from pathlib import Path

import pytest

from scraping.topcv_crawler import TopCVCrawler

EXTERNAL = Path(__file__).resolve().parent.parent / "data" / "external"


def _parser() -> TopCVCrawler:
    crawler = TopCVCrawler.__new__(TopCVCrawler)
    crawler.base_url = "https://www.topcv.vn"
    return crawler


def _fixture(fragment: str) -> str:
    matches = [p for p in EXTERNAL.glob("*.html") if fragment in p.name]
    if not matches:
        pytest.skip(f"HTML fixture containing '{fragment}' not found in data/external/")
    return matches[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "fragment, expected",
    [
        ("Backend Developer (Java)", {"salary_min": 18_000_000, "salary_max": 24_000_000, "location": "Hà Nội",
                                      "experience_required": 3.0, "posted_date": "2026-10-29", "deadline": "2026-11-28",
                                      "company_size": "100-499 employees", "job_type": "Full-time"}),
        ("Android Developer", {"salary_min": 12_000_000, "salary_max": 18_000_000, "location": "Hồ Chí Minh",
                               "experience_required": 0.0, "posted_date": "2026-10-28"}),
    ],
)
def test_detail_page_fields(fragment, expected):
    record = _parser()._parse_detail_page(_fixture(fragment), "https://www.topcv.vn/x")
    for key, value in expected.items():
        assert record[key] == value, key
    assert record["salary_currency"] == "VND"
    assert len(record["job_description"]) > 500
    assert "Mô tả công việc" in record["job_description"]
    assert record["benefits"]
    assert record["skills"]
    assert record["job_level"] == "Nhân viên"
    assert record["url"].startswith("https://www.topcv.vn/viec-lam/")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("15 - 25 triệu", (15_000_000, 25_000_000, "VND")),
        ("Tới 30 triệu", (None, 30_000_000, "VND")),
        ("Từ 1,000 USD", (1000, None, "USD")),
        ("Thỏa thuận", (None, None, "VND")),
        ("", (None, None, "VND")),
    ],
)
def test_parse_salary_text(text, expected):
    assert TopCVCrawler._parse_salary(text) == expected


def test_salary_from_json_ld_yearly_is_monthly_normalized():
    base = {"currency": "USD", "value": {"unitText": "YEAR", "minValue": 12000, "maxValue": 24000}}
    assert TopCVCrawler._salary_from_json(base) == (1000, 2000, "USD")


def test_missing_json_ld_does_not_invent_posted_date():
    record = _parser()._parse_detail_page("<html><h1>Dev</h1></html>", "https://www.topcv.vn/x")
    assert record["posted_date"] is None
    assert record["job_title"] == "Dev"


def test_crawl_skips_repeated_listing_urls_and_stops_when_stale(monkeypatch):
    crawler = TopCVCrawler(headless=True)
    pages = {
        1: ["/viec-lam/a/1.html", "/viec-lam/b/2.html"],
        2: ["/viec-lam/a/1.html", "/viec-lam/c/3.html"],  # one featured repeat
        3: ["/viec-lam/a/1.html"],
        4: ["/viec-lam/c/3.html"],
    }
    fetched_details = []
    monkeypatch.setattr(crawler, "_init_driver", lambda: None)
    monkeypatch.setattr(crawler, "_quit_driver", lambda: None)
    monkeypatch.setattr(crawler, "_fetch", lambda url, wait_for_js=True: url)
    monkeypatch.setattr(crawler, "_parse_listing_page",
                        lambda html: [crawler.base_url + p for p in pages.get(int(html.split("page=")[1]), [])])
    monkeypatch.setattr(crawler, "_crawl_detail_page", lambda url: fetched_details.append(url) or {"url": url})

    records = crawler.crawl(max_pages=10)

    assert [r["url"].rsplit("/", 1)[1] for r in records] == ["1.html", "2.html", "3.html"]
    assert len(fetched_details) == 3
    assert crawler._total_pages == 4  # stopped after pages 3 and 4 brought nothing new


def _fake_crawler(monkeypatch, pages):
    crawler = TopCVCrawler(headless=True)
    fetched = []
    monkeypatch.setattr(crawler, "_init_driver", lambda: None)
    monkeypatch.setattr(crawler, "_quit_driver", lambda: None)
    monkeypatch.setattr(crawler, "_fetch", lambda url, wait_for_js=True: url)
    monkeypatch.setattr(crawler, "_parse_listing_page",
                        lambda html: [crawler.base_url + p for p in pages.get(int(html.split("page=")[1]), [])])
    monkeypatch.setattr(crawler, "_crawl_detail_page", lambda url: fetched.append(url) or {"url": url})
    return crawler, fetched


def test_crawl_resumes_from_start_page_and_skips_saved_urls(monkeypatch):
    pages = {p: [f"/viec-lam/j/{p}.html"] for p in range(1, 6)}
    crawler, fetched = _fake_crawler(monkeypatch, pages)
    saved = {crawler.base_url + "/viec-lam/j/3.html", crawler.base_url + "/viec-lam/j/4.html"}

    records = crawler.crawl(max_pages=5, start_page=3, skip_urls=saved)

    assert [u.rsplit("/", 1)[1] for u in fetched] == ["5.html"]
    assert len(records) == 1
    # pages 3-4 hold only previously saved jobs but must not count as stale
    assert crawler._total_pages == 3


def test_run_topcv_merges_into_todays_file(monkeypatch, tmp_path):
    import scripts.run_crawlers as rc
    from datetime import datetime
    import scraping.topcv_crawler as tc

    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    today = raw / f"topcv_{datetime.now():%Y-%m-%d}.csv"
    today.write_text("job_title,url\nOld,https://www.topcv.vn/viec-lam/j/1.html\n", encoding="utf-8")
    monkeypatch.setattr(rc, "PROJECT_ROOT", tmp_path)

    seen = {}

    class FakeCrawler(tc.TopCVCrawler):
        def crawl(self, max_pages=None, start_page=1, skip_urls=None):
            seen["skip"], seen["start"] = set(skip_urls), start_page
            self._start_session(); self._end_session()
            return [{"job_title": "New", "url": "https://www.topcv.vn/viec-lam/j/2.html"},
                    {"job_title": "Dup", "url": "https://www.topcv.vn/viec-lam/j/1.html"}]

        def save_raw(self, records, output_dir=None):
            seen["saved"] = records

    monkeypatch.setattr(tc, "TopCVCrawler", FakeCrawler)
    rc.run_topcv(max_pages=60, start_page=31)

    assert seen["start"] == 31
    assert seen["skip"] == {"https://www.topcv.vn/viec-lam/j/1.html"}
    assert [r["job_title"] for r in seen["saved"]] == ["Old", "New"]
