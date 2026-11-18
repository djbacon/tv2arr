import re

SCAN_SUMMARY = re.compile(r"([^:\n]+): (\d+) listed, (\d+) new\.(.*)", re.DOTALL)
REQUESTS_SENT = re.compile(r"(?<!\S)(\d+) requests? sent\.(?=\s|$)")


def activity_columns(row):
    event = dict(row)
    # Older releases saved scan counts only in the human-readable message.
    if event["kind"] == "Scan" and all(
        event[name] is None for name in ("listed_count", "new_count", "requested_count")
    ):
        summary = SCAN_SUMMARY.fullmatch(event["message"])
        if summary:
            source, listed, new, notes = summary.groups()
            requested = REQUESTS_SENT.search(notes)
            event.update(
                source=event["source"] or source,
                listed_count=int(listed), new_count=int(new),
                requested_count=int(requested[1]) if requested else 0,
                message=" ".join(REQUESTS_SENT.sub("", notes).split()),
            )
    return event
