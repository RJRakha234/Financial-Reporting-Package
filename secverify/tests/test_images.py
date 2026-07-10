"""Tests for the image checks (inventory, OCR of embedded images, outline)."""

import base64
import io
import os
import sys

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from test_annotate import make_corpus  # noqa: E402

from secverify.annotate import Annotator  # noqa: E402
from secverify.images import _ocr_engine  # noqa: E402


def run(pages, html, **kw):
    return Annotator(
        make_corpus(pages), level="sigma", pdf_paths=[], **kw
    ).run(html, "ref.pdf", "doc.html")


def test_image_inventory_reported():
    pages = ["Revenue 1000"]
    html = (
        "<html><body><p>Revenue <img src='rupee-symbol.gif'/> 1000</p>"
        "<p><img src='rupee-symbol.gif' alt='rupee symbol'/> 2000</p></body></html>"
    )
    r = run(pages, html)
    inv = [i for i in r.issues if i.kind == "image"]
    assert inv and "2 image(s)" in inv[0].excerpt
    assert "rupee-symbol.gif" in inv[0].excerpt
    # no images -> no image issue
    r2 = run(pages, "<html><body><p>Revenue 1000</p></body></html>")
    assert not [i for i in r2.issues if i.kind == "image"]


def test_images_outlined_in_review_zones():
    pages = ["Revenue 1000"]
    html = "<html><body><p><img src='rupee-symbol.gif'/> 1000</p></body></html>"
    r = run(pages, html, review_zones=True)
    soup = BeautifulSoup(r.html_out, "html.parser")
    img = soup.find("img")
    assert "secv-img-review" in img.get("class", [])
    # off by default
    r2 = run(pages, html)
    img2 = BeautifulSoup(r2.html_out, "html.parser").find("img")
    assert "secv-img-review" not in (img2.get("class") or [])


def _png_data_uri(text: str) -> str:
    from PIL import Image, ImageDraw

    img = Image.new("L", (240, 60), 255)
    d = ImageDraw.Draw(img)
    d.text((10, 20), text, fill=0)
    img = img.resize((960, 240))  # big and crisp for OCR
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.mark.skipif(_ocr_engine() is None, reason="no OCR engine installed")
def test_embedded_image_figure_not_in_pdf_flagged():
    uri = _png_data_uri("48,375")
    html = f"<html><body><p>Chart:</p><img src='{uri}'/></body></html>"
    # PDF does NOT contain 48,375 -> OCR finding
    r = run(["Revenue 1000"], html)
    ocr_issues = [i for i in r.issues if i.kind == "image-ocr"]
    assert ocr_issues, "figure inside image absent from PDF must be flagged"
    # PDF DOES contain it -> no OCR mismatch (inventory issue remains)
    r2 = run(["Revenue 48,375 and 1000"], html)
    assert not [i for i in r2.issues if i.kind == "image-ocr"]
