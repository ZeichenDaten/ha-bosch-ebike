"""Convert Komoot v007 embedded coordinates into a small GPX document."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape


def _base_time(detail: dict[str, Any]) -> datetime | None:
    for key in ("date", "start_time", "changed_at"):
        value = detail.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed
    return None


def _coordinate_time(value: Any, base: datetime | None) -> datetime | None:
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds < 0:
        return None
    # Recorded tours normally carry epoch milliseconds. Some coordinate
    # arrays start at zero and use elapsed milliseconds instead.
    if milliseconds >= 100_000_000_000:
        return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)
    if base is not None:
        return base + timedelta(milliseconds=milliseconds)
    return None


def detail_to_gpx(detail: dict[str, Any]) -> str:
    """Build GPX without third-party dependencies or a second endpoint."""
    embedded = detail.get("_embedded")
    coordinates = (
        embedded.get("coordinates") if isinstance(embedded, dict) else None
    )
    items = coordinates.get("items") if isinstance(coordinates, dict) else None
    if not isinstance(items, list):
        raise ValueError("Komoot detail contains no coordinate array")

    base = _base_time(detail)
    title = escape(str(detail.get("name") or "Komoot-Tour"))
    points: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            lat = float(item["lat"])
            lon = float(item.get("lng", item.get("lon")))
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        lines = [f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">']
        try:
            altitude = float(item.get("alt"))
        except (TypeError, ValueError):
            altitude = None
        if altitude is not None:
            lines.append(f"        <ele>{altitude:.2f}</ele>")
        when = _coordinate_time(item.get("t"), base)
        if when is not None:
            lines.append(
                f"        <time>{when.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}</time>"
            )
        lines.append("      </trkpt>")
        points.append("\n".join(lines))

    if len(points) < 2:
        raise ValueError("Komoot detail has fewer than two valid coordinates")
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="Home Assistant Bosch eBike Komoot Sync"',
            ' xmlns="http://www.topografix.com/GPX/1/1">',
            "  <metadata>",
            f"    <name>{title}</name>",
            "  </metadata>",
            "  <trk>",
            f"    <name>{title}</name>",
            "    <trkseg>",
            *points,
            "    </trkseg>",
            "  </trk>",
            "</gpx>",
        ]
    )
