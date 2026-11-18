"""Adapters for public movie listings."""

from dataclasses import dataclass, field


@dataclass
class Catalogue:
    listings: list[tuple[str, str, str]]
    # Metadata supplied with a listing avoids a separate request per movie.
    details: dict[str, tuple[str, int | None, str]] = field(default_factory=dict)
