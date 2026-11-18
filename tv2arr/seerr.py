import re
import unicodedata
from urllib.parse import quote, urlencode, urlsplit

import httpx

LANGUAGES = {"lv": "Latvian", "en": "English"}


class SeerrError(Exception):
    def __init__(self, message, uncertain=False, status=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status


def base_url(value):
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("Enter a valid Seerr URL.") from None
    if (
        parts.scheme not in {"http", "https"} or not parts.hostname
        or parts.username is not None or parts.password is not None
        or parts.query or parts.fragment or port == 0
        or re.search(r"[\s\\?#]", value)
    ):
        raise ValueError("Enter a Seerr URL such as http://192.168.1.20:5055.")
    return value.removesuffix("/api/v1")


class Seerr:
    def __init__(self, url, api_key, transport=None):
        if not url or not api_key:
            raise SeerrError("Connect Seerr in Settings first.")
        self.client = httpx.Client(
            base_url=url + "/api/v1/",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=10,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()

    def call(self, method, path, *, query=None, **kwargs):
        operation = f"{method} /api/v1/{path}"
        # Seerr rejects the raw '+' that HTTPX uses for spaces in params.
        url = f"{path}?{urlencode(query, quote_via=quote)}" if query else path
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise SeerrError(f"Could not reach Seerr for {operation}.", uncertain=method == "POST") from None

        status = response.status_code
        if status >= 300:
            detail = self.error_detail(response) if 400 <= status < 500 else ""
            raise SeerrError(
                f"Seerr returned HTTP {status} for {operation}." + (f" {detail}" if detail else ""),
                uncertain=method == "POST" and (
                    status in {408, 409, 425, 429} or status >= 500
                ),
                status=status,
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise SeerrError(f"Seerr returned an invalid response for {operation}.", uncertain=method == "POST")
        return data

    def error_detail(self, response):
        try:
            body = response.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        detail = body.get("message") or body.get("error")
        if not isinstance(detail, str):
            return ""
        detail = detail.replace(self.client.headers["X-Api-Key"], "[redacted]")
        return " ".join(detail.split())[:220]

    def test(self):
        result = self.call("GET", "auth/me")
        if not result.get("id"):
            raise SeerrError("Seerr authentication could not be verified.")
        try:
            movies = self.search("The Matrix")
        except SeerrError as error:
            raise SeerrError(f"Seerr authenticated, but movie search failed. {error}") from error
        if not movies:
            raise SeerrError("Seerr authenticated, but movie search returned no results. Check Seerr's connection to TMDB.")

    def search(self, title, language="en"):
        params = {"query": title, "page": 1}
        try:
            data = self.call("GET", "search", query=params | {"language": language})
        except SeerrError as error:
            if error.status != 400 or "language" not in str(error).casefold():
                raise
            data = self.call("GET", "search", query=params)
        results = data.get("results")
        if not isinstance(results, list):
            raise SeerrError("Seerr search returned an invalid response.")
        return [
            {
                "id": item["id"],
                "title": item.get("title") or item.get("originalTitle") or "Untitled",
                "originalTitle": item.get("originalTitle") or "",
                "year": (item.get("releaseDate") or "")[:4],
                "language": item.get("originalLanguage") or "",
            }
            for item in results
            if isinstance(item, dict) and item.get("mediaType") == "movie"
            and type(item.get("id")) is int and item["id"] > 0
        ]

    def movie(self, tmdb_id):
        result = self.call("GET", f"movie/{tmdb_id}")
        if result.get("id") != tmdb_id or not result.get("title"):
            raise SeerrError("Seerr could not verify the selected movie.")
        return result

    def submit(self, tmdb_id, settings):
        payload = {"mediaType": "movie", "mediaId": tmdb_id, "is4k": settings["is4k"]}
        if settings["user_id"]:
            payload["userId"] = settings["user_id"]
        result = self.call("POST", "request", json=payload)
        if type(result.get("id")) is not int or result["id"] < 1:
            raise SeerrError("Seerr did not return a request ID.", uncertain=True)
        return result["id"]


def poster_url(movie):
    path = movie.get("posterPath")
    if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)", path):
        return "https://image.tmdb.org/t/p/w500" + path
    return ""


def normalize(title):
    return "".join(c for c in unicodedata.normalize("NFKC", title).casefold() if c.isalnum())


def find_candidates(api, listing, title_override=""):
    original = listing["original_title"]
    localized = listing["title"]
    if title_override:
        return api.search(title_override)[:12], None

    by_id = {}
    exact = set()
    listing_names = {normalize(title) for title in (original, localized) if title}
    listing_names.discard("")
    queries = [(original, "en"), (localized, "lv")] if original else [(localized, "lv")]
    for title, locale in dict.fromkeys(queries):
        for movie in api.search(title, locale):
            by_id.setdefault(movie["id"], movie)
            names = {normalize(movie["title"]), normalize(movie["originalTitle"])}
            if listing_names & names and str(listing["year"]) == movie["year"]:
                exact.add(movie["id"])
    candidates = sorted(by_id.values(), key=lambda movie: movie["id"] not in exact)
    return candidates[:12], next(iter(exact)) if len(exact) == 1 else None


def existing_request(movie):
    media = movie.get("mediaInfo") or {}
    if media.get("status") == 6:
        return "Blocked in Seerr", None
    # A request in either quality satisfies the one-request-per-movie policy.
    for req in media.get("requests") or []:
        if req.get("status") in {1, 2, 5}:
            return "Already requested in Seerr", req.get("id")
    if any(media.get(key) in {2, 3, 4, 5} for key in ("status", "status4k")):
        return "Already requested or available in Seerr", None
    return None, None
