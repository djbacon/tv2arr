import json


def matched_original_title(movie):
    if movie["matched_original_title"]:
        return movie["matched_original_title"]
    # Older matches already have original titles in their saved search results.
    if movie["tmdb_id"]:
        for candidate in json.loads(movie["candidates"] or "[]"):
            if candidate["id"] == movie["tmdb_id"] and candidate.get("originalTitle"):
                return candidate["originalTitle"]
    return ""


def movie_title(movie):
    return matched_original_title(movie) or movie["original_title"] or movie["title"]


def title_language(movie):
    if matched_original_title(movie):
        return movie["language"] or ""
    return "" if movie["original_title"] else "lv"
