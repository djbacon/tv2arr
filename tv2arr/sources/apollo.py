import re
from urllib.parse import urlsplit

from . import Catalogue

NAME = "Apollo Kino"
SLUG = "apollo"
URL = "https://www.apollokino.lv/movies"
FEED_URL = "https://www.apollokino.lv/xml/Events/"
POSTER_ORIGIN = "https://mcswebsites.blob.core.windows.net"
PERFORMANCE_GENRES = {"balets", "koncerts", "opera", "ballet", "concert"}


def parse_catalogue(data):
    if not isinstance(data, list) or not data:
        raise ValueError("Apollo's movie feed is empty or invalid.")

    listings, metadata, seen = [], {}, set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Apollo's movie feed contains an invalid entry.")
        source_id = item.get("ID")
        event_type = item.get("EventType")
        genres = item.get("Genres")
        if (
            type(source_id) is not int or source_id < 1 or source_id in seen
            or not isinstance(event_type, str) or not event_type.strip()
            or not isinstance(genres, str)
        ):
            raise ValueError("Apollo's movie feed contains invalid movie metadata.")
        seen.add(source_id)
        if event_type.casefold() != "movie" or PERFORMANCE_GENRES.intersection(
            genre.strip().casefold() for genre in re.split(r"[,;]", genres)
        ):
            continue

        title = item.get("Title")
        url = item.get("EventURL")
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
            raise ValueError("Apollo's movie feed contains an invalid title or link.")
        parts = urlsplit(url)
        if (
            parts.scheme != "https" or parts.netloc != "www.apollokino.lv"
            or not re.fullmatch(rf"/event/{source_id}/[^/]+/?", parts.path)
        ):
            raise ValueError("Apollo's movie link could not be verified.")

        original = item.get("OriginalTitle") or ""
        if not isinstance(original, str):
            raise ValueError("Apollo's original movie title is invalid.")
        year = item.get("ProductionYear")
        if type(year) is not int or not 1000 <= year <= 2999:
            year = None
        images = item.get("Images") or {}
        if not isinstance(images, dict):
            raise ValueError("Apollo's movie images are invalid.")
        poster = images.get("EventLargeImagePortrait") or ""
        if not isinstance(poster, str):
            poster = ""
        image_parts = urlsplit(poster)
        if image_parts.scheme != "https" or image_parts.netloc != "mcswebsites.blob.core.windows.net":
            poster = ""

        source_id = str(source_id)
        listings.append((source_id, title.strip(), parts._replace(query="", fragment="").geturl()))
        metadata[source_id] = (original.strip(), year, poster)

    return Catalogue(listings, metadata)


def listing(client):
    response = client.get(FEED_URL, headers={"Accept": "application/json"})
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        raise ValueError("Apollo's movie feed did not return valid JSON.") from None
    return parse_catalogue(data)
