import unittest
from unittest.mock import Mock

from tv2arr.seerr import find_candidates


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.listing = {"title": "Dzīvais ūdens", "original_title": "Magic Water", "year": 2009}
        self.unrelated = {
            "id": 1, "title": "Magic in the Water", "originalTitle": "Magic in the Water",
            "year": "1995", "language": "en",
        }
        self.match = {
            "id": 2, "title": "The Magic Water", "originalTitle": "Dzīvais ūdens",
            "year": "2009", "language": "lv",
        }
        self.api = Mock()
        self.api.search.return_value = [self.unrelated, self.match]

    def test_localized_title_and_year_match_even_when_original_title_differs(self):
        candidates, exact = find_candidates(self.api, self.listing)
        self.assertEqual(exact, 2)
        self.assertEqual([movie["id"] for movie in candidates], [2, 1])
        self.assertEqual(self.api.search.call_args_list, [
            unittest.mock.call("Magic Water", "en"),
            unittest.mock.call("Dzīvais ūdens", "lv"),
        ])

    def test_either_candidate_title_can_match(self):
        self.api.search.return_value = [dict(self.match, title="DZĪVAIS ŪDENS!", originalTitle="Other")]
        self.assertEqual(find_candidates(self.api, self.listing)[1], 2)

    def test_original_title_still_matches(self):
        self.api.search.return_value = [dict(self.match, title="Magic Water", originalTitle="Other")]
        self.assertEqual(find_candidates(self.api, self.listing)[1], 2)

    def test_different_or_missing_year_requires_review(self):
        for year in (1995, None):
            with self.subTest(year=year):
                self.assertIsNone(find_candidates(self.api, dict(self.listing, year=year))[1])

    def test_different_ids_matching_either_title_are_ambiguous(self):
        self.api.search.return_value += [dict(self.match, id=3, title="Magic Water", originalTitle="Other")]
        self.assertIsNone(find_candidates(self.api, self.listing)[1])

    def test_match_found_only_in_second_search_is_used(self):
        self.api.search.side_effect = [[self.unrelated], [self.match]]
        self.assertEqual(find_candidates(self.api, self.listing)[1], 2)

    def test_empty_titles_do_not_match(self):
        self.api.search.return_value = [dict(self.match, title="Other", originalTitle="")]
        self.assertIsNone(find_candidates(self.api, dict(self.listing, original_title=""))[1])

    def test_localized_title_works_without_original_title(self):
        self.assertEqual(find_candidates(self.api, dict(self.listing, original_title=""))[1], 2)

    def test_manual_search_does_not_automatically_select_a_match(self):
        candidates, exact = find_candidates(self.api, self.listing, "Dzīvais ūdens")
        self.assertEqual(candidates, [self.unrelated, self.match])
        self.assertIsNone(exact)
        self.api.search.assert_called_once_with("Dzīvais ūdens")


if __name__ == "__main__":
    unittest.main()
