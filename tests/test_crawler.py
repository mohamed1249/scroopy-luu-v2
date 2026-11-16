import unittest
from unittest.mock import patch
import requests
from scr_prp import scrape_page_and_subpages_report
from crawler import canonical_url
from download_limits import DownloadBudget

BASE = "https://portfolio.example"


def response(url, html, status=200, location=None):
    r = requests.Response()
    r.status_code, r.url = status, url
    r._content = html.encode()
    r._content_consumed = True
    if location:
        r.headers["Location"] = location
    return r


class CrawlerTests(unittest.TestCase):
    def test_large_shared_frontier_fetches_each_url_once(self):
        links = ''.join(f'<a href="/p/{n}">Page</a><a href="/p/{n}#repeat">Again</a>' for n in range(250))
        graph = {BASE + "/": links}
        graph.update({BASE + f"/p/{n}": '<p>Body</p><a href="/">Home</a><a href="/shared">Shared</a>'
                      for n in range(250)})
        graph[BASE + "/shared"] = "Shared content"
        report, calls = self.crawl(graph, max_levels=3, max_pages=None)
        self.assertEqual(len(calls), 252)
        self.assertEqual(len(set(calls)), 252)
        self.assertEqual(report["summary"]["success"], 252)

    def test_repeated_href_on_main_page_fetches_child_once(self):
        graph = {BASE + "/": '<a href="/child">A</a><a href="/child">B</a><a href="/child#x">C</a>',
                 BASE + "/child": "Child"}
        report, calls = self.crawl(graph, max_levels=2, max_pages=None)
        self.assertEqual(calls, [BASE + "/", BASE + "/child"])
        self.assertEqual(report["crawl"]["duplicate_links_avoided"], 2)

    def crawl(self, graph, **options):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            value = graph[url]
            if isinstance(value, tuple):
                return response(url, "", value[0], value[1])
            return response(url, value)
        seeds = options.pop("seeds", BASE + "/")
        with patch("download_limits.request_get", side_effect=get):
            report = scrape_page_and_subpages_report(seeds, **options)
        return report, calls

    def graph(self):
        return {
            BASE + "/": '<a href="/tabs/a">A</a><a href="/tabs/b">B</a>',
            BASE + "/tabs/a": '<a href="/posts/shared">Shared</a><a href="/">Home</a>',
            BASE + "/tabs/b": '<a href="/posts/shared#part">Same</a><a href="/posts/b">B</a>',
            BASE + "/posts/shared": '<a href="/tabs/a">Cycle</a><a href="/too-deep">Deep</a>',
            BASE + "/posts/b": 'Post B',
        }

    def test_three_levels_cycle_and_diamond_fetched_once(self):
        report, calls = self.crawl(self.graph(), max_levels=3, max_pages=None)
        self.assertEqual(calls, list(self.graph()))
        self.assertEqual([p["level"] for p in report["pages"]], [1, 2, 2, 3, 3])
        self.assertEqual(report["summary"]["success"], 5)
        self.assertGreaterEqual(report["crawl"]["duplicate_links_avoided"], 2)
        self.assertEqual(report["pages"][3]["parent_url"], BASE + "/tabs/a")

    def test_level_one_fetches_only_seed(self):
        report, calls = self.crawl(self.graph(), max_levels=1, max_pages=None)
        self.assertEqual(calls, [BASE + "/"])

    def test_unlimited_has_no_old_twenty_page_cap(self):
        graph = {BASE + "/": "".join(f'<a href="/p/{n}">{n}</a>' for n in range(25))}
        graph.update({BASE + f"/p/{n}": str(n) for n in range(25)})
        report, calls = self.crawl(graph, max_pages=None)
        self.assertEqual(len(calls), 26)
        self.assertEqual(report["summary"]["skipped"], 0)

    def test_finite_cap_still_enforced(self):
        report, calls = self.crawl(self.graph(), max_pages=2, max_levels=3)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(p.get("skip_reason") == "page_limit" for p in report["pages"]))

    def test_multiple_seeds_share_visited_set_and_shortest_depth(self):
        graph = self.graph()
        graph[BASE + "/too-deep"] = "deep"
        report, calls = self.crawl(graph, seeds=[BASE + "/", BASE + "/tabs/a", BASE + "/#top"],
                                   max_levels=3, max_pages=None)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(len(calls), 6)
        self.assertEqual(next(p for p in report["pages"] if p["url"].endswith("/tabs/a"))["level"], 1)

    def test_redirect_alias_does_not_refetch_known_target(self):
        graph = {BASE + "/": '<a href="/real">Real</a><a href="/alias">Alias</a>',
                 BASE + "/real": "Text", BASE + "/alias": (302, "/real")}
        report, calls = self.crawl(graph, max_pages=None)
        self.assertEqual(calls.count(BASE + "/real"), 1)
        self.assertEqual(report["pages"][-1]["skip_reason"], "duplicate_redirect")

    def test_redirect_target_already_queued_is_not_refetched(self):
        graph = {BASE + "/": '<a href="/alias">Alias</a><a href="/real">Real</a>',
                 BASE + "/alias": (302, "/real"), BASE + "/real": "Text"}
        report, calls = self.crawl(graph, max_pages=None)
        self.assertEqual(calls.count(BASE + "/real"), 1)
        self.assertEqual(report["summary"]["success"], 2)

    def test_external_links_and_redirects_blocked_by_scope(self):
        graph = {BASE + "/": '<a href="https://other.example/">External</a><a href="/alias">Alias</a>',
                 BASE + "/alias": (302, "https://other.example/")}
        report, calls = self.crawl(graph, max_pages=None)
        self.assertFalse(any("other.example" in url for url in calls))
        self.assertEqual(report["summary"]["skipped"], 2)

    def test_budget_survives_unlimited_mode(self):
        graph = {BASE + "/": '<a href="/next">Next</a>', BASE + "/next": "x" * 100}
        report, calls = self.crawl(graph, max_pages=None, budget=DownloadBudget(1000, 30))
        self.assertEqual(report["pages"][-1]["error"]["type"], "RunDownloadLimitExceeded")

    def test_normalization_is_conservative(self):
        self.assertEqual(canonical_url("HTTPS://PORTFOLIO.EXAMPLE:443/%7Eme#top"), BASE + "/~me")
        self.assertNotEqual(canonical_url(BASE + "/?page=1"), canonical_url(BASE + "/?page=2"))

    def test_invalid_levels_rejected_before_fetch(self):
        with patch("download_limits.request_get") as get:
            for value in (0, -1, True, None):
                with self.assertRaises(ValueError):
                    scrape_page_and_subpages_report(BASE, max_levels=value)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
