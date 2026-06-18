#!/usr/bin/env python3
"""Build tiny text PDFs by hand (standard library only) for testing pdfcompare.

This deliberately uses no third-party libraries so the whole test suite runs in
an air-gapped environment.
"""

import zlib
from pathlib import Path


def _make_pdf(lines, compress=False):
    """Build a minimal one-page PDF showing ``lines`` of text."""
    content_parts = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
    for i, line in enumerate(lines):
        esc = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        if i == 0:
            content_parts.append(f"({esc}) Tj")
        else:
            content_parts.append("T*")
            content_parts.append(f"({esc}) Tj")
    content_parts.append("ET")
    content = ("\n".join(content_parts)).encode("latin-1")

    if compress:
        stream_data = zlib.compress(content)
        stream_dict = f"<< /Length {len(stream_data)} /Filter /FlateDecode >>"
    else:
        stream_data = content
        stream_dict = f"<< /Length {len(stream_data)} >>"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        stream_dict.encode("latin-1") + b"\nstream\n" + stream_data + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.5\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def main():
    here = Path(__file__).parent
    base = [
        "ACME CORP - Balance Sheet",
        "Cash and cash equivalents   8,750",
        "Trade receivables           3,400",
        "Inventories                 6,800",
        "Total current assets       18,950",
    ]
    changed = [
        "ACME CORP - Balance Sheet",
        "Cash and cash equivalents   8,750",
        "Trade receivables           3,200",
        "Inventories                 6,800",
        "Total current assets       18,750",
    ]
    (here / "sample_a.pdf").write_bytes(_make_pdf(base))
    (here / "sample_b.pdf").write_bytes(_make_pdf(changed, compress=True))
    (here / "sample_a_copy.pdf").write_bytes(_make_pdf(base))
    print("wrote sample_a.pdf, sample_b.pdf, sample_a_copy.pdf")


if __name__ == "__main__":
    main()
