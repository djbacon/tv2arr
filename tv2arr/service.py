import json
import logging
from datetime import datetime, timedelta
from threading import Event, Lock, Thread

import httpx

from .seerr import LANGUAGES, Seerr, SeerrError, existing_request, find_candidates, poster_url
from .sources import apollo, replay, tv3
from .storage import RIGA, now_stamp
from .titles import movie_title

log = logging.getLogger(__name__)
MATCHING_REVISION = 2
MISSING_RETRY_SECONDS = 24 * 3600


class Service:
    def __init__(self, db, sources=(tv3, apollo, replay), seerr=Seerr, source_transport=None):
        self.db = db
        self.sources = {source.SLUG: source for source in sources}
        self.seerr = seerr
        self.source_transport = source_transport
        self.scan_lock = Lock()
        self.request_lock = Lock()
        self.wake = Event()

    def api(self, settings=None):
        settings = self.db.settings() if settings is None else settings
        return self.seerr(settings["url"], settings["api_key"])

    def source_client(self):
        return httpx.Client(
            timeout=10, trust_env=False, transport=self.source_transport,
            headers={"User-Agent": "tv2arr/0.1 (public movie catalogue)"},
        )

    def scan_async(self):
        if not self.scan_lock.acquire(blocking=False):
            return False
        Thread(target=self._scan, name="catalogue-scan", daemon=True).start()
        return True

    def _scan(self):
        try:
            settings = self.db.settings()
            self.db.set_config("scan_started", now_stamp())
            if self.seerr_lookup_due(settings):
                matching_changed = self.db.config("scan_matching_revision", 0) != MATCHING_REVISION
                self.db.write(
                    """UPDATE listings SET match_state='new'
                       WHERE present=1 AND ignored=0 AND match_state IN ('review','missing') AND tmdb_id IS NULL
                       AND (candidates='[]' OR ?)""",
                    (matching_changed,),
                )
                self.db.set_config("scan_lookup_revision", settings["lookup_revision"])
                self.db.set_config("scan_matching_revision", MATCHING_REVISION)
            remaining = settings["limit"]
            for source in self.sources.values():
                try:
                    remaining -= self.scan_source(source, settings | {"limit": remaining})
                except (httpx.HTTPError, ValueError) as error:
                    message = str(error) if isinstance(error, ValueError) else "Could not load the listing."
                    self.db.event("Scan", f"{source.NAME}: {message}", False)
        except Exception:
            log.exception("Catalogue scan stopped unexpectedly")
            self.db.event("Scan", "Catalogue scan stopped unexpectedly.", False)
        finally:
            self.scan_lock.release()

    def scan_source(self, source, settings=None):
        settings = self.db.settings() if settings is None else settings
        configured = settings["url"] and settings["api_key"]
        failures = 0
        with self.source_client() as web:
            catalogue = source.listing(web)
            listings = catalogue.listings
            new, baseline = self.db.observe(source.SLUG, listings, now_stamp())
            queue = self.db.rows(
                """SELECT * FROM listings WHERE source=? AND present=1 AND ignored=0
                   AND (enriched=0 OR match_state='new'
                        OR (match_state='missing' AND tmdb_id IS NULL
                            AND (match_checked_at IS NULL OR match_checked_at <= ?))
                        OR (tmdb_id IS NOT NULL AND matched_poster IS NULL)) ORDER BY id""",
                (source.SLUG, now_stamp() - MISSING_RETRY_SECONDS),
            )
            if configured:
                with self.api(settings) as api:
                    for listing in queue:
                        try:
                            if listing["tmdb_id"]:
                                if not listing["enriched"]:
                                    listing = self.enrich(listing, source, web, catalogue)
                                self.refresh_poster(listing, api)
                            else:
                                self.match(listing, source, web, api, catalogue)
                        except SeerrError as error:
                            failures += 1
                            self.db.event("Scan", f"{source.NAME} lookup for {movie_title(listing)}: {error}", False)
                            if error.status not in {400, 404, 422}:
                                break
                        except (httpx.HTTPError, ValueError):
                            failures += 1
            else:
                for listing in queue:
                    if not listing["enriched"]:
                        try:
                            self.enrich(listing, source, web, catalogue)
                        except (httpx.HTTPError, ValueError):
                            failures += 1

        auto_count = self.auto_request(source.SLUG, settings)
        message = f"{source.NAME}: {len(listings)} listed, {new} new."
        if baseline:
            message += " First scan saved."
        if not configured:
            message += " Connect Seerr in Settings to look up movies."
        if auto_count:
            message += f" {auto_count} requests sent."
        if failures:
            message += f" {failures} lookup errors. Unfinished lookups will retry on a later scan."
        self.db.event("Scan", message, not failures)
        return auto_count

    def enrich(self, listing, source, web, catalogue):
        if listing["source_id"] in catalogue.details:
            original, year, poster = catalogue.details[listing["source_id"]]
        else:
            original, year, poster = source.details(web, listing)
        self.db.write(
            "UPDATE listings SET original_title=?, year=?, poster=?, enriched=1 WHERE id=?",
            (original, year, poster, listing["id"]),
        )
        return self.db.movie(listing["id"])

    def match(self, listing, source, web, api, catalogue):
        if not listing["enriched"]:
            listing = self.enrich(listing, source, web, catalogue)
        candidates, exact = find_candidates(api, listing)
        if exact:
            self.db.save_match(listing["id"], api.movie(exact), auto=True)
        else:
            self.db.write(
                """UPDATE listings SET match_state=?, tmdb_id=NULL, matched_title=NULL,
                   matched_original_title='', matched_poster=NULL, language=NULL, auto_match=0 WHERE id=?""",
                ("review" if candidates else "missing", listing["id"]),
            )
        self.db.write(
            "UPDATE listings SET candidates=?, match_checked_at=? WHERE id=?",
            (json.dumps(candidates), now_stamp(), listing["id"]),
        )

    def refresh_poster(self, listing, api):
        movie = api.movie(listing["tmdb_id"])
        # NULL means an older match still needs its poster checked; '' means no poster exists.
        self.db.write("UPDATE listings SET matched_poster=? WHERE id=? AND tmdb_id=?",
                      (poster_url(movie), listing["id"], listing["tmdb_id"]))

    def auto_request(self, source, settings):
        if settings["mode"] != "auto" or not settings["url"] or not settings["api_key"]:
            return 0

        listings = self.db.rows(
            """SELECT l.* FROM listings l WHERE l.source=? AND l.present=1 AND l.ignored=0
               AND l.match_state='ready' AND l.tmdb_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM history h WHERE h.tmdb_id=l.tmdb_id)
               ORDER BY l.first_seen DESC, l.id""",
            (source,),
        )
        count = 0
        for listing in listings:
            if count >= settings["limit"]:
                break
            try:
                outcome = self.request(listing["id"], automatic=True)
                count += outcome == "Request sent to Seerr"
            except SeerrError as error:
                self.db.event("Scan", f"Automatic request: {error}", False)
                break
        return count

    def request(self, listing_id, automatic=False):
        with self.request_lock:
            return self._request(listing_id, automatic)

    def _request(self, listing_id, automatic):
        settings = self.db.settings()
        listing = self.db.movie(listing_id)
        if listing and listing["ignored"]:
            raise SeerrError("Restore this movie before requesting it.")
        if settings["mode"] == "preview" or (automatic and settings["mode"] != "auto"):
            raise SeerrError("Request sending is disabled by the current mode.")
        if not listing or listing["match_state"] != "ready" or listing["language"] not in LANGUAGES:
            raise SeerrError("Choose a movie with verified Latvian or English original audio first.")
        if automatic and not listing["present"]:
            raise SeerrError("This movie is no longer listed on the source.")
        tmdb_id = listing["tmdb_id"]
        previous = self.db.rows("SELECT outcome FROM history WHERE tmdb_id=?", (tmdb_id,))
        if previous and previous[0]["outcome"] != "Confirmation needed":
            return "Already in movie history"

        with self.api(settings) as api:
            movie = api.movie(tmdb_id)
            if movie.get("originalLanguage") not in LANGUAGES:
                self.db.save_match(listing_id, movie)
                raise SeerrError("Seerr must verify that the original language is Latvian or English.")
            if movie.get("originalTitle"):
                self.db.write("UPDATE listings SET matched_original_title=? WHERE id=?",
                              (movie["originalTitle"], listing_id))
                listing = self.db.movie(listing_id)
            existing, seerr_id = existing_request(movie)
            if existing:
                self.record(listing, existing, seerr_id)
                return existing
            if previous:
                raise SeerrError("The earlier submission is still unconfirmed. No duplicate was sent.")

            self.record(listing, "Confirmation needed")
            try:
                seerr_id = api.submit(tmdb_id, settings)
            except SeerrError as error:
                if not error.uncertain:
                    self.db.write("DELETE FROM history WHERE tmdb_id=?", (tmdb_id,))
                raise
            self.record(listing, "Request sent to Seerr", seerr_id)
            return "Request sent to Seerr"

    def record(self, listing, outcome, seerr_id=None):
        self.db.write(
            """INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(tmdb_id) DO UPDATE SET title=excluded.title,
                   outcome=excluded.outcome, seerr_id=excluded.seerr_id""",
            (listing["tmdb_id"], movie_title(listing), listing["source"], now_stamp(), outcome, seerr_id),
        )

    def maintenance_if_due(self, now):
        local = datetime.fromtimestamp(now, RIGA)
        scheduled = local.date()
        if local.hour < 4:
            scheduled -= timedelta(days=1)
        day = scheduled.isoformat()
        if self.db.config("maintenance", "") < day:
            with self.request_lock:
                self.db.maintain(now, day)

    def schedule_tick(self, now):
        self.maintenance_if_due(now)
        settings = self.db.settings()
        last = self.db.config("scan_started")
        scheduled = last is not None and now - last >= settings["interval"] * 3600
        if scheduled or self.seerr_lookup_due(settings):
            self.scan_async()

    def seerr_lookup_due(self, settings):
        return bool(
            settings["url"] and settings["api_key"]
            and (
                settings["lookup_revision"] != self.db.config("scan_lookup_revision", -1)
                or self.db.config("scan_matching_revision", 0) != MATCHING_REVISION
            )
        )

    def run_scheduler(self):
        while True:
            try:
                self.schedule_tick(now_stamp())
            except Exception:
                log.exception("Scheduled maintenance could not finish")
                try:
                    self.db.event("Maintenance", "Scheduled maintenance could not finish.", False)
                except Exception:
                    log.exception("Could not save maintenance error")
                self.wake.wait(300)
            else:
                self.wake.wait(30)
            self.wake.clear()
