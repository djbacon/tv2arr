import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

NAME = "TV3 Play"
SLUG = "tv3"
URL = "https://play.tv3.lv/filmas/"
PRODUCT_META = "cXenseParse:zfv-playProduct"


def movie_link(href):
    parts = urlsplit(urljoin(URL, href))
    product = re.fullmatch(r"/filmas/[^/]+-(\d+)/?", parts.path)
    if product and parts.scheme == "https" and parts.netloc == "play.tv3.lv":
        return product[1], parts._replace(query="", fragment="").geturl()
    return None


def parse_listing(html):
    page = BeautifulSoup(html, "html.parser")
    heading = next(
        (h for h in page.select(".tv3-play-block h2") if h.get_text(" ", strip=True) == "Jaunākās filmas"),
        None,
    )
    if heading is None:
        raise ValueError("TV3's latest movies section could not be found.")

    movies = {}
    for link in heading.find_parent(class_="tv3-play-block").select("h3.tv3-post-title a[href]"):
        product = movie_link(link["href"])
        title = link.get_text(" ", strip=True)
        if product and title:
            source_id, url = product
            movies[source_id] = (source_id, title, url)

    if not movies:
        raise ValueError("No movie cards found in TV3's latest movies section.")
    return list(movies.values())


def parse_details(html, source_id):
    page = BeautifulSoup(html, "html.parser")

    def meta(suffix):
        tag = page.find("meta", attrs={"name": PRODUCT_META + suffix})
        return tag.get("content", "").strip() if tag else ""

    product_id = meta("Id")
    if product_id != source_id:
        raise ValueError("TV3 movie metadata could not be verified.")

    try:
        label = json.loads(meta("LabeloriginalTitle") or "{}")
        original = label.get("text") if isinstance(label, dict) else None
    except ValueError:
        original = None

    year = meta("Year")
    poster = meta("Image3x4")
    parts = urlsplit(poster)
    return (
        original.strip() if isinstance(original, str) else "",
        int(year) if re.fullmatch(r"[12]\d{3}", year) else None,
        poster if parts.scheme == "https" and parts.netloc == "tv3cdn.lv" else "",
    )


def listing(client):
    response = client.get(URL)
    response.raise_for_status()
    return parse_listing(response.text)


def details(client, movie):
    url = movie["url"]
    source_id = movie["source_id"]
    for _ in range(4):
        response = client.get(url)
        if not response.has_redirect_location:
            response.raise_for_status()
            return parse_details(response.text, source_id)
        redirect = movie_link(urljoin(url, response.headers["location"]))
        if not redirect or redirect[0] != source_id:
            raise ValueError("TV3 redirected the movie to an unexpected page.")
        url = redirect[1]
    raise ValueError("TV3 sent too many redirects for the movie.")
