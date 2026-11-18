import copy
import json
import unittest

import httpx

from tv2arr.sources import replay


def movie(sid=101, **changes):
    return {
        "id": sid + 50000, "external_id": sid, "external_source": "ltv",
        "title": f"Filma {sid}. Drāma", "slug": f"filma-{sid}-drama",
        "type": "full", "playback_type": "VIDEO", "show": {"id": 252},
        "thumbnail": f"{replay.POSTER_ORIGIN}/storage/{sid}.jpg",
    } | changes


def details(item=None, metadata=None):
    return {"data": {"viewMedia": {
        "mediaItem": movie() if item is None else item,
        "metadata": [
            {"name": "year", "values": [{"title": "2019"}]},
            {"name": "title-original", "values": [{"title": "Original 101"}]},
        ] if metadata is None else metadata,
    }}}


class ReplayTest(unittest.TestCase):
    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def metadata(self, body):
        return replay.details(self.client(lambda r: httpx.Response(200, json=body)), {
            "source_id": "50101", "url": replay.parse_item(movie())[2],
        })

    def test_pagination_reads_complete_collection_with_required_request_context(self):
        items = [movie(sid) for sid in range(101, 126)]
        pages = []

        def respond(request):
            self.assertEqual(str(request.url).split("?")[0], replay.API_URL)
            self.assertEqual(request.headers["Referer"], replay.URL)
            self.assertEqual(request.headers["Set-Language"], "lv")
            self.assertEqual(request.headers["X-Media-Type"], "video")
            self.assertEqual(json.loads(request.url.params["extensions"]), {"language": "lv", "media": "video"})
            self.assertIn("show_ids: [252]", request.url.params["query"])
            page = json.loads(request.url.params["variables"])["page"]
            pages.append(page)
            return httpx.Response(200, json={"data": {"media": {
                "total": 25, "data": items[(page - 1) * 24:page * 24],
            }}})

        catalogue = replay.listing(self.client(respond))
        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(catalogue.listings), 25)
        self.assertEqual(catalogue.listings[0], (
            "50101", "Filma 101", "https://replay.lsm.lv/lv/skaties/ieraksts/ltv/101/filma-101-drama",
        ))
        self.assertEqual(catalogue.listings[-1][0], "50125")

    def test_title_cleanup_preserves_title_segments_and_punctuation(self):
        for title, expected in (
            ("Dvēseļu putenis. Vēsturiska drāma", "Dvēseļu putenis"),
            ("Sapņu ceļojums. Kirasao. Romantiska filma", "Sapņu ceļojums. Kirasao"),
            ("Sveiciens no Marsa! Ģimenes filma", "Sveiciens no Marsa!"),
            ("Sirds pienākums. Laipni lūgti Rīgenē! Romantiska filma", "Sirds pienākums. Laipni lūgti Rīgenē!"),
            ("Frenks. Igaunijas mākslas filma", "Frenks"),
            ("Mans klasesbiedrs Lāšs", "Mans klasesbiedrs Lāšs"),
            ("Filma. Nezināms apakšvirsraksts", "Filma. Nezināms apakšvirsraksts"),
        ):
            with self.subTest(title=title):
                self.assertEqual(replay.parse_item(movie(title=title))[1], expected)

    def test_skips_non_full_video_entries(self):
        items = [movie(), movie(102, type="episode"), movie(103, playback_type="AUDIO")]
        client = self.client(lambda r: httpx.Response(200, json={"data": {"media": {"total": 3, "data": items}}}))
        self.assertEqual(len(replay.listing(client).listings), 1)

    def test_rejects_invalid_identities_and_wrong_collection(self):
        for changes in ({"id": True}, {"id": 0}, {"external_id": "101"}, {"external_id": False},
                        {"external_source": "other"}, {"show": {"id": 253}}, {"show": None},
                        {"title": ""}, {"slug": "../other"}, {"slug": "https://evil.test"},
                        {"type": None}, {"playback_type": None}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replay.parse_item(movie(**changes))

    def test_rejects_empty_incomplete_or_oversized_catalogues(self):
        for batch in (None, {}, {"total": 0, "data": []}, {"total": True, "data": [movie()]},
                      {"total": 2, "data": [movie()]}, {"total": 1, "data": {}},
                      {"total": 2401, "data": [movie()]}, {"total": 1, "data": [{}]}):
            with self.subTest(batch=batch):
                client = self.client(lambda r: httpx.Response(200, json={"data": {"media": batch}}))
                with self.assertRaises(ValueError):
                    replay.listing(client)

    def test_rejects_duplicate_movies_and_changing_totals_across_pages(self):
        first = [movie(sid) for sid in range(101, 125)]
        for second in ({"total": 25, "data": [first[0]]}, {"total": 26, "data": [movie(125), movie(126)]},
                       {"total": 25, "data": []}):
            with self.subTest(second=second):
                def respond(request):
                    page = json.loads(request.url.params["variables"])["page"]
                    batch = {"total": 25, "data": first} if page == 1 else second
                    return httpx.Response(200, json={"data": {"media": batch}})
                with self.assertRaises(ValueError):
                    replay.listing(self.client(respond))

    def test_graphql_errors_and_partial_data_are_rejected(self):
        for body in (None, [], {}, {"data": None}, {"errors": [{"message": "failed"}]},
                     {"data": {"media": {"total": 1, "data": [movie()]}}, "errors": [{"message": "partial"}]}):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    replay.listing(self.client(lambda r: httpx.Response(200, json=body)))

    def test_http_errors_and_invalid_json_fail_without_fallback(self):
        for status, body, error in ((403, "denied", httpx.HTTPStatusError),
                                    (503, "unavailable", httpx.HTTPStatusError),
                                    (302, "", httpx.HTTPStatusError), (200, "<html>blocked</html>", ValueError)):
            with self.subTest(status=status):
                with self.assertRaises(error):
                    replay.listing(self.client(lambda r: httpx.Response(status, text=body)))

    def test_original_title_and_production_year_are_not_broadcast_dates(self):
        body = details(movie(aired_at="2026-11-17", published_at="2026-11-17"))
        original, year, poster = self.metadata(body)
        self.assertEqual((original, year), ("Original 101", 2019))
        self.assertEqual(poster, f"{replay.POSTER_ORIGIN}/storage/101.jpg")

    def test_missing_or_conflicting_metadata_stays_unverified(self):
        self.assertEqual(self.metadata(details(metadata=[]))[:2], ("", None))
        body = details(metadata=[
            {"name": "year", "values": [{"title": "2019"}, {"title": "2020"}]},
            {"name": "title-original", "values": [{"title": "First"}, {"title": "Second"}]},
        ])
        self.assertEqual(self.metadata(body)[:2], ("", None))
        for year in ("2026-11-17", "0", "unknown"):
            with self.subTest(year=year):
                self.assertIsNone(self.metadata(details(metadata=[{"name": "year", "values": [{"title": year}]}]))[1])

    def test_wrong_movie_details_are_rejected(self):
        for changes in ({"id": 999}, {"external_id": 999}, {"show": {"id": 999}},
                        {"slug": "other-title"}, {"type": "episode"}, {"playback_type": "AUDIO"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.metadata(details(movie(**changes)))

    def test_invalid_detail_links_are_rejected_before_fetching(self):
        calls = []
        client = self.client(lambda r: calls.append(r))
        url = replay.parse_item(movie())[2]
        for bad in (url.replace("replay.lsm.lv", "evil.test"), url.replace("https:", "http:"),
                    url + "?q=1", url + "#other", "https://replay.lsm.lv/lv/skaties/raidijums/252/test"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    replay.details(client, {"source_id": "50101", "url": bad})
        self.assertEqual(calls, [])

    def test_malformed_metadata_remains_an_error(self):
        for metadata in ({}, [None], [{"name": "year", "values": None}],
                         [{"name": "year", "values": [{"title": 2019}]}]):
            body = copy.deepcopy(details())
            body["data"]["viewMedia"]["metadata"] = metadata
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    self.metadata(body)

    def test_poster_urls_are_limited_to_verified_ltv_origin(self):
        for poster in (None, 7, "javascript:alert(1)", "http://ltv.lsm.lv/a.jpg",
                       "https://ltv.lsm.lv.evil.test/a.jpg", "https://ltv.lsm.lv@evil.test/a.jpg"):
            with self.subTest(poster=poster):
                self.assertEqual(self.metadata(details(movie(thumbnail=poster)))[2], "")
