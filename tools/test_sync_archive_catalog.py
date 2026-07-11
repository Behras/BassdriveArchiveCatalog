import unittest
from unittest.mock import patch

from tools.sync_archive_catalog import (
    ArticleSummaryParser,
    best_profile,
    build_index,
    parse_listing,
    parse_size,
    suggest_tags,
)


class CatalogSyncTests(unittest.TestCase):
    def test_directory_parser_reads_full_title_size_and_date(self):
        rows = parse_listing(
            """
            <table><tr>
              <td class="link"><a href="track.mp3" title="Full Track.mp3">Track..&gt;</a></td>
              <td class="size">164.5 MiB</td>
              <td class="date">2026-Jul-11 12:00</td>
            </tr></table>
            """
        )
        self.assertEqual("Full Track.mp3", rows[0]["name"])
        self.assertEqual("164.5 MiB", rows[0]["size"])
        self.assertEqual(172_490_752, parse_size(rows[0]["size"]))

    def test_description_parser_excludes_sidebar_text(self):
        parser = ArticleSummaryParser()
        parser.feed(
            """
            <aside>liquid soulful jungle</aside>
            <div class="article-summary"><div><p>dark techstep pressure</p></div></div>
            <footer>dancefloor jump up</footer>
            """
        )
        self.assertEqual(["TECH_DARK"], suggest_tags(parser.text))

    def test_profile_matching_accepts_spacing_but_rejects_similar_names(self):
        profiles = [
            {"name": "The Onward Show", "matchName": "onward", "url": "onward"},
            {"name": "Reflect Live", "matchName": "reflect", "url": "reflect"},
        ]
        profile, score = best_profile("On Ward Show - Jay", profiles)
        self.assertEqual("onward", profile["url"])
        self.assertGreaterEqual(score, 0.78)

        profile, score = best_profile("Reflective Music Show", profiles)
        self.assertLess(score, 0.78)

    def test_index_reuses_unchanged_folders_unless_full_scan_is_requested(self):
        source = {
            "name": "Test Show",
            "day": "Monday",
            "url": "http://bassdrivearchive.com/test/",
            "folderModified": "2026-07-11T00:00:00Z",
        }
        cached = {
            **source,
            "playableEpisodes": 4,
            "brokenEpisodes": 0,
            "newestEpisode": None,
            "newestEpisodeName": None,
            "scanStatus": "OK",
        }

        with patch("tools.sync_archive_catalog.inspect_source", return_value=cached) as inspect:
            _, inspected = build_index([source], {"sources": [cached]})
            self.assertEqual(0, inspected)
            inspect.assert_not_called()

            _, inspected = build_index(
                [source],
                {"sources": [cached]},
                force_full_scan=True,
            )
            self.assertEqual(1, inspected)
            inspect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
