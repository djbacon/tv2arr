import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

from tv2arr.app import create_app
from tv2arr.seerr import Seerr, SeerrError
from tv2arr.service import Service
from tv2arr.sources import tv3
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
        self.rejected_title = ""
        self.reject_latvian_locale = False
        self.source_ids = [101]
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
            db, seerr=lambda url, key: Seerr(url, key, httpx.MockTransport(self.fake_seerr)),
            source_transport=httpx.MockTransport(self.fake_tv3),
        )

    def fake_tv3(self, request):
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
        self.assertEqual(self.db.movie(1)["match_state"], "review")
        response = self.browser.post("/movies/1/match", data={
            "csrf": self.csrf("/movies/1"), "tmdb_id": 101,
        })
        self.assertEqual(response.status_code, 303)
        with self.service.scan_lock:
            self.assertEqual(self.db.movie(1)["auto_match"], 0)
            self.assertEqual([item["mediaId"] for item in self.posts], [101])

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
