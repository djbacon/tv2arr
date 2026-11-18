import json
import re
from urllib.parse import urlsplit

from . import Catalogue

NAME = "REplay.lv"
SLUG = "replay"
URL = "https://replay.lsm.lv/lv/skaties/raidijums/252/makslas-filmas"
API_URL = "https://replay.lsm.lv/graphql"
POSTER_ORIGIN = "https://ltv.lsm.lv"
SHOW_ID = 252
PAGE_SIZE = 24
MAX_PAGES = 100

LIST_QUERY = """
query($page: Int!) {
  media(show_ids: [252], per_page: 24, page: $page,
        order_by: [{field: PUBLISHED_AT, direction: DESC}]) {
    total
    data {
      id title slug external_source external_id playback_type type
      show { id }
    }
  }
}
"""
DETAIL_QUERY = """
query($source: String!, $id: Int!) {
  viewMedia(external_source: $source, external_id: $id, media_type: "video") {
    mediaItem {
      id title slug external_source external_id playback_type type
      thumbnail(size: LARGE)
      show { id }
    }
    metadata { name values { title } }
  }
}
"""

# Only remove known descriptive suffixes; preserve dots within the movie title.
TITLE_SUFFIX = re.compile(
    r"(?:\.\s+|(?<=[!?])\s+)"
    r"(?:(?:Latvijas|Lietuvas|Igaunijas)\s+)?"
    r"(?:mākslas filma|ģimenes filma|romantiska filma|"
    r"(?:vēsturiska|psiholoģiska|biogrāfiska|pieaugšanas|komiska)\s+drāma|"
    r"krimināldrāma|drāma|kriminālkomēdija|komēdija|trilleris|mūzikls)\.?$",
    re.IGNORECASE,
)


def api(client, query, variables):
    response = client.get(
        API_URL,
        params={
            "query": query, "variables": json.dumps(variables),
            "extensions": json.dumps({"language": "lv", "media": "video"}),
        },
        headers={
            "Accept": "application/json", "Set-Language": "lv",
            "X-Media-Type": "video", "Referer": URL,
        },
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        raise ValueError("Replay's catalogue API did not return valid JSON.") from None
    if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
        raise ValueError("Replay's catalogue API returned an invalid response or an error.")
    return body["data"]


def parse_item(item):
    if not isinstance(item, dict):
        raise ValueError("Replay's catalogue contains an invalid entry.")
    source_id, external_id = item.get("id"), item.get("external_id")
    title, slug = item.get("title"), item.get("slug")
    show = item.get("show")
    if (
        type(source_id) is not int or source_id < 1
        or type(external_id) is not int or external_id < 1
        or item.get("external_source") != "ltv"
        or not isinstance(show, dict) or show.get("id") != SHOW_ID
        or not isinstance(title, str) or not title.strip()
        or not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug)
        or not isinstance(item.get("type"), str) or not item["type"]
        or not isinstance(item.get("playback_type"), str) or not item["playback_type"]
    ):
        raise ValueError("Replay's movie identity or collection could not be verified.")
    if item["type"] != "full" or item["playback_type"] != "VIDEO":
        return None
    title = TITLE_SUFFIX.sub("", title.strip()).strip()
    if not title:
        raise ValueError("Replay's movie title is empty.")
    url = f"https://replay.lsm.lv/lv/skaties/ieraksts/ltv/{external_id}/{slug}"
    return str(source_id), title, url


def listing(client):
    movies, seen, total = [], set(), None
    for page in range(1, MAX_PAGES + 1):
        batch = api(client, LIST_QUERY, {"page": page}).get("media")
        if not isinstance(batch, dict):
            raise ValueError("Replay's movie catalogue is invalid.")
        count, items = batch.get("total"), batch.get("data")
        if (
            type(count) is not int or not 1 <= count <= MAX_PAGES * PAGE_SIZE
            or not isinstance(items, list) or (total is not None and count != total)
            or len(items) != min(PAGE_SIZE, count - len(seen))
        ):
            raise ValueError("Replay's movie catalogue is empty, incomplete or changed during pagination.")
        total = count
        for item in items:
            movie = parse_item(item)
            if item["id"] in seen:
                raise ValueError("Replay repeated a movie while paging through the catalogue.")
            seen.add(item["id"])
            if movie:
                movies.append(movie)
        if len(seen) == total:
            return Catalogue(movies)
    raise ValueError("Replay's movie catalogue exceeded the pagination limit.")


def details(client, movie):
    parts = urlsplit(movie["url"])
    route = re.fullmatch(r"/lv/skaties/ieraksts/ltv/(\d+)/[a-z0-9-]+", parts.path)
    if parts.scheme != "https" or parts.netloc != "replay.lsm.lv" or not route or parts.query or parts.fragment:
        raise ValueError("Replay's movie link could not be verified.")
    view = api(client, DETAIL_QUERY, {"source": "ltv", "id": int(route[1])}).get("viewMedia")
    if not isinstance(view, dict):
        raise ValueError("Replay's movie details could not be loaded.")
    item = view.get("mediaItem")
    identity = parse_item(item)
    if not identity or identity[0] != movie["source_id"] or identity[2] != movie["url"]:
        raise ValueError("Replay returned details for an unexpected movie.")
    metadata = view.get("metadata")
    if not isinstance(metadata, list):
        raise ValueError("Replay's movie metadata is invalid.")
    values = {"title-original": set(), "year": set()}
    for category in metadata:
        if not isinstance(category, dict) or not isinstance(category.get("name"), str):
            raise ValueError("Replay's movie metadata is invalid.")
        name = category["name"]
        if name not in values:
            continue
        if not isinstance(category.get("values"), list):
            raise ValueError("Replay's movie metadata values are invalid.")
        for value in category["values"]:
            if not isinstance(value, dict) or not isinstance(value.get("title"), str):
                raise ValueError("Replay's movie metadata values are invalid.")
            if value["title"].strip():
                values[name].add(value["title"].strip())
    original = next(iter(values["title-original"])) if len(values["title-original"]) == 1 else ""
    year = next(iter(values["year"])) if len(values["year"]) == 1 else ""
    poster = item.get("thumbnail")
    poster = poster if isinstance(poster, str) else ""
    image = urlsplit(poster)
    if image.scheme != "https" or image.netloc != "ltv.lsm.lv":
        poster = ""
    return original, int(year) if re.fullmatch(r"[12]\d{3}", year) else None, poster
