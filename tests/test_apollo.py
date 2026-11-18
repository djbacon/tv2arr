import unittest

import httpx

from tv2arr.sources import apollo


def movie(**changes):
    return {
        "ID": 304228, "Title": "Klajumā aiz meža", "OriginalTitle": "Klajumā aiz meža",
        "ProductionYear": 2026, "EventType": "Movie", "Genres": "Drāma, Detektīvfilma",
        "EventURL": "https://www.apollokino.lv/event/304228/klajum%C4%81%20aiz%20me%C5%BEa",
        "Images": {"EventLargeImagePortrait": apollo.POSTER_ORIGIN + "/1048/poster.jpg"},
    } | changes


class ApolloTest(unittest.TestCase):
    def test_catalogue_preserves_ids_titles_and_production_year(self):
        catalogue = apollo.parse_catalogue([movie(dtLocalRelease="2027-01-01T00:00:00")])
        self.assertEqual(catalogue.listings, [
            ("304228", "Klajumā aiz meža", movie()["EventURL"]),
        ])
        self.assertEqual(catalogue.details["304228"], (
            "Klajumā aiz meža", 2026, apollo.POSTER_ORIGIN + "/1048/poster.jpg",
        ))

    def test_skips_performances_even_when_marked_as_movies(self):
        for genre in ("Balets", "Koncerts", "Opera", "Drāma, BALLET", "Concert; Mūzika"):
            with self.subTest(genre=genre):
                catalogue = apollo.parse_catalogue([movie(Genres=genre)])
                self.assertEqual(catalogue.listings, [])
                self.assertEqual(catalogue.details, {})
        self.assertEqual(apollo.parse_catalogue([movie(EventType="Live")]).listings, [])
        # A film with an opera-related title is still a movie.
        self.assertEqual(len(apollo.parse_catalogue([movie(Title="Operas spoks")]).listings), 1)

    def test_rejects_empty_invalid_partial_and_duplicate_catalogues(self):
        for data in (None, {}, [], "blocked", [None], [movie(), {}], [movie(), movie()]):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    apollo.parse_catalogue(data)
        for changes in ({"ID": True}, {"ID": "304228"}, {"ID": 0}, {"Title": " "},
                        {"EventType": None}, {"Genres": None}, {"OriginalTitle": 42},
                        {"Images": ["invalid"]}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    apollo.parse_catalogue([movie(**changes)])

    def test_rejects_foreign_or_mismatched_movie_links(self):
        for url in ("https://example.com/event/304228/test", "javascript:alert(1)",
                    "https://www.apollokino.lv.evil.test/event/304228/test",
                    "https://www.apollokino.lv@evil.test/event/304228/test",
                    "http://www.apollokino.lv/event/304228/test",
                    "https://www.apollokino.lv/event/999/test", "/event/304228/test"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    apollo.parse_catalogue([movie(EventURL=url)])

    def test_missing_optional_metadata_stays_unverified(self):
        catalogue = apollo.parse_catalogue([movie(OriginalTitle=None, ProductionYear=None, Images=None)])
        self.assertEqual(catalogue.details["304228"], ("", None, ""))
        for year in (True, "2026", 0, 9999):
            with self.subTest(year=year):
                self.assertIsNone(apollo.parse_catalogue([movie(ProductionYear=year)]).details["304228"][1])

    def test_poster_origin_is_restricted_and_query_is_removed_from_movie_link(self):
        for poster in ("http://mcswebsites.blob.core.windows.net/a.jpg",
                       "https://mcswebsites.blob.core.windows.net.evil.test/a.jpg", "javascript:alert(1)", 7):
            with self.subTest(poster=poster):
                catalogue = apollo.parse_catalogue([movie(Images={"EventLargeImagePortrait": poster})])
                self.assertEqual(catalogue.details["304228"][2], "")
        url = movie()["EventURL"]
        self.assertEqual(apollo.parse_catalogue([movie(EventURL=url + "?theatreAreaID=1014#shows")]).listings[0][2], url)

    def test_http_errors_and_non_json_responses_fail(self):
        for status, body, error in ((503, "unavailable", httpx.HTTPStatusError),
                                    (200, "<html>blocked</html>", ValueError),
                                    (302, "", httpx.HTTPStatusError)):
            with self.subTest(status=status):
                with httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text=body)
                )) as client:
                    with self.assertRaises(error):
                        apollo.listing(client)

    def test_requests_only_the_public_feed(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(200, json=[movie()])

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            catalogue = apollo.listing(client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), apollo.FEED_URL)
        self.assertEqual(calls[0].headers["Accept"], "application/json")
        self.assertEqual(len(catalogue.details), 1)
