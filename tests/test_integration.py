import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup

from tv2arr.app import create_app
from tv2arr.seerr import Seerr, SeerrError
from tv2arr.service import MISSING_RETRY_SECONDS, Service
from tv2arr.sources import apollo, tv3
from tv2arr.storage import RIGA, Storage, now_stamp, retention_cutoff

PASSWORD = "temporary-test-password"
API_KEY = "temporary-test-key"


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.posts = []
        self.movie_language = "en"
        self.movie_original_title = ""
        self.movie_poster_path = None
        self.movie_lookups = []
        self.remote_has_request = False
        self.timed_out = False
        self.search_available = True
        self.search_queries = []
        self.rejected_title = ""
        self.reject_latvian_locale = False
        self.source_ids = [101]
        self.apollo_feed = []
        self.source_calls = []
        self.config = {
            "TESTING": True, "DATA_DIR": data.name, "ADMIN_PASSWORD": PASSWORD,
            "LAN_IP": "192.168.2.5", "LAN_CIDR": "192.168.2.0/24",
        }
        self.app = create_app(self.config, start_scheduler=False, service_factory=self.service_factory)
        self.service = self.app.extensions["tv2arr"]
        self.db = self.service.db
        self.browser = self.app.test_client()

    def service_factory(self, db):
        return Service(
            db, sources=(tv3,), seerr=lambda url, key: Seerr(url, key, httpx.MockTransport(self.fake_seerr)),
            source_transport=httpx.MockTransport(self.fake_tv3),
        )

    def fake_tv3(self, request):
        self.source_calls.append(str(request.url))
        if str(request.url) == apollo.FEED_URL:
            return httpx.Response(200, json=self.apollo_feed)
        if request.url.path == "/filmas/":
            cards = "".join(
                f'<h3 class="tv3-post-title"><a href="/filmas/filma-{sid}/">Filma {sid}</a></h3>'
                for sid in self.source_ids
            )
            return httpx.Response(200, text=f"""
                <div class="tv3-play-block"><h2>Jaunākās filmas</h2>{cards}</div>
                <div class="tv3-play-block"><h2>Citas filmas</h2>
                   <h3 class="tv3-post-title"><a href="/filmas/ignore-999/">Ignore</a></h3>
                </div>
            """)
        sid = re.search(r"(\d+)/$", request.url.path)[1]
        if request.url.path == "/filmas/filma-101/":
            return httpx.Response(301, headers={"Location": "/filmas/original-101/"})
        return httpx.Response(200, text=f"""
            <meta name="{tv3.PRODUCT_META}Id" content="{sid}">
            <meta name="{tv3.PRODUCT_META}Year" content="2024">
            <meta name="{tv3.PRODUCT_META}LabeloriginalTitle" content='{{"text":"Original {sid}"}}'>
            <meta name="{tv3.PRODUCT_META}Image3x4" content="https://tv3cdn.lv/frame-{sid}.jpg">
        """)

    def fake_seerr(self, request):
        self.assertEqual(request.headers["x-api-key"], API_KEY)
        path = request.url.path
        if path == "/seerr/api/v1/auth/me":
            return httpx.Response(200, json={"id": 1})
        if path == "/seerr/api/v1/search":
            self.assertEqual(request.url.params.get("page"), "1")
            self.search_queries.append(request.url.params["query"])
            if not self.search_available:
                return httpx.Response(200, json={"results": []})
            title = request.url.params["query"]
            if title == self.rejected_title:
                return httpx.Response(400, json={"message": f"Invalid search query. {API_KEY}"})
            if self.reject_latvian_locale and request.url.params.get("language") == "lv":
                return httpx.Response(400, json={"message": "Unsupported language parameter."})
            if title == "The Matrix":
                return httpx.Response(200, json={"results": [
                    {"id": 603, "title": title, "mediaType": "movie"},
                ]})
            sid = int(title.split()[-1])
            return httpx.Response(200, json={"results": [
                {
                    "id": sid, "title": title, "originalTitle": f"Original {sid}",
                    "originalLanguage": self.movie_language, "releaseDate": "2024-02-03", "mediaType": "movie",
                },
                {
                    "id": sid + 1000, "title": title, "originalTitle": title,
                    "releaseDate": "2024-02-03", "mediaType": "tv",
                },
            ]})
        if path.startswith("/seerr/api/v1/movie/"):
            sid = int(path.split("/")[-1])
            self.movie_lookups.append(sid)
            movie = {"id": sid, "title": f"Original {sid}", "originalLanguage": self.movie_language,
                     "posterPath": self.movie_poster_path}
            if self.movie_original_title:
                movie["originalTitle"] = self.movie_original_title
            if self.remote_has_request:
                movie["mediaInfo"] = {"status": 2, "requests": [{"id": 52, "status": 1}]}
            return httpx.Response(200, json=movie)
        if path == "/seerr/api/v1/request":
            self.posts.append(json.loads(request.content))
            if self.timed_out:
                raise httpx.ReadTimeout("simulated lost response")
            return httpx.Response(201, json={"id": 52})
        self.fail(f"Unexpected Seerr route {path}")

    def csrf(self, url):
        page = self.browser.get(url)
        self.assertEqual(page.status_code, 200)
        return re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True))[1]

    def submit_connection(self, mode="manual"):
        response = self.browser.post("/login", data={"csrf": self.csrf("/login"), "password": PASSWORD})
        self.assertEqual(response.status_code, 303)
        return self.browser.post("/settings", data={
            "csrf": self.csrf("/settings"), "url": "http://seerr:5055/seerr/",
            "api_key": API_KEY, "mode": mode, "interval": 6, "limit": 2, "action": "test",
        }, follow_redirects=True)

    def connect(self, mode="manual"):
        response = self.submit_connection(mode)
        self.assertIn("Seerr connection verified", response.get_data(as_text=True))
        self.assertNotIn(API_KEY, response.get_data(as_text=True))

    def scan(self):
        self.service.scan_source(tv3)

    def enable_apollo(self, ids=(102,)):
        self.service.sources[apollo.SLUG] = apollo
        self.apollo_feed = [
            {
                "ID": sid, "Title": f"Apollo filma {sid}", "OriginalTitle": f"Original {sid}",
                "ProductionYear": 2024, "dtLocalRelease": "2099-01-01T00:00:00",
                "EventType": "Movie", "Genres": "Drāma",
                "EventURL": f"https://www.apollokino.lv/event/{sid}/filma-{sid}",
                "Images": {"EventLargeImagePortrait": f"{apollo.POSTER_ORIGIN}/1048/{sid}.jpg"},
            }
            for sid in ids
        ]

    def scan_all(self):
        self.service.scan_lock.acquire()
        self.service._scan()

    def test_default_sources_include_apollo_and_tv3(self):
        self.assertEqual(set(Service(self.db).sources), {"tv3", "apollo"})

    def test_apollo_catalogue_enriches_without_seerr_or_detail_requests(self):
        self.enable_apollo((102, 103))
        self.service.scan_source(apollo)
        movies = self.db.rows("SELECT * FROM listings ORDER BY id")
        self.assertEqual([m["source_id"] for m in movies], ["102", "103"])
        self.assertTrue(all(m["source"] == "apollo" and m["enriched"] for m in movies))
        self.assertEqual(movies[0]["original_title"], "Original 102")
        self.assertEqual(movies[0]["year"], 2024)
        self.assertEqual(movies[0]["poster"], f"{apollo.POSTER_ORIGIN}/1048/102.jpg")
        self.assertEqual(self.source_calls, [apollo.FEED_URL])
        self.assertEqual(self.search_queries, [])
        self.assertEqual(self.posts, [])
        self.apollo_feed.pop()
        self.service.scan_source(apollo)
        self.assertEqual(self.db.movie(2)["present"], 0)
        self.assertEqual(self.source_calls, [apollo.FEED_URL] * 2)

    def test_apollo_uses_shared_matching_language_gate_and_preview_mode(self):
        self.enable_apollo()
        self.connect("auto")
        for language, state in (("en", "ready"), ("fr", "excluded"), ("", "review")):
            with self.subTest(language=language):
                self.db.write("DELETE FROM listings")
                self.db.write("DELETE FROM history")
                self.posts.clear()
                self.movie_language = language
                self.service.scan_source(apollo)
                self.assertEqual(self.db.movie(1)["match_state"], state)
                self.assertEqual(bool(self.posts), state == "ready")
        self.db.write("DELETE FROM listings")
        self.db.write("DELETE FROM history")
        self.posts.clear()
        self.movie_language = "en"
        self.db.set_config("settings", self.db.settings() | {"mode": "preview"})
        self.service.scan_source(apollo)
        self.assertEqual(self.db.movie(1)["match_state"], "ready")
        self.assertEqual(self.posts, [])

    def test_apollo_and_tv3_share_request_history(self):
        self.enable_apollo((101, 102))
        self.connect("auto")
        self.scan_all()
        self.assertEqual([p["mediaId"] for p in self.posts], [101, 102])
        self.assertEqual(self.db.rows("SELECT count(*) FROM listings")[0][0], 3)
        self.assertEqual(self.db.rows("SELECT count(*) FROM history")[0][0], 2)
        self.scan_all()
        self.assertEqual(len(self.posts), 2)

    def test_request_cap_is_shared_across_sources(self):
        self.source_ids = [101, 102]
        self.enable_apollo((103, 104))
        self.connect("auto")
        self.scan_all()
        self.assertEqual([p["mediaId"] for p in self.posts], [101, 102])
        self.assertEqual(self.db.rows("SELECT count(*) FROM listings WHERE match_state='ready'")[0][0], 4)
        self.scan_all()
        self.assertEqual([p["mediaId"] for p in self.posts], [101, 102, 103, 104])

    def test_apollo_failure_preserves_catalogue_and_other_source_continues(self):
        self.enable_apollo()
        self.scan_all()
        saved = dict(self.db.rows("SELECT * FROM listings WHERE source='apollo'")[0])
        self.source_ids.append(103)
        good_feed = list(self.apollo_feed)
        for bad in ([], {}, good_feed + [{}]):
            with self.subTest(feed=bad):
                self.apollo_feed = bad
                # Apollo fails first; TV3 must still run afterward.
                self.service.sources = {"apollo": apollo, "tv3": tv3}
                self.scan_all()
                self.assertEqual(dict(self.db.movie(saved["id"])), saved)
                self.assertTrue(self.db.rows("SELECT * FROM listings WHERE source='tv3' AND source_id='103'"))
                self.assertIn("Apollo Kino:", self.db.rows("SELECT * FROM events WHERE success=0")[-1]["message"])
        self.apollo_feed = good_feed
        self.scan_all()
        self.assertTrue(self.db.movie(saved["id"])["present"])

    def test_apollo_source_labels_links_and_posters_in_full_and_live_views(self):
        self.enable_apollo()
        self.connect()
        self.scan_all()
        listing = self.db.rows("SELECT * FROM listings WHERE source='apollo'")[0]
        for headers in ({}, {"X-TV2ARR-Refresh": "1"}):
            with self.subTest(headers=headers):
                response = self.browser.get("/movies", headers=headers)
                page = BeautifulSoup(response.data, "html.parser")
                cards = page.select(".movie-card")
                self.assertTrue(any("Apollo Kino" in card.get_text() for card in cards))
                self.assertTrue(any("TV3 Play" in card.get_text() for card in cards))
                detail = BeautifulSoup(self.browser.get(f'/movies/{listing["id"]}', headers=headers).data, "html.parser")
                self.assertEqual(detail.find("a", string="View on Apollo Kino")["href"], listing["url"])
                self.assertEqual(detail.select_one(".poster img")["src"], listing["poster"])
                self.assertIn(apollo.POSTER_ORIGIN, response.headers["Content-Security-Policy"])
                self.assertNotIn("img-src *", response.headers["Content-Security-Policy"])
                overview = self.browser.get("/", headers=headers).get_data(as_text=True)
                self.assertIn(apollo.URL, overview)

    def test_manual_request_and_history_survive_recreation(self):
        self.connect()
        self.scan()
        response = self.browser.get("/movies")
        self.assertIn("Original 101", response.get_data(as_text=True))
        self.assertNotIn("Ignore", response.get_data(as_text=True))
        self.assertEqual(self.browser.post("/movies/1/request").status_code, 403)
        response = self.browser.post("/movies/1/request", data={
            "csrf": self.csrf("/movies/1"),
        }, follow_redirects=True)
        self.assertIn("Request sent to Seerr", response.get_data(as_text=True))
        self.assertEqual(self.posts, [{"mediaType": "movie", "mediaId": 101, "is4k": False}])
        app = create_app(self.config, start_scheduler=False, service_factory=self.service_factory)
        service = app.extensions["tv2arr"]
        self.assertEqual(service.request(1), "Already in movie history")
        self.assertEqual(len(self.posts), 1)

    def test_original_titles_in_headings_candidates_tabs_and_live_updates(self):
        self.connect()
        self.scan()
        for headers in ({}, {"X-TV2ARR-Refresh": "1"}):
            with self.subTest(headers=headers):
                movies = BeautifulSoup(self.browser.get("/movies", headers=headers).data, "html.parser")
                self.assertEqual(movies.select_one(".movie-copy h2").get_text(), "Original 101")
                self.assertEqual(movies.select_one(".movie-copy h2")["lang"], "en")
                detail = BeautifulSoup(self.browser.get("/movies/1", headers=headers).data, "html.parser")
                self.assertEqual(detail.select_one(".film-detail h1").get_text(), "Original 101")
                self.assertEqual(detail.select_one(".film-detail .muted span[lang=lv]").get_text(), "Filma 101")
                self.assertEqual(detail.select_one(".candidates strong").get_text(), "Original 101")
                if not headers:
                    self.assertEqual(detail.title.get_text(), "Original 101 · tv2arr")

        # Before enrichment, fall back to the title supplied by TV3.
        self.db.write("UPDATE listings SET original_title='', candidates='[]', tmdb_id=NULL WHERE id=1")
        detail = BeautifulSoup(self.browser.get("/movies/1").data, "html.parser")
        self.assertEqual(detail.h1.get_text(), "Filma 101")
        self.assertEqual(detail.h1["lang"], "lv")

    def test_verified_original_title_takes_priority_and_is_saved_in_history(self):
        self.movie_original_title = "Dzīvais ūdens"
        self.movie_language = "lv"
        self.connect("auto")
        self.scan()
        listing = self.db.movie(1)
        self.assertEqual(listing["original_title"], "Original 101")
        self.assertEqual(listing["matched_original_title"], "Dzīvais ūdens")
        detail = BeautifulSoup(self.browser.get("/movies/1").data, "html.parser")
        self.assertEqual(detail.h1.get_text(), "Dzīvais ūdens")
        self.assertEqual(detail.h1["lang"], "lv")
        self.assertEqual(self.db.rows("SELECT title FROM history")[0]["title"], "Dzīvais ūdens")
        history = BeautifulSoup(self.browser.get("/history").data, "html.parser")
        self.assertEqual(history.select_one(".activity strong").get_text(), "Dzīvais ūdens")

    def test_existing_database_and_history_use_cached_original_titles(self):
        self.connect()
        self.scan()
        self.service.request(1)
        self.db.write("UPDATE history SET title='Latvian translation'")
        self.db.write("UPDATE listings SET original_title='Source English title'")
        # Simulate the schema from the previous release, preserving its data.
        with self.db.connect() as db:
            db.execute("ALTER TABLE listings DROP COLUMN matched_original_title")
            db.execute("ALTER TABLE listings DROP COLUMN matched_poster")
        migrated = Storage(self.db.path)
        self.assertEqual(migrated.movie(1)["matched_original_title"], "")
        self.assertIsNone(migrated.movie(1)["matched_poster"])
        self.assertEqual(migrated.movie(1)["poster"], "https://tv3cdn.lv/frame-101.jpg")
        self.assertEqual(migrated.movie(1)["tmdb_id"], 101)
        self.assertEqual(migrated.rows("SELECT count(*) FROM history")[0][0], 1)
        detail = BeautifulSoup(self.browser.get("/movies/1").data, "html.parser")
        self.assertEqual(detail.h1.get_text(), "Original 101")
        history = BeautifulSoup(self.browser.get("/history").data, "html.parser")
        self.assertEqual(history.select_one(".activity strong").get_text(), "Original 101")
        self.assertEqual(len(self.posts), 1)

        # History remains readable even if its source listing has been removed.
        self.db.write("DELETE FROM listings")
        history = BeautifulSoup(self.browser.get("/history").data, "html.parser")
        self.assertEqual(history.select_one(".activity strong").get_text(), "Latvian translation")

    def test_manual_match_updates_poster_and_rematch_without_poster_uses_source(self):
        self.connect()
        self.scan()
        self.movie_poster_path = "/chosen-poster.jpg"
        response = self.browser.post("/movies/1/match", data={
            "csrf": self.csrf("/movies/1"), "tmdb_id": 202,
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        poster = "https://image.tmdb.org/t/p/w500/chosen-poster.jpg"
        self.assertEqual(self.db.movie(1)["matched_poster"], poster)
        self.assertEqual(self.db.movie(1)["poster"], "https://tv3cdn.lv/frame-101.jpg")
        for headers in ({}, {"X-TV2ARR-Refresh": "1"}):
            for url in ("/movies", "/movies/1"):
                with self.subTest(headers=headers, url=url):
                    response = self.browser.get(url, headers=headers)
                    page = BeautifulSoup(response.data, "html.parser")
                    self.assertEqual(page.select_one(".poster img")["src"], poster)
                    policy = response.headers["Content-Security-Policy"]
                    images = next(rule.split() for rule in policy.split(";") if rule.strip().startswith("img-src"))
                    self.assertIn("https://image.tmdb.org", images)
                    self.assertNotIn("*", images)
        self.assertEqual(Storage(self.db.path).movie(1)["matched_poster"], poster)

        self.movie_poster_path = None
        response = self.browser.post("/movies/1/match", data={
            "csrf": self.csrf("/movies/1"), "tmdb_id": 303,
        }, follow_redirects=True)
        page = BeautifulSoup(response.data, "html.parser")
        self.assertEqual(page.select_one(".poster img")["src"], "https://tv3cdn.lv/frame-101.jpg")
        self.assertEqual(self.db.movie(1)["matched_poster"], "")
        self.scan()
        self.assertEqual(self.db.movie(1)["tmdb_id"], 303)

    def test_scan_backfills_posters_without_replacing_existing_manual_match(self):
        self.connect()
        self.scan()
        self.db.save_match(1, {"id": 999, "title": "Manual selection", "originalLanguage": "lv"})
        self.db.write("UPDATE listings SET matched_poster=NULL WHERE id=1")
        self.movie_poster_path = "/existing-match.jpg"
        self.scan()
        listing = self.db.movie(1)
        self.assertEqual(listing["tmdb_id"], 999)
        self.assertEqual(listing["matched_title"], "Manual selection")
        self.assertEqual(listing["auto_match"], 0)
        self.assertEqual(listing["matched_poster"], "https://image.tmdb.org/t/p/w500/existing-match.jpg")
        self.assertEqual(self.movie_lookups[-1], 999)
        lookups = len(self.movie_lookups)
        self.scan()
        self.assertEqual(len(self.movie_lookups), lookups)
        self.assertEqual(self.posts, [])

    def test_missing_poster_is_not_rechecked_every_scan(self):
        self.connect()
        self.scan()
        self.db.write("UPDATE listings SET matched_poster=NULL WHERE id=1")
        self.scan()
        self.assertEqual(self.db.movie(1)["matched_poster"], "")
        lookups = len(self.movie_lookups)
        self.scan()
        self.assertEqual(len(self.movie_lookups), lookups)

    def test_poster_refresh_failure_retries_and_preserves_match(self):
        self.connect()
        self.scan()
        self.db.write("UPDATE listings SET matched_poster=NULL WHERE id=1")
        fake_seerr = self.fake_seerr

        def unavailable_details(request):
            if request.url.path == "/seerr/api/v1/movie/101":
                return httpx.Response(503)
            return fake_seerr(request)

        self.fake_seerr = unavailable_details
        self.scan()
        self.assertIsNone(self.db.movie(1)["matched_poster"])
        self.assertEqual(self.db.movie(1)["tmdb_id"], 101)
        self.fake_seerr = fake_seerr
        self.movie_poster_path = "/retried.jpg"
        self.scan()
        self.assertEqual(self.db.movie(1)["matched_poster"], "https://image.tmdb.org/t/p/w500/retried.jpg")

    def test_baseline_auto_request_and_language_gate(self):
        self.connect("auto")
        self.scan()
        self.assertEqual([item["mediaId"] for item in self.posts], [101])
        self.source_ids += [102]
        self.scan()
        self.assertEqual([item["mediaId"] for item in self.posts], [101, 102])
        self.movie_language = "fr"
        self.source_ids += [103]
        self.scan()
        self.assertEqual(self.db.movie(3)["match_state"], "excluded")
        with self.assertRaises(SeerrError):
            self.service.request(3)
        self.db.write("DELETE FROM history WHERE tmdb_id=101")  # Simulate expired local history.
        with self.assertRaises(SeerrError):
            self.service.request(1)  # changed upstream after initial match
        self.assertEqual(len(self.posts), 2)

    def test_auto_requests_existing_manual_matches_and_respects_cap(self):
        self.source_ids = [101, 102, 103]
        self.connect()
        self.scan()
        for listing in self.db.rows("SELECT * FROM listings"):
            self.db.save_match(listing["id"], {
                "id": int(listing["source_id"]), "title": listing["title"], "originalLanguage": "lv",
            })
        self.db.set_config("settings", self.db.settings() | {"mode": "auto"})
        self.scan()
        self.assertEqual([item["mediaId"] for item in self.posts], [101, 102])
        self.scan()
        self.assertEqual([item["mediaId"] for item in self.posts], [101, 102, 103])
        self.scan()
        self.assertEqual(len(self.posts), 3)

    def test_auto_skips_existing_seerr_requests_without_using_submission_cap(self):
        self.source_ids = [101, 102, 103]
        self.connect()
        self.scan()
        # One movie already exists in Seerr but has no local history yet.
        fake_seerr = self.fake_seerr

        def remote_request(request):
            if request.url.path == "/seerr/api/v1/movie/101":
                return httpx.Response(200, json={
                    "id": 101, "title": "Original 101", "originalLanguage": "en",
                    "mediaInfo": {"status": 2, "requests": [{"id": 51, "status": 1}]},
                })
            return fake_seerr(request)

        self.fake_seerr = remote_request
        self.db.set_config("settings", self.db.settings() | {"mode": "auto"})
        self.scan()
        self.assertEqual([item["mediaId"] for item in self.posts], [102, 103])
        self.assertEqual(self.db.rows("SELECT outcome FROM history WHERE tmdb_id=101")[0]["outcome"],
                         "Already requested in Seerr")

    def test_auto_ignores_absent_unmatched_and_unconfirmed_movies(self):
        self.source_ids = [101, 102, 103, 104, 105]
        self.connect()
        self.scan()
        self.source_ids.remove(101)
        self.db.write("UPDATE listings SET match_state='review', auto_match=0 WHERE id=2")
        self.db.write("UPDATE listings SET match_state='excluded', language='fr' WHERE id=3")
        self.db.write("INSERT INTO history VALUES (104, 'Original 104', 'tv3', ?, 'Confirmation needed', NULL)",
                      (now_stamp(),))
        self.db.set_config("settings", self.db.settings() | {"mode": "auto"})
        self.scan()
        self.assertEqual([item["mediaId"] for item in self.posts], [105])
        with self.assertRaisesRegex(SeerrError, "no longer listed"):
            self.service.request(1, automatic=True)

    def test_saving_manual_match_in_auto_mode_requests_it(self):
        self.connect("auto")
        self.search_available = False
        self.scan()
        self.assertEqual(self.db.movie(1)["match_state"], "missing")
        response = self.browser.post("/movies/1/match", data={
            "csrf": self.csrf("/movies/1"), "tmdb_id": 101,
        })
        self.assertEqual(response.status_code, 303)
        with self.service.scan_lock:
            self.assertEqual(self.db.movie(1)["auto_match"], 0)
            self.assertEqual([item["mediaId"] for item in self.posts], [101])

    def test_missing_movies_are_visible_and_cannot_be_requested(self):
        self.connect("auto")
        self.search_available = False
        self.scan()
        listing = self.db.movie(1)
        self.assertEqual(listing["match_state"], "missing")
        self.assertEqual(listing["candidates"], "[]")
        self.assertIsNone(listing["tmdb_id"])
        self.assertIsNotNone(listing["match_checked_at"])
        self.assertEqual(self.posts, [])
        with self.assertRaises(SeerrError):
            self.service.request(1)
        for headers in ({}, {"X-TV2ARR-Refresh": "1"}):
            for url in ("/movies", "/movies?view=review", "/movies?view=missing"):
                with self.subTest(headers=headers, url=url):
                    response = self.browser.get(url, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    page = BeautifulSoup(response.data, "html.parser")
                    self.assertEqual(page.select_one(".tag").get_text(), "Missing from TMDb")
            detail = BeautifulSoup(self.browser.get("/movies/1", headers=headers).data, "html.parser")
            self.assertIn("Missing from TMDb", detail.get_text())
            self.assertIn("once 24 hours have passed", detail.get_text())
            self.assertIsNone(detail.select_one('form[action$="/request"]'))
            overview = BeautifulSoup(self.browser.get("/", headers=headers).data, "html.parser")
            self.assertEqual(overview.select_one('a.metric[href$="view=review"] strong').get_text(), "1")
        page = BeautifulSoup(self.browser.get("/movies?view=missing").data, "html.parser")
        self.assertEqual(page.select_one('.filters [aria-current="page"]').get_text(), "Missing from TMDb")

    def test_missing_retry_waits_24_hours_and_survives_restart(self):
        self.connect()
        self.search_available = False
        checked = now_stamp()
        with patch("tv2arr.service.now_stamp", return_value=checked):
            self.scan()
        query_count = len(self.search_queries)
        self.service = self.service_factory(Storage(self.db.path))
        self.search_available = True
        with patch("tv2arr.service.now_stamp", return_value=checked + MISSING_RETRY_SECONDS - 1):
            self.scan()
        self.assertEqual(len(self.search_queries), query_count)
        self.assertEqual(self.db.movie(1)["match_state"], "missing")
        with patch("tv2arr.service.now_stamp", return_value=checked + MISSING_RETRY_SECONDS):
            self.scan()
        self.assertGreater(len(self.search_queries), query_count)
        self.assertEqual(self.db.movie(1)["match_state"], "ready")
        self.assertEqual(self.db.movie(1)["tmdb_id"], 101)
        self.assertEqual(self.posts, [])
        self.assertNotIn("Original 101", self.browser.get("/movies?view=missing").get_data(as_text=True))

    def test_missing_retry_recovers_with_automatic_requests_and_language_gate(self):
        self.connect("auto")
        for language, state, requested in (("en", "ready", True), ("fr", "excluded", False),
                                            ("", "review", False)):
            with self.subTest(language=language):
                self.db.write("DELETE FROM listings")
                self.db.write("DELETE FROM history")
                self.posts.clear()
                self.search_available = False
                self.scan()
                self.assertEqual(self.posts, [])
                self.search_available = True
                self.movie_language = language
                self.db.write("UPDATE listings SET match_checked_at=?", (now_stamp() - MISSING_RETRY_SECONDS,))
                self.scan()
                self.assertEqual(self.db.movie(1)["match_state"], state)
                self.assertEqual(bool(self.posts), requested)

    def test_missing_retries_skip_absent_movies_and_keep_manual_matches(self):
        self.source_ids = [101, 102, 103]
        self.connect()
        self.search_available = False
        self.scan()
        self.db.save_match(2, {"id": 999, "title": "Manual selection", "originalLanguage": "lv"})
        self.db.write("UPDATE listings SET match_checked_at=?", (now_stamp() - MISSING_RETRY_SECONDS,))
        self.source_ids.remove(101)
        query_count = len(self.search_queries)
        self.search_available = True
        self.scan()
        self.assertEqual(self.search_queries[query_count:], ["Original 103", "Filma 103"])
        self.assertEqual(self.db.movie(1)["match_state"], "missing")
        self.assertEqual(self.db.movie(1)["present"], 0)
        self.assertEqual(self.db.movie(2)["tmdb_id"], 999)
        self.assertEqual(self.db.movie(2)["auto_match"], 0)
        self.assertEqual(self.db.movie(3)["match_state"], "ready")

    def test_missing_retry_error_preserves_status_and_retries_next_scan(self):
        self.connect()
        self.search_available = False
        self.scan()
        checked = now_stamp() - MISSING_RETRY_SECONDS
        self.db.write("UPDATE listings SET match_checked_at=?", (checked,))
        self.search_available = True
        self.rejected_title = "Filma 101"  # Fail after the original-title search succeeded.
        self.scan()
        listing = self.db.movie(1)
        self.assertEqual(listing["match_state"], "missing")
        self.assertEqual(listing["match_checked_at"], checked)
        self.assertEqual(listing["candidates"], "[]")
        self.assertTrue(self.db.rows("SELECT * FROM events WHERE success=0"))
        self.rejected_title = ""
        self.scan()
        self.assertEqual(self.db.movie(1)["match_state"], "ready")

    def test_ambiguous_candidates_stay_in_review_without_daily_retry(self):
        self.connect()
        fake_seerr = self.fake_seerr

        def wrong_year(request):
            response = fake_seerr(request)
            if request.url.path == "/seerr/api/v1/search":
                data = response.json()
                for item in data["results"]:
                    item["releaseDate"] = "1990-01-01"
                return httpx.Response(200, json=data)
            return response

        self.fake_seerr = wrong_year
        self.scan()
        self.assertEqual(self.db.movie(1)["match_state"], "review")
        self.assertNotEqual(self.db.movie(1)["candidates"], "[]")
        query_count = len(self.search_queries)
        self.db.write("UPDATE listings SET match_checked_at=?", (now_stamp() - MISSING_RETRY_SECONDS,))
        self.scan()
        self.assertEqual(len(self.search_queries), query_count)
        self.assertNotIn("Original 101", self.browser.get("/movies?view=missing").get_data(as_text=True))

    def test_upgrade_rechecks_old_empty_searches_and_preserves_saved_matches(self):
        self.source_ids = [101, 102]
        self.connect()
        self.scan()
        self.service.request(2)
        self.db.write("""UPDATE listings SET match_state='review', candidates='[]', tmdb_id=NULL,
                         matched_title=NULL, language=NULL, auto_match=0 WHERE id=1""")
        with self.db.connect() as db:
            db.execute("ALTER TABLE listings DROP COLUMN match_checked_at")
        migrated = Storage(self.db.path)
        self.assertIsNone(migrated.movie(1)["match_checked_at"])
        self.db.set_config("scan_matching_revision", 1)
        self.db.set_config("scan_lookup_revision", self.db.settings()["lookup_revision"])
        self.search_available = False
        self.service.schedule_tick(now_stamp())
        with self.service.scan_lock:
            self.assertEqual(self.db.movie(1)["match_state"], "missing")
            self.assertIsNotNone(self.db.movie(1)["match_checked_at"])
            self.assertEqual(self.db.movie(2)["tmdb_id"], 102)
            self.assertEqual(self.db.rows("SELECT count(*) FROM history")[0][0], 1)
            self.assertFalse(self.service.seerr_lookup_due(self.db.settings()))

    def test_connection_retest_retries_missing_movies_before_daily_deadline(self):
        self.connect()
        self.assertTrue(self.service.scan_async())
        with self.service.scan_lock:
            self.db.write("UPDATE listings SET match_state='new', tmdb_id=NULL WHERE id=1")
        self.search_available = False
        self.scan()
        self.assertEqual(self.db.movie(1)["match_state"], "missing")
        self.search_available = True
        response = self.browser.post("/settings", data={
            "csrf": self.csrf("/settings"), "url": "http://seerr:5055/seerr/",
            "api_key": "", "mode": "manual", "interval": 6, "limit": 2, "action": "test",
        }, follow_redirects=True)
        self.assertIn("Seerr connection verified", response.get_data(as_text=True))
        self.service.schedule_tick(now_stamp())
        with self.service.scan_lock:
            self.assertEqual(self.db.movie(1)["match_state"], "ready")

    def test_enabling_auto_mode_wakes_scan_for_existing_matches(self):
        self.connect()
        self.assertTrue(self.service.scan_async())
        with self.service.scan_lock:
            self.assertFalse(self.service.seerr_lookup_due(self.db.settings()))
        response = self.browser.post("/settings", data={
            "csrf": self.csrf("/settings"), "url": "http://seerr:5055/seerr/",
            "api_key": "", "mode": "auto", "interval": 6, "limit": 2, "action": "save",
        })
        self.assertEqual(response.status_code, 303)
        self.service.schedule_tick(now_stamp())
        with self.service.scan_lock:
            self.assertEqual([item["mediaId"] for item in self.posts], [101])

    def test_preview_mode_does_not_auto_request_matches(self):
        self.connect("preview")
        self.scan()
        self.assertEqual(self.db.movie(1)["match_state"], "ready")
        self.assertEqual(self.posts, [])
        with self.assertRaisesRegex(SeerrError, "disabled"):
            self.service.request(1, automatic=True)

    def test_uncertain_request_and_nightly_expiry(self):
        self.connect()
        self.scan()
        self.timed_out = True
        with self.assertRaises(SeerrError):
            self.service.request(1)
        with self.assertRaisesRegex(SeerrError, "unconfirmed"):
            self.service.request(1)
        self.remote_has_request = True
        self.assertEqual(self.service.request(1), "Already requested in Seerr")

        now = int(datetime(2026, 11, 18, 4, tzinfo=RIGA).timestamp())
        self.db.write("UPDATE history SET recorded_at=?", (retention_cutoff(now) - 1,))
        self.db.write("INSERT INTO history VALUES (102, 'Recent', 'tv3', ?, 'Confirmation needed', NULL)", (now,))
        self.service.maintenance_if_due(now)
        self.service.maintenance_if_due(now + int(timedelta(hours=1).total_seconds()))
        self.assertEqual([record["tmdb_id"] for record in self.db.rows("SELECT * FROM history")], [102])
        self.assertEqual(len(self.db.rows("SELECT * FROM events WHERE kind='Maintenance'")), 1)
        self.assertEqual(self.service.request(1), "Already requested in Seerr")
        self.assertEqual(len(self.posts), 1)

    def test_lan_allowlist_uses_peer_address(self):
        external = self.browser.get("/health", environ_overrides={"REMOTE_ADDR": "192.168.3.5"},
                                    headers={"X-Forwarded-For": "192.168.2.7"})
        self.assertEqual(external.status_code, 403)
        local = self.browser.get("/health", environ_overrides={"REMOTE_ADDR": "192.168.2.7"})
        self.assertEqual(local.status_code, 200)

    def test_connecting_seerr_looks_up_previous_scan_immediately(self):
        self.source_ids = [101, 102]
        self.assertTrue(self.service.scan_async())
        with self.service.scan_lock:
            self.assertEqual(self.db.movie(1)["match_state"], "new")
            self.db.write("UPDATE listings SET match_state='review' WHERE id=2")
        self.connect()
        self.service.schedule_tick(now_stamp())
        with self.service.scan_lock:
            self.assertEqual(self.db.movie(1)["match_state"], "ready")
            self.assertEqual(self.db.movie(2)["match_state"], "ready")
        self.assertFalse(self.service.seerr_lookup_due(self.db.settings()))
        self.assertEqual(self.posts, [])

    def test_connection_verifies_seerr_search_in_addition_to_authentication(self):
        self.search_available = False
        response = self.submit_connection()
        self.assertIn("movie search returned no results", response.get_data(as_text=True))
        self.assertFalse(self.db.settings()["url"])

    def test_matching_update_rechecks_cached_review_entries_once(self):
        self.source_ids = [101, 102, 103]
        self.connect()
        self.assertTrue(self.service.scan_async())
        with self.service.scan_lock:
            self.assertFalse(self.service.seerr_lookup_due(self.db.settings()))
            self.db.write(
                """UPDATE listings SET match_state='review', tmdb_id=NULL,
                   matched_title=NULL, language=NULL, auto_match=0 WHERE id=1"""
            )
            self.assertNotEqual(self.db.movie(1)["candidates"], "[]")
            self.db.save_match(2, {"id": 999, "title": "Manual selection", "originalLanguage": "lv"})
            self.db.save_match(3, {"id": 103, "title": "Excluded", "originalLanguage": "fr"})
            self.db.set_config("scan_matching_revision", 0)

        self.service.schedule_tick(now_stamp())
        with self.service.scan_lock:
            listing = self.db.movie(1)
            self.assertEqual(listing["match_state"], "ready")
            self.assertEqual(listing["tmdb_id"], 101)
            self.assertEqual(listing["auto_match"], 1)
            self.assertEqual(self.db.movie(2)["tmdb_id"], 999)
            self.assertEqual(self.db.movie(2)["auto_match"], 0)
            self.assertEqual(self.db.movie(3)["match_state"], "excluded")
            self.assertFalse(self.service.seerr_lookup_due(self.db.settings()))
            self.assertEqual(self.posts, [])  # Manual mode only updates matches.
            self.db.write("UPDATE listings SET match_state='review', tmdb_id=NULL WHERE id=1")

        self.assertTrue(self.service.scan_async())
        with self.service.scan_lock:
            self.assertEqual(self.db.movie(1)["match_state"], "review")

    def test_invalid_lookup_is_diagnosed_without_blocking_other_movies(self):
        self.source_ids = [101, 102]
        self.rejected_title = "Original 101"
        self.connect()
        self.scan()
        self.assertEqual(self.db.movie(1)["match_state"], "new")
        self.assertEqual(self.db.movie(2)["match_state"], "ready")
        errors = " ".join(row["message"] for row in self.db.rows("SELECT * FROM events WHERE success=0"))
        self.assertIn("lookup for Filma 101", errors)
        self.assertIn("HTTP 400 for GET /api/v1/search. Invalid search query.", errors)
        self.assertNotIn(API_KEY, errors)

    def test_search_retries_without_an_explicitly_rejected_language(self):
        self.reject_latvian_locale = True
        self.connect()
        self.scan()
        self.assertEqual(self.db.movie(1)["match_state"], "ready")

    def test_search_percent_encodes_spaces_and_reserved_characters(self):
        examples = {
            "Kā savaldzināt princi": b"K%C4%81%20savaldzin%C4%81t%20princi",
            "Uzzīmēt Ziemassvētkus": b"Uzz%C4%ABm%C4%93t%20Ziemassv%C4%93tkus",
            "Ziemassvētki Šveicē": b"Ziemassv%C4%93tki%20%C5%A0veic%C4%93",
            "Ocean's Eleven (2001) + A&B?": b"Ocean%27s%20Eleven%20%282001%29%20%2B%20A%26B%3F",
        }

        def validate_search(request):
            self.assertEqual(request.url.path, "/api/v1/search")
            title = request.url.params["query"]
            raw = dict(part.split(b"=", 1) for part in request.url.query.split(b"&"))
            self.assertEqual(raw[b"query"], examples[title])
            self.assertEqual(request.url.params["page"], "1")
            self.assertEqual(request.url.params["language"], "lv")
            return httpx.Response(200, json={"results": [{"id": 1, "mediaType": "movie", "title": title}]})

        with Seerr("http://seerr:5055", API_KEY, httpx.MockTransport(validate_search)) as api:
            for title in examples:
                with self.subTest(title=title):
                    self.assertEqual(api.search(title, "lv")[0]["title"], title)


if __name__ == "__main__":
    unittest.main()
