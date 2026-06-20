"""Optional hand-off of a freshly downloaded report into ``fincheck``.

Keeps the dependency soft: if ``fincheck`` is not importable, or the file is not
a PDF, we skip silently and report it rather than failing the download.
"""

from __future__ import annotations

from pathlib import Path


def run_fincheck(path: Path) -> str | None:
    """Run fincheck on ``path`` if it is a PDF and fincheck is available.

    Returns a one-line human summary, or ``None`` if the check was skipped.
    """
    if path.suffix.lower() != ".pdf":
        return None
    try:
        from fincheck import analyze
    except ModuleNotFoundError:
        return None

    out = path.with_suffix(".highlighted.pdf")
    result = analyze(str(path), output_pdf=str(out))
    if result.consistent:
        return f"fincheck: all {result.totals_checked} totals foot ✓"
    return (
        f"fincheck: {len(result.issues)} inconsistency(ies) found "
        f"→ {out.name}"
    )
