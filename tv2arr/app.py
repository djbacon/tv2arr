import hmac
import json
import os
import secrets
from datetime import datetime
from ipaddress import ip_address, ip_network
from pathlib import Path
from threading import Thread

from flask import (
    Flask, abort, flash, redirect, render_template, request, session, url_for,
)

from . import __version__
from .seerr import LANGUAGES, SeerrError, base_url, find_candidates
from .service import Service
from .sources.apollo import POSTER_ORIGIN
from .sources.replay import POSTER_ORIGIN as REPLAY_POSTER_ORIGIN
from .storage import RIGA, Storage, now_stamp
from .titles import movie_title, title_language

PRIVATE_NETWORKS = tuple(ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def create_app(config=None, *, start_scheduler=True, service_factory=Service):
    release = f"v{__version__}"
    app = Flask(__name__, static_url_path=f"/static/{release}")
    app.config.from_mapping(
        DATA_DIR=os.environ.get("TV2ARR_DATA", "/data"),
        ADMIN_PASSWORD=os.environ.get("TV2ARR_ADMIN_PASSWORD", ""),
        LAN_IP=os.environ.get("TV2ARR_LAN_IP", "127.0.0.1"),
        LAN_CIDR=os.environ.get("TV2ARR_LAN_CIDR", "127.0.0.0/8"),
        SESSION_COOKIE_SECURE=os.environ.get("TV2ARR_SECURE_COOKIE") == "1",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if config:
        app.config.update(config)
    password = app.config["ADMIN_PASSWORD"]
    if len(password) < 12:
        raise RuntimeError("Set TV2ARR_ADMIN_PASSWORD to at least 12 characters.")
    lan_ip = ip_address(app.config["LAN_IP"])
    network = ip_network(app.config["LAN_CIDR"], strict=True)
    if lan_ip not in network or (
        network != ip_network("127.0.0.0/8")
        and not any(network.version == block.version and network.subnet_of(block) for block in PRIVATE_NETWORKS)
    ):
        raise RuntimeError("Set TV2ARR_LAN_IP and TV2ARR_LAN_CIDR to the server IP and its private IPv4 subnet.")
    app.config["TRUSTED_HOSTS"] = [str(lan_ip), "127.0.0.1", "localhost"]

    # The database contains the Seerr credential and session signing material.
    db = Storage(Path(app.config["DATA_DIR"]) / "tv2arr.sqlite3")
    signing_key = db.config("signing_key")
    if signing_key is None:
        signing_key = secrets.token_hex(32)
        db.set_config("signing_key", signing_key)
    app.secret_key = hmac.digest(bytes.fromhex(signing_key), password.encode(), "sha256")

    service = service_factory(db)
    app.extensions["tv2arr"] = service
    app.jinja_env.filters["movie_title"] = movie_title
    app.jinja_env.filters["title_language"] = title_language

    @app.before_request
    def require_lan_and_session():
        try:
            client = ip_address(request.remote_addr or "")
        except ValueError:
            abort(403)
        if not client.is_loopback and client not in network:
            abort(403)
        if request.endpoint in {"static", "health", "app_version"}:
            return None
        if request.method == "POST":
            provided = request.form.get("csrf", "").encode()
            expected = session.get("csrf")
            if not expected or not hmac.compare_digest(expected.encode(), provided):
                abort(403)
        if request.endpoint != "login" and not session.get("authenticated"):
            return redirect(url_for("login"), code=303)
        return None

    @app.after_request
    def response_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https://tv3cdn.lv https://image.tmdb.org "
            f"{POSTER_ORIGIN} {REPLAY_POSTER_ORIGIN}; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def template_defaults():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {
            "csrf": session["csrf"], "version": release, "languages": LANGUAGES,
            "sources": service.sources,
            "source_names": ", ".join(source.NAME for source in service.sources.values()),
        }

    @app.template_filter("localtime")
    def localtime(stamp):
        return datetime.fromtimestamp(stamp, RIGA).strftime("%d %b %Y, %H:%M")

    def live_request():
        return request.headers.get("X-TV2ARR-Refresh") == "1"

    def render_live(name, **context):
        template = f"fragments/{name}.html" if live_request() else f"{name}.html"
        return render_template(template, **context)

    @app.get("/health")
    def health():
        db.rows("SELECT 1")
        return {"status": "ok"}

    @app.get("/version")
    def app_version():
        client_version = request.headers.get("X-TV2ARR-Version")
        if request.is_secure and client_version and client_version != release:
            return {"version": release}, 200, {"Clear-Site-Data": '"cache"'}
        return {"version": release}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                session.clear()
                session["authenticated"] = True
                return redirect(url_for("dashboard"), code=303)
            return render_template("login.html", title="Sign in", error="Incorrect password."), 401
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", title="Sign in")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"), code=303)

    def status_counts():
        return db.rows(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE match_state IN ('new','review','missing')) AS review,
                      count(*) FILTER (WHERE match_state='ready') AS eligible
               FROM listings WHERE present=1 AND ignored=0"""
        )[0]

    @app.get("/")
    def dashboard():
        counts = status_counts()
        sent = db.rows("SELECT count(*) FROM history WHERE outcome='Request sent to Seerr'")[0][0]
        events = db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 8")
        return render_live(
            "dashboard", title="Overview", counts=counts, sent=sent, events=events,
            settings=db.settings(), scanning=service.scan_lock.locked(),
        )

    @app.post("/scan")
    def scan():
        started = service.scan_async()
        if live_request():
            return {"started": started}, 202
        flash("Catalogue scan started." if started else "A scan is already running.")
        return redirect(url_for("dashboard"), code=303)

    @app.get("/movies")
    def movies():
        view = request.args.get("view", "all")
        filters = {
            "all": "", "review": "AND l.match_state IN ('new','review','missing')",
            "missing": "AND l.match_state='missing'",
            "ready": "AND l.match_state='ready'", "excluded": "AND l.match_state='excluded'",
            "ignored": "",
        }
        if view not in filters:
            abort(404)
        page = max(1, min(request.args.get("page", 1, type=int), 100_000))
        visibility = "l.ignored=1" if view == "ignored" else "l.present=1 AND l.ignored=0"
        listings = db.rows(
            f"""SELECT l.*, h.outcome FROM listings l
                LEFT JOIN history h ON h.tmdb_id=l.tmdb_id
                WHERE {visibility} {filters[view]}
                ORDER BY l.first_seen DESC, l.id LIMIT 25 OFFSET ?""",
            ((page - 1) * 24,),
        )
        return render_live(
            "movies", title={"review": "Review queue", "missing": "Missing from TMDb", "ignored": "Ignored movies"}.get(view, "Movies"),
            movies=listings[:24], view=view, page=page, more=len(listings) > 24,
        )

    def render_movie(listing, candidates=None, search="", error=None):
        if candidates is None:
            candidates = json.loads(listing["candidates"])
        history = db.rows("SELECT * FROM history WHERE tmdb_id=?", (listing["tmdb_id"],))
        return render_live(
            "movie", title=movie_title(listing), movie=listing, candidates=candidates,
            history=history[0] if history else None, mode=db.settings()["mode"],
            search=search, error=error,
        )

    def get_movie(listing_id):
        listing = db.movie(listing_id)
        if listing is None:
            abort(404)
        return listing

    @app.get("/movies/<int:listing_id>")
    def movie(listing_id):
        listing = get_movie(listing_id)
        if live_request():
            return render_movie(listing)
        query = request.args.get("q", "").strip()[:160]
        if query:
            try:
                with service.api() as api:
                    candidates, _ = find_candidates(api, listing, query)
                return render_movie(listing, candidates, query)
            except SeerrError as error:
                return render_movie(listing, search=query, error=str(error)), 502
        return render_movie(listing)

    @app.post("/movies/<int:listing_id>/match")
    def match_movie(listing_id):
        get_movie(listing_id)
        scan_after_match = False
        tmdb_id = request.form.get("tmdb_id", type=int)
        if not tmdb_id or not 0 < tmdb_id < 2**31:
            return render_movie(get_movie(listing_id), error="Enter a valid positive TMDB movie ID."), 400
        if not service.scan_lock.acquire(blocking=False):
            flash("Wait for the scan to finish before changing a match.", "error")
        else:
            try:
                with service.request_lock, service.api() as api:
                    state = db.save_match(listing_id, api.movie(tmdb_id))
                    statuses = {
                        "ready": "Movie match saved.",
                        "excluded": "Movie matched. Its original language is outside the allowed list.",
                        "review": "Movie matched. Seerr has no verified original language for it.",
                    }
                    flash(statuses[state])
                    scan_after_match = (
                        state == "ready" and not db.movie(listing_id)["ignored"]
                        and db.settings()["mode"] == "auto"
                    )
            except SeerrError as error:
                flash(str(error), "error")
            finally:
                service.scan_lock.release()
        if scan_after_match:
            service.scan_async()
        return redirect(url_for("movie", listing_id=listing_id), code=303)

    @app.post("/movies/<int:listing_id>/request")
    def request_movie(listing_id):
        get_movie(listing_id)
        try:
            flash(service.request(listing_id) + ".")
        except SeerrError as error:
            flash(str(error), "error")
        return redirect(url_for("movie", listing_id=listing_id), code=303)

    @app.post("/movies/<int:listing_id>/ignore")
    def ignore_movie(listing_id):
        value = request.form.get("ignored")
        if value not in {"0", "1"}:
            abort(400)
        ignored = value == "1"
        with service.request_lock:
            listing = get_movie(listing_id)
            changed = bool(listing["ignored"]) != ignored
            db.write("UPDATE listings SET ignored=? WHERE id=?", (ignored, listing_id))
            scan_after_restore = (
                changed and not ignored and listing["present"]
                and db.settings()["mode"] == "auto"
            )
        flash("Movie ignored." if ignored else "Movie restored.")
        if scan_after_restore:
            service.scan_async()
        return redirect(url_for("movie", listing_id=listing_id), code=303)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            return save_settings()
        return render_template("settings.html", title="Settings", settings=db.settings())

    def save_settings():
        with service.request_lock:
            current = db.settings()
            try:
                proposed = parse_settings(current)
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("settings"), code=303)
            test_connection = request.form.get("action") == "test"
            connection_changed = (proposed["url"], proposed["api_key"]) != (current["url"], current["api_key"])
            enabling_auto = current["mode"] != "auto" and proposed["mode"] == "auto"
            proposed["lookup_revision"] = current["lookup_revision"] + int(
                test_connection or connection_changed or enabling_auto
            )
            if test_connection:
                try:
                    with service.api(proposed) as api:
                        api.test()
                    proposed["connection"] = "Connected"
                    flash("Seerr connection verified and settings saved. Movie lookup will run shortly.")
                except SeerrError as error:
                    flash(str(error) + " Settings were not saved.", "error")
                    return redirect(url_for("settings"), code=303)
            else:
                flash("Settings saved.")
            db.set_config("settings", proposed)
        service.wake.set()
        return redirect(url_for("settings"), code=303)

    @app.get("/history")
    def history():
        tab = request.args.get("tab", "movies")
        if tab not in {"movies", "maintenance"}:
            abort(404)
        context = {"tab": tab}
        if tab == "movies":
            page = max(1, min(request.args.get("page", 1, type=int), 100_000))
            records = db.rows(
                """SELECT h.*, l.original_title, l.matched_original_title, l.candidates, l.language
                   FROM history h LEFT JOIN listings l ON l.id=(
                       SELECT id FROM listings WHERE tmdb_id=h.tmdb_id
                       ORDER BY (matched_original_title != '') DESC, id LIMIT 1
                   ) ORDER BY h.recorded_at DESC, h.tmdb_id LIMIT 51 OFFSET ?""",
                ((page - 1) * 50,),
            )
            context.update(records=records[:50], page=page, more=len(records) > 50)
        else:
            events = db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 30")
            maintenance = db.rows("SELECT * FROM events WHERE kind='Maintenance' ORDER BY id DESC LIMIT 1")
            # Keep the latest cleanup visible even after many newer scan events.
            if maintenance and all(event["id"] != maintenance[0]["id"] for event in events):
                events.append(maintenance[0])
            context["events"] = events
        return render_template("history.html", title="History", **context)

    if start_scheduler:
        service.maintenance_if_due(now_stamp())
        Thread(target=service.run_scheduler, name="scheduler", daemon=True).start()
    return app


def parse_settings(current):
    url = base_url(request.form.get("url", ""))
    key = request.form.get("api_key", "").strip()
    if len(key) > 512 or (key and (not key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in key))):
        raise ValueError("Enter a valid Seerr API key.")
    # Require explicit authorization before sending a stored credential to a different URL.
    if url != current["url"] and not key:
        raise ValueError("Enter the API key when changing the Seerr URL.")
    key = key or current["api_key"]
    if not key:
        raise ValueError("Enter the Seerr API key.")
    interval = request.form.get("interval", type=int)
    limit = request.form.get("limit", type=int)
    mode = request.form.get("mode")
    user_id_raw = request.form.get("user_id", "").strip()
    user_id = request.form.get("user_id", type=int)
    if not (interval and 1 <= interval <= 168 and limit and 1 <= limit <= 50):
        raise ValueError("Pick a scan interval of 1–168 hours and a request limit of 1–50.")
    if mode not in {"manual", "auto", "preview"}:
        raise ValueError("Select a request mode.")
    if user_id_raw and (not user_id or not 0 < user_id < 2**31):
        raise ValueError("Enter a valid Seerr user ID or leave it blank.")
    connection = current["connection"] if (url, key) == (current["url"], current["api_key"]) else "Not tested"
    return {
        "url": url, "api_key": key, "connection": connection,
        "interval": interval, "mode": mode, "limit": limit,
        "is4k": request.form.get("is4k") == "on", "user_id": user_id,
    }
