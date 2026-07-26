"""Render a two-PDF comparison as console text or JSON.

The console layout puts the layered verdicts first — each one an exact
equality test — and the per-page differences after, so the question "are these
the same document?" is answered before any detail.
"""

import json

from .compare import ComparisonResult, PageComparison, SpanChange
from .numbers import format_number

_SYMBOL = {
    "added": "+",
    "removed": "-",
    "edited": "~",
    "moved": "→",
    "restyled": "≈",
    "moved+restyled": "≉",
}


def _short(digest: str) -> str:
    return digest[:12]


def _span_dict(span) -> dict | None:
    if span is None:
        return None
    return {
        "text": span.text,
        "font": span.font,
        "size": span.size,
        "color": span.color,
        "bbox": [span.x0, span.y0, span.x1, span.y1],
        "value": span.value,
    }


def _change_dict(change: SpanChange) -> dict:
    entry = {
        "kind": change.kind,
        "before": _span_dict(change.before),
        "after": _span_dict(change.after),
        "context_label": change.context,
    }
    if change.is_numeric:
        entry["value_change"] = {
            "before": change.before.value,
            "after": change.after.value,
            "delta": change.delta,
        }
    return entry


def _page_dict(page: PageComparison) -> dict:
    entry = {
        "page_a": None if page.page_a is None else page.page_a + 1,
        "page_b": None if page.page_b is None else page.page_b + 1,
        "identical": page.identical,
        "geometry_equal": page.geometry_equal,
        "text_equal": page.text_equal,
        "spans_equal": page.spans_equal,
        "graphics_equal": page.graphics_equal,
        "text_changes": [_change_dict(c) for c in page.span_changes],
        "graphics": {
            "paths_added": page.paths_added,
            "paths_removed": page.paths_removed,
            "images_added": page.images_added,
            "images_removed": page.images_removed,
        },
    }
    if page.geometry_note:
        entry["geometry_note"] = page.geometry_note
    if page.pixels is not None:
        entry["pixels"] = {
            "dpi": page.pixels.dpi,
            "comparable": page.pixels.comparable,
            "identical": page.pixels.identical,
            "changed_pixels": page.pixels.changed_pixels,
            "total_pixels": page.pixels.total_pixels,
            "changed_regions": [list(r.bbox) for r in page.pixels.regions],
            "note": page.pixels.note,
        }
    return entry


def comparison_to_dict(result: ComparisonResult) -> dict:
    return {
        "a": {
            "path": result.pdf_a,
            "sha256": result.sha256_a,
            "pages": result.page_count_a,
        },
        "b": {
            "path": result.pdf_b,
            "sha256": result.sha256_b,
            "pages": result.page_count_b,
        },
        "settings": {
            "position_tolerance": result.position_tolerance,
            "render_dpi": result.dpi,
            "page_alignment": "content" if result.aligned else "positional",
        },
        "verdict": {
            "identical": result.identical,
            "bytes_identical": result.bytes_identical,
            "geometry_identical": result.geometry_identical,
            "text_identical": result.text_identical,
            "spans_identical": result.spans_identical,
            "graphics_identical": result.graphics_identical,
            "content_identical": result.content_identical,
            "visually_identical": result.visually_identical,
        },
        "changed_page_count": len(result.changed_pages),
        "pages": [_page_dict(p) for p in result.pages if not p.identical],
    }


def comparison_to_json(result: ComparisonResult) -> str:
    return json.dumps(comparison_to_dict(result), indent=2)


def _verdict_line(label: str, state, detail: str = "") -> str:
    if state is None:
        word = "not checked"
    elif state:
        word = "same"
    else:
        word = "DIFFERS"
    dots = "." * max(3, 36 - len(label))
    return f"  {label} {dots} {word:<12}{detail}".rstrip()


def _describe_change(change: SpanChange) -> list[str]:
    symbol = _SYMBOL.get(change.kind, "?")
    context = f"   [{change.context[:40]}]" if change.context else ""

    if change.kind == "edited":
        head = (
            f"    {symbol} {change.kind:<14} "
            f"{change.before.text.strip()!r} -> {change.after.text.strip()!r}"
            f"   at {change.after.where()}{context}"
        )
        if change.is_numeric:
            return [
                head,
                f"      {'':16}value {format_number(change.before.value)} -> "
                f"{format_number(change.after.value)} "
                f"(change of {format_number(change.delta)})",
            ]
        return [head]

    if change.kind in ("moved", "restyled", "moved+restyled"):
        return [
            f"    {symbol} {change.kind:<14} {change.before.text.strip()!r}"
            f"   {change.before.where()} -> {change.after.where()}{context}"
        ]

    span = change.after or change.before
    return [
        f"    {symbol} {change.kind:<14} {span.text.strip()!r}"
        f"   at {span.where()}{context}"
    ]


def comparison_to_console(result: ComparisonResult, max_changes: int = 40) -> str:
    lines = [
        "Exact PDF comparison — every check below is an equality test on what",
        "the files actually contain. Nothing is inferred.",
        "",
        f"  A  {result.pdf_a}",
        f"     {result.page_count_a} page(s)   sha256 {_short(result.sha256_a)}…",
        f"  B  {result.pdf_b}",
        f"     {result.page_count_b} page(s)   sha256 {_short(result.sha256_b)}…",
        "",
    ]

    changed = result.changed_pages
    text_pages = sum(1 for p in result.pages if not p.text_equal)
    span_pages = sum(1 for p in result.pages if not p.spans_equal)
    graphics_pages = sum(1 for p in result.pages if not p.graphics_equal)

    lines.append(_verdict_line("byte-for-byte identical", result.bytes_identical))
    lines.append(
        _verdict_line(
            "page count and geometry",
            result.geometry_identical,
            ""
            if result.page_counts_equal
            else f"({result.page_count_a} vs {result.page_count_b} pages)",
        )
    )
    lines.append(
        _verdict_line(
            "text, every character",
            result.text_identical,
            f"({text_pages} page(s))" if text_pages else "",
        )
    )
    lines.append(
        _verdict_line(
            "text spans, incl. font+position",
            result.spans_identical,
            f"({span_pages} page(s))" if span_pages else "",
        )
    )
    lines.append(
        _verdict_line(
            "vector graphics and images",
            result.graphics_identical,
            f"({graphics_pages} page(s))" if graphics_pages else "",
        )
    )

    if result.pixels_compared:
        differing = [
            p for p in result.pages if p.pixels is not None and not p.pixels.identical
        ]
        # A page whose two renderings come out different sizes was never
        # measured. Folding those into the percentage produced "0.000% of
        # pixels", which reads as "differs by almost nothing" — the opposite of
        # "could not be compared at all".
        measured = [p for p in differing if p.pixels.comparable]
        unmeasured = [p for p in differing if not p.pixels.comparable]
        total_px = sum(
            p.pixels.total_pixels
            for p in result.pages
            if p.pixels is not None and p.pixels.comparable
        )
        parts = []
        if measured:
            changed_px = sum(p.pixels.changed_pixels for p in measured)
            share = 100.0 * changed_px / total_px if total_px else 0.0
            parts.append(f"{len(measured)} page(s), {share:.3f}% of pixels")
        if unmeasured:
            parts.append(
                f"{len(unmeasured)} page(s) not comparable, different rendered size"
            )
        lines.append(
            _verdict_line(
                f"rendered pixels at {result.dpi} dpi",
                result.visually_identical,
                f"({'; '.join(parts)})" if parts else "",
            )
        )
    else:
        lines.append(_verdict_line("rendered pixels", None, "(rendering skipped)"))

    lines.append("")
    if result.bytes_identical:
        lines.append("  Verdict: the two files are the same file, byte for byte.")
    elif result.identical:
        lines.append(
            "  Verdict: different files, but identical documents — every drawing"
        )
        lines.append(
            "  operation matches"
            + (
                " and so does every rendered pixel."
                if result.visually_identical
                else ", so they must render alike (pixels not checked)."
            )
        )
        lines.append("  The bytes differ only in metadata or encoding.")
    else:
        lines.append(
            f"  Verdict: the documents are NOT identical "
            f"({len(changed)} of {max(result.page_count_a, result.page_count_b)} "
            f"page(s) differ)."
        )

    numeric = result.numeric_changes
    if numeric:
        lines += ["", f"Figures that changed ({len(numeric)}):", ""]
        for page, change in numeric[:max_changes]:
            where = "" if page.page_b is None else f"Page {page.page_b + 1}"
            label = f"  ·  {change.context}" if change.context else ""
            lines.append(
                f"  {where}{label}"
                f"\n      {format_number(change.before.value)} -> "
                f"{format_number(change.after.value)}"
                f"   (change of {format_number(change.delta)})"
            )
        if len(numeric) > max_changes:
            lines.append(f"  … and {len(numeric) - max_changes} more.")
        lines += [
            "",
            "  The values and their positions above are exact. The label after the",
            "  page number is the nearest text on the same baseline — a reading aid,",
            "  not part of the comparison.",
        ]

    if not changed:
        return "\n".join(lines)

    lines += ["", "Differences by page:"]
    shown = 0
    for page in changed:
        header = (
            f"  Page {page.page_a + 1}"
            if page.page_b is None
            else f"  Page {page.page_b + 1}"
        )
        if page.page_a is not None and page.page_b is not None and result.aligned:
            header = f"  Page {page.page_a + 1} (A) / {page.page_b + 1} (B)"
        lines.append("")
        lines.append(header)

        if page.only_in_one:
            side = "A only" if page.page_b is None else "B only"
            lines.append(f"    page exists in one document only ({side})")
            continue
        if not page.geometry_equal:
            lines.append(f"    page geometry differs: {page.geometry_note}")

        for change in page.span_changes:
            if shown >= max_changes:
                break
            lines += _describe_change(change)
            shown += 1

        if not page.graphics_equal:
            lines.append(
                f"    vector paths +{page.paths_added}/-{page.paths_removed}, "
                f"images +{page.images_added}/-{page.images_removed}"
            )
        if page.pixels is not None and not page.pixels.identical:
            if not page.pixels.comparable:
                lines.append(f"    pixels: {page.pixels.note}")
            else:
                lines.append(
                    f"    pixels: {page.pixels.changed_pixels:,} of "
                    f"{page.pixels.total_pixels:,} differ "
                    f"({page.pixels.changed_fraction * 100:.3f}%) in "
                    f"{len(page.pixels.regions)} region(s)"
                )

    total_changes = sum(len(p.span_changes) for p in changed)
    if total_changes > shown:
        lines.append("")
        lines.append(f"  … {total_changes - shown} further text change(s) not shown.")

    return "\n".join(lines)
