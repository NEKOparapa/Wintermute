"""Conservative pixel comparison of successive JPEGs from the same capture session."""

from io import BytesIO

type Region = tuple[int, int, int, int]


def badge_regions(value: object) -> tuple[Region, ...]:
    """Only tiny peripheral rectangles may suppress pixel changes (normalized to 0–1000)."""
    if not isinstance(value, (list, tuple)):
        return ()
    regions: list[Region] = []
    for rect in value:
        if (
            not isinstance(rect, (list, tuple))
            or len(rect) != 4
            or not all(type(v) is int for v in rect)
        ):
            continue
        x1, y1, x2, y2 = rect
        if (
            0 <= x1 < x2 <= 1000
            and 0 <= y1 < y2 <= 1000
            and x2 - x1 <= 90
            and y2 - y1 <= 55
            and (x2 <= 330 or (x1 >= 800 and (y2 <= 105 or y1 >= 920)))
        ):
            regions.append((x1, y1, x2, y2))
    return tuple(regions) if len(regions) <= 4 else ()


def changed_region(before: bytes, after: bytes, regions: tuple[Region, ...]) -> Region | None:
    """None means pixel-identical outside badges; uncertain comparisons cover the full frame."""
    # Pillow is an optional dependency on deployments that only receive text events.
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return (0, 0, 1000, 1000)
    try:
        with (
            Image.open(BytesIO(before), formats=["JPEG"]) as a,
            Image.open(BytesIO(after), formats=["JPEG"]) as b,
        ):
            width, height = a.size
            if (
                a.size != b.size
                or width * height > 12_000_000
                or a.mode != b.mode
                or a.mode not in {"RGB", "L"}
                or a.getexif().get(274, 1) != 1
                or b.getexif().get(274, 1) != 1
            ):
                return (0, 0, 1000, 1000)
            delta = ImageChops.difference(a, b)
            for x1, y1, x2, y2 in badge_regions(regions):
                box = (
                    (x1 * width + 999) // 1000,
                    (y1 * height + 999) // 1000,
                    x2 * width // 1000,
                    y2 * height // 1000,
                )
                if box[0] < box[2] and box[1] < box[3]:
                    delta.paste(0, box)
            # Compare at full resolution. A one-character edit can be the only useful change.
            bounds = delta.getbbox()
            if bounds is None:
                return None
            x1, y1, x2, y2 = bounds
            return (
                x1 * 1000 // width,
                y1 * 1000 // height,
                (x2 * 1000 + width - 1) // width,
                (y2 * 1000 + height - 1) // height,
            )
    except (OSError, ValueError, Image.DecompressionBombError):
        return (0, 0, 1000, 1000)
