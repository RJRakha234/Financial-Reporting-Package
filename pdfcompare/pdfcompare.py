#!/usr/bin/env python3
"""pdfcompare - compare the *text content* of two PDFs using only the Python
standard library.

Designed for locked-down / air-gapped corporate environments where you cannot
``pip install`` anything: this module imports nothing outside the standard
library (``zlib``, ``base64``, ``difflib``, ``argparse`` ...). Copy this single
file onto the machine and run it.

It works by:
  1. Scanning the PDF for indirect objects (without trusting the cross-reference
     table, which is often broken in edited files).
  2. Decompressing content streams (FlateDecode/zlib, ASCIIHex, ASCII85, LZW).
  3. Walking the page tree and pulling the text-showing operators (Tj, TJ, '
     and ") out of each page's content stream, using each font's ToUnicode CMap
     when present so modern subset/Identity-H fonts decode correctly.
  4. Diffing the resulting text of the two files with ``difflib``.

Limitations (honest ones):
  * Scanned / image-only PDFs contain no text -- they need OCR, which the
    standard library cannot do. The tool detects this and tells you.
  * Encrypted PDFs are not decrypted.
  * Some embedded subset fonts ship no ToUnicode map; their glyphs cannot be
    turned back into characters without the font program. The tool reports how
    much of each page it was able to decode so you know when to be cautious.

Usage:
    python pdfcompare.py old.pdf new.pdf
    python pdfcompare.py old.pdf new.pdf --ignore-whitespace --context 5
    python pdfcompare.py old.pdf new.pdf --by-page
    python pdfcompare.py file.pdf --dump          # print extracted text only
    python pdfcompare.py old.pdf new.pdf --json

Exit status: 0 if the extracted text is identical, 1 if it differs, 2 on error.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import difflib
import hashlib
import json
import re
import sys
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# PDF object model
# --------------------------------------------------------------------------- #


class Ref:
    """An indirect reference ``N G R``."""

    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int):
        self.num = num
        self.gen = gen

    def __repr__(self):  # pragma: no cover - debug aid
        return f"Ref({self.num} {self.gen})"

    def key(self):
        return (self.num, self.gen)


class Name(str):
    """A PDF name like ``/Type``; subclassing str lets us tell it apart from a
    string object while still comparing naturally."""


class Stream:
    """A stream object: its dictionary plus the *decoded* bytes."""

    __slots__ = ("dict", "data")

    def __init__(self, d: dict, data: bytes):
        self.dict = d
        self.data = data


# --------------------------------------------------------------------------- #
# Tokenizer (shared by the object parser and the content-stream reader)
# --------------------------------------------------------------------------- #

_WHITESPACE = b"\x00\t\n\f\r "
_DELIMITERS = b"()<>[]{}/%"
_REGULAR = lambda b: b not in _WHITESPACE and b not in _DELIMITERS


class Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value, pos: int):
        self.kind = kind  # 'num' 'str' 'name' 'kw' '[' ']' '<<' '>>'
        self.value = value
        self.pos = pos

    def __repr__(self):  # pragma: no cover
        return f"<{self.kind}:{self.value!r}>"


def tokenize(data: bytes, start: int = 0, end: int | None = None):
    """Yield :class:`Token` objects from ``data[start:end]``."""
    if end is None:
        end = len(data)
    i = start
    n = end
    while i < n:
        c = data[i]
        # whitespace
        if c in _WHITESPACE:
            i += 1
            continue
        # comment
        if c == 0x25:  # %
            j = i + 1
            while j < n and data[j] not in b"\r\n":
                j += 1
            i = j
            continue
        # literal string
        if c == 0x28:  # (
            s, i = _read_literal_string(data, i + 1, n)
            yield Token("str", s, i)
            continue
        # hex string or dict open
        if c == 0x3C:  # <
            if i + 1 < n and data[i + 1] == 0x3C:
                yield Token("<<", None, i)
                i += 2
                continue
            s, i = _read_hex_string(data, i + 1, n)
            yield Token("str", s, i)
            continue
        if c == 0x3E:  # >
            if i + 1 < n and data[i + 1] == 0x3E:
                yield Token(">>", None, i)
                i += 2
                continue
            i += 1  # stray '>', skip
            continue
        if c == 0x5B:  # [
            yield Token("[", None, i)
            i += 1
            continue
        if c == 0x5D:  # ]
            yield Token("]", None, i)
            i += 1
            continue
        if c in b"{}":  # postscript-calc braces; treat as delimiters we ignore
            i += 1
            continue
        # name
        if c == 0x2F:  # /
            name, i = _read_name(data, i + 1, n)
            yield Token("name", name, i)
            continue
        # number
        if c in b"+-.0123456789":
            tok, i = _read_number_or_kw(data, i, n)
            yield tok
            continue
        # bare keyword / operator
        j = i
        while j < n and _REGULAR(data[j]):
            j += 1
        if j == i:  # no progress; skip the byte to avoid an infinite loop
            i += 1
            continue
        yield Token("kw", data[i:j].decode("latin-1"), j)
        i = j


def _read_literal_string(data, i, n):
    out = bytearray()
    depth = 1
    while i < n:
        c = data[i]
        if c == 0x5C:  # backslash escape
            i += 1
            if i >= n:
                break
            e = data[i]
            if e == 0x6E:  # n
                out.append(0x0A)
            elif e == 0x72:  # r
                out.append(0x0D)
            elif e == 0x74:  # t
                out.append(0x09)
            elif e == 0x62:  # b
                out.append(0x08)
            elif e == 0x66:  # f
                out.append(0x0C)
            elif e in b"()\\":
                out.append(e)
            elif e in b"\r\n":  # line continuation
                if e == 0x0D and i + 1 < n and data[i + 1] == 0x0A:
                    i += 1
            elif 0x30 <= e <= 0x37:  # up to 3 octal digits
                oct_digits = bytes([e])
                for _ in range(2):
                    if i + 1 < n and 0x30 <= data[i + 1] <= 0x37:
                        i += 1
                        oct_digits += bytes([data[i]])
                    else:
                        break
                out.append(int(oct_digits, 8) & 0xFF)
            else:
                out.append(e)
            i += 1
            continue
        if c == 0x28:  # (
            depth += 1
            out.append(c)
        elif c == 0x29:  # )
            depth -= 1
            if depth == 0:
                i += 1
                break
            out.append(c)
        else:
            out.append(c)
        i += 1
    return bytes(out), i


def _read_hex_string(data, i, n):
    hexchars = bytearray()
    while i < n and data[i] != 0x3E:  # >
        c = data[i]
        if c not in _WHITESPACE:
            hexchars.append(c)
        i += 1
    if i < n:
        i += 1  # consume '>'
    if len(hexchars) % 2:
        hexchars.append(0x30)  # pad with '0'
    try:
        return binascii.unhexlify(hexchars), i
    except binascii.Error:
        return b"", i


def _read_name(data, i, n):
    out = bytearray()
    while i < n and _REGULAR(data[i]):
        c = data[i]
        if c == 0x23 and i + 2 < n:  # '#xx' hex escape in a name
            try:
                out.append(int(data[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return Name(out.decode("latin-1")), i


def _read_number_or_kw(data, i, n):
    j = i
    while j < n and _REGULAR(data[j]):
        j += 1
    raw = data[i:j]
    text = raw.decode("latin-1")
    try:
        if b"." in raw or b"e" in raw or b"E" in raw:
            return Token("num", float(text), j), j
        return Token("num", int(text), j), j
    except ValueError:
        # Things like "1.2.3" or stray symbols: best-effort float, else keyword
        try:
            return Token("num", float(text.replace("..", ".")), j), j
        except ValueError:
            return Token("kw", text, j), j


# --------------------------------------------------------------------------- #
# Value parser (turns a token list into Python values, detecting "N G R" refs)
# --------------------------------------------------------------------------- #


class ValueParser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.i = 0

    def at_end(self):
        return self.i >= len(self.toks)

    def parse(self):
        tok = self.toks[self.i]
        if tok.kind == "<<":
            return self._parse_dict()
        if tok.kind == "[":
            return self._parse_array()
        if tok.kind == "num":
            return self._parse_number_or_ref()
        if tok.kind == "name":
            self.i += 1
            return tok.value
        if tok.kind == "str":
            self.i += 1
            return tok.value
        if tok.kind == "kw":
            self.i += 1
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            if tok.value == "null":
                return None
            return tok.value  # an operator / stray keyword
        # Closing tokens or anything unexpected: consume and report None.
        self.i += 1
        return None

    def _parse_number_or_ref(self):
        # Could be "N G R" reference. Look ahead.
        t0 = self.toks[self.i]
        if (
            isinstance(t0.value, int)
            and self.i + 2 < len(self.toks)
            and self.toks[self.i + 1].kind == "num"
            and isinstance(self.toks[self.i + 1].value, int)
            and self.toks[self.i + 2].kind == "kw"
            and self.toks[self.i + 2].value == "R"
        ):
            ref = Ref(t0.value, self.toks[self.i + 1].value)
            self.i += 3
            return ref
        self.i += 1
        return t0.value

    def _parse_dict(self):
        self.i += 1  # consume '<<'
        d = {}
        while not self.at_end():
            tok = self.toks[self.i]
            if tok.kind == ">>":
                self.i += 1
                break
            if tok.kind != "name":
                # malformed; skip token
                self.i += 1
                continue
            key = tok.value
            self.i += 1
            if self.at_end():
                break
            d[key] = self.parse()
        return d

    def _parse_array(self):
        self.i += 1  # consume '['
        arr = []
        while not self.at_end():
            tok = self.toks[self.i]
            if tok.kind == "]":
                self.i += 1
                break
            arr.append(self.parse())
        return arr


def parse_value(data: bytes, start: int = 0, end: int | None = None):
    toks = list(tokenize(data, start, end))
    if not toks:
        return None
    return ValueParser(toks).parse()


# --------------------------------------------------------------------------- #
# Stream filters (decompression)
# --------------------------------------------------------------------------- #


def _apply_predictor(data: bytes, parms: dict) -> bytes:
    predictor = parms.get("Predictor", 1)
    if isinstance(predictor, Ref) or predictor in (None, 1):
        return data
    columns = parms.get("Columns", 1) or 1
    colors = parms.get("Colors", 1) or 1
    bpc = parms.get("BitsPerComponent", 8) or 8
    bytes_per_pixel = max(1, (colors * bpc + 7) // 8)
    row_len = (columns * colors * bpc + 7) // 8
    if predictor == 2:  # TIFF predictor 2 (only handle 8-bit case)
        if bpc != 8:
            return data
        out = bytearray(data)
        for r in range(0, len(out), row_len):
            row = out[r : r + row_len]
            for k in range(bytes_per_pixel, len(row)):
                row[k] = (row[k] + row[k - bytes_per_pixel]) & 0xFF
            out[r : r + row_len] = row
        return bytes(out)
    # PNG predictors (>= 10): each row prefixed with a filter-type byte
    out = bytearray()
    prev = bytearray(row_len)
    stride = row_len + 1
    for r in range(0, len(data), stride):
        ft = data[r]
        row = bytearray(data[r + 1 : r + stride])
        if len(row) < row_len:
            row.extend(b"\x00" * (row_len - len(row)))
        if ft == 0:  # None
            pass
        elif ft == 1:  # Sub
            for k in range(bytes_per_pixel, row_len):
                row[k] = (row[k] + row[k - bytes_per_pixel]) & 0xFF
        elif ft == 2:  # Up
            for k in range(row_len):
                row[k] = (row[k] + prev[k]) & 0xFF
        elif ft == 3:  # Average
            for k in range(row_len):
                left = row[k - bytes_per_pixel] if k >= bytes_per_pixel else 0
                row[k] = (row[k] + ((left + prev[k]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for k in range(row_len):
                a = row[k - bytes_per_pixel] if k >= bytes_per_pixel else 0
                b = prev[k]
                cc = prev[k - bytes_per_pixel] if k >= bytes_per_pixel else 0
                p = a + b - cc
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - cc)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = cc
                row[k] = (row[k] + pr) & 0xFF
        out.extend(row)
        prev = row
    return bytes(out)


def _lzw_decode(data: bytes, early_change: int = 1) -> bytes:
    """Decode PDF LZW data (variable code width, EarlyChange aware)."""
    CLEAR, EOD = 256, 257
    out = bytearray()
    bit_buffer = 0
    bits = 0
    code_width = 9
    table: list[bytes] = []

    def reset_table():
        nonlocal table, code_width
        table = [bytes([i]) for i in range(256)] + [b"", b""]
        code_width = 9

    reset_table()
    prev = None
    for byte in data:
        bit_buffer = (bit_buffer << 8) | byte
        bits += 8
        while bits >= code_width:
            bits -= code_width
            code = (bit_buffer >> bits) & ((1 << code_width) - 1)
            if code == EOD:
                return bytes(out)
            if code == CLEAR:
                reset_table()
                prev = None
                continue
            if prev is None:
                entry = table[code]
                out.extend(entry)
                prev = entry
                continue
            if code < len(table):
                entry = table[code]
            elif code == len(table):
                entry = prev + prev[:1]
            else:  # corrupt stream
                return bytes(out)
            out.extend(entry)
            table.append(prev + entry[:1])
            prev = entry
            if len(table) + early_change - 1 >= (1 << code_width) and code_width < 12:
                code_width += 1
    return bytes(out)


def _ascii85_decode(data: bytes) -> bytes:
    data = data.split(b"~>")[0]
    try:
        return base64.a85decode(data, adobe=False, ignorechars=b" \t\r\n\f\v")
    except (ValueError, binascii.Error):
        return b""


def _asciihex_decode(data: bytes) -> bytes:
    data = data.split(b">")[0]
    hexchars = bytes(b for b in data if b not in _WHITESPACE)
    if len(hexchars) % 2:
        hexchars += b"0"
    try:
        return binascii.unhexlify(hexchars)
    except binascii.Error:
        return b""


def decode_stream(raw: bytes, sdict: dict, resolve) -> bytes:
    filt = resolve(sdict.get("Filter"))
    if filt is None:
        return raw
    parms = resolve(sdict.get("DecodeParms")) or resolve(sdict.get("DP"))
    if not isinstance(filt, list):
        filt = [filt]
        parms = [parms]
    elif not isinstance(parms, list):
        parms = [parms] * len(filt)
    if len(parms) < len(filt):
        parms = parms + [None] * (len(filt) - len(parms))

    data = raw
    for f, p in zip(filt, parms):
        f = resolve(f)
        p = resolve(p) or {}
        p = {k: resolve(v) for k, v in p.items()} if isinstance(p, dict) else {}
        name = str(f)
        if name in ("FlateDecode", "Fl"):
            try:
                data = zlib.decompress(data)
            except zlib.error:
                # Some producers leave junk; try a tolerant decompressor.
                try:
                    d = zlib.decompressobj()
                    data = d.decompress(data) + d.flush()
                except zlib.error:
                    return b""
            data = _apply_predictor(data, p)
        elif name in ("LZWDecode", "LZW"):
            data = _lzw_decode(data, int(p.get("EarlyChange", 1)))
            data = _apply_predictor(data, p)
        elif name in ("ASCII85Decode", "A85"):
            data = _ascii85_decode(data)
        elif name in ("ASCIIHexDecode", "AHx"):
            data = _asciihex_decode(data)
        elif name in ("DCTDecode", "JPXDecode", "CCITTFaxDecode", "JBIG2Decode"):
            # Image data; no text here.
            return b""
        else:
            # Unknown filter -- give back what we have.
            return data
    return data


# --------------------------------------------------------------------------- #
# Document: scan objects, resolve refs, walk the page tree
# --------------------------------------------------------------------------- #

_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_STREAM_RE = re.compile(rb"stream(\r\n|\r|\n)")


class PDFDocument:
    def __init__(self, data: bytes):
        self.data = data
        self.objects: dict[tuple, object] = {}
        self._parse_objects()
        self._parse_object_streams()

    # -- object table ----------------------------------------------------- #

    def _parse_objects(self):
        data = self.data
        matches = list(_OBJ_RE.finditer(data))
        for idx, m in enumerate(matches):
            num, gen = int(m.group(1)), int(m.group(2))
            body_start = m.end()
            body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(data)
            region = data[body_start:body_end]
            sm = _STREAM_RE.search(region)
            if sm:
                value = parse_value(region, 0, sm.start())
                stream_data = self._extract_stream_bytes(region, sm, value)
                if isinstance(value, dict):
                    decoded = decode_stream(stream_data, value, self.resolve)
                    self.objects[(num, gen)] = Stream(value, decoded)
                else:
                    self.objects[(num, gen)] = stream_data
            else:
                self.objects[(num, gen)] = parse_value(region, 0)

    def _extract_stream_bytes(self, region, sm, value):
        start = sm.end()
        length = value.get("Length") if isinstance(value, dict) else None
        if isinstance(length, int) and 0 <= start + length <= len(region):
            candidate = region[start : start + length]
            # Verify it is actually followed by endstream (allowing whitespace).
            tail = region[start + length : start + length + 20]
            if b"endstream" in tail:
                return candidate
        end = region.find(b"endstream", start)
        if end == -1:
            end = len(region)
        chunk = region[start:end]
        # Trim the single EOL that precedes 'endstream'.
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith((b"\n", b"\r")):
            chunk = chunk[:-1]
        return chunk

    def _parse_object_streams(self):
        """Expand PDF 1.5 object streams (/Type /ObjStm) into the table."""
        for key, obj in list(self.objects.items()):
            if not isinstance(obj, Stream):
                continue
            if obj.dict.get("Type") != "ObjStm":
                continue
            n = self.resolve(obj.dict.get("N"))
            first = self.resolve(obj.dict.get("First"))
            if not isinstance(n, int) or not isinstance(first, int):
                continue
            header = obj.data[:first]
            nums = [int(x) for x in re.findall(rb"\d+", header)]
            pairs = list(zip(nums[0::2], nums[1::2]))[:n]
            for objnum, offset in pairs:
                if (objnum, 0) in self.objects:
                    continue
                val = parse_value(obj.data, first + offset)
                self.objects[(objnum, 0)] = val

    # -- reference resolution --------------------------------------------- #

    def resolve(self, obj, _depth=0):
        if isinstance(obj, Ref) and _depth < 50:
            target = self.objects.get(obj.key())
            if target is None:
                target = self.objects.get((obj.num, 0))
            return self.resolve(target, _depth + 1)
        return obj

    # -- page tree -------------------------------------------------------- #

    def _find_catalog(self):
        for obj in self.objects.values():
            d = obj.dict if isinstance(obj, Stream) else obj
            if isinstance(d, dict) and d.get("Type") == "Catalog":
                return d
        return None

    def pages(self):
        """Return page dictionaries in reading order, each augmented with the
        inherited Resources/MediaBox."""
        catalog = self._find_catalog()
        result: list[dict] = []
        if catalog is not None:
            root = self.resolve(catalog.get("Pages"))
            if isinstance(root, dict):
                self._walk_pages(root, {}, result, set())
        if not result:
            # Fallback: every /Type /Page object, in object-number order.
            page_objs = []
            for key, obj in self.objects.items():
                d = obj.dict if isinstance(obj, Stream) else obj
                if isinstance(d, dict) and d.get("Type") == "Page":
                    page_objs.append((key, dict(d)))
            page_objs.sort(key=lambda kv: kv[0])
            result = [d for _, d in page_objs]
        return result

    def _walk_pages(self, node, inherited, result, seen):
        node_id = id(node)
        if node_id in seen:
            return
        seen.add(node_id)
        inheritable = {}
        for k in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if k in node:
                inheritable[k] = node[k]
        merged = {**inherited, **inheritable}
        ntype = node.get("Type")
        if ntype == "Page" or ("Contents" in node and "Kids" not in node):
            page = dict(node)
            for k, v in merged.items():
                page.setdefault(k, v)
            result.append(page)
            return
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list):
            for kid in kids:
                child = self.resolve(kid)
                if isinstance(child, dict):
                    self._walk_pages(child, merged, result, seen)


# --------------------------------------------------------------------------- #
# Font handling (ToUnicode CMaps + 1- vs 2-byte codes)
# --------------------------------------------------------------------------- #


class Font:
    __slots__ = ("two_byte", "to_unicode", "has_glyph_problem")

    def __init__(self, two_byte: bool, to_unicode: dict):
        self.two_byte = two_byte
        self.to_unicode = to_unicode
        self.has_glyph_problem = False

    def decode(self, raw: bytes) -> str:
        out = []
        if self.two_byte:
            it = range(0, len(raw) - 1, 2)
            codes = [(raw[i] << 8) | raw[i + 1] for i in it]
            for code in codes:
                ch = self.to_unicode.get(code)
                if ch is None:
                    self.has_glyph_problem = True
                    ch = chr(code) if 32 <= code < 0x300 else "�"
                out.append(ch)
        else:
            for b in raw:
                ch = self.to_unicode.get(b)
                if ch is None:
                    ch = bytes([b]).decode("latin-1")
                out.append(ch)
        return "".join(out)


_BFCHAR_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_RE = re.compile(
    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])"
)


def _utf16be_to_str(hexbytes: bytes) -> str:
    raw = binascii.unhexlify(hexbytes if len(hexbytes) % 2 == 0 else hexbytes + b"0")
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_tounicode(data: bytes) -> dict:
    cmap: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in _BFCHAR_RE.findall(block):
            cmap[int(src, 16)] = _utf16be_to_str(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for src, dst, target in _BFRANGE_RE.findall(block):
            lo, hi = int(src, 16), int(dst, 16)
            if target.startswith(b"["):
                items = re.findall(rb"<([0-9A-Fa-f]+)>", target)
                for offset, item in enumerate(items):
                    if lo + offset <= hi:
                        cmap[lo + offset] = _utf16be_to_str(item)
            else:
                base = target.strip(b"<>")
                base_str = _utf16be_to_str(base)
                for code in range(lo, hi + 1):
                    # Increment the last code unit across the range.
                    if base_str:
                        cmap[code] = base_str[:-1] + chr(
                            ord(base_str[-1]) + (code - lo)
                        )
    return cmap


def build_fonts(doc: PDFDocument, resources: dict) -> dict:
    fonts: dict[str, Font] = {}
    resources = doc.resolve(resources) or {}
    font_dict = doc.resolve(resources.get("Font")) if isinstance(resources, dict) else None
    if not isinstance(font_dict, dict):
        return fonts
    for name, ref in font_dict.items():
        fobj = doc.resolve(ref)
        if not isinstance(fobj, dict):
            continue
        subtype = fobj.get("Subtype")
        encoding = doc.resolve(fobj.get("Encoding"))
        two_byte = subtype == "Type0"
        if isinstance(encoding, str) and "Identity" in encoding:
            two_byte = True
        to_unicode = {}
        tu = doc.resolve(fobj.get("ToUnicode"))
        if isinstance(tu, Stream):
            to_unicode = parse_tounicode(tu.data)
        fonts[name] = Font(two_byte=two_byte, to_unicode=to_unicode)
    return fonts


# --------------------------------------------------------------------------- #
# Content-stream text extraction
# --------------------------------------------------------------------------- #


def _page_content_bytes(doc: PDFDocument, page: dict) -> bytes:
    contents = doc.resolve(page.get("Contents"))
    chunks = []
    if isinstance(contents, Stream):
        chunks.append(contents.data)
    elif isinstance(contents, list):
        for ref in contents:
            obj = doc.resolve(ref)
            if isinstance(obj, Stream):
                chunks.append(obj.data)
    return b"\n".join(chunks)


def extract_page_text(doc: PDFDocument, page: dict):
    """Return (text, stats) for one page."""
    content = _page_content_bytes(doc, page)
    fonts = build_fonts(doc, page.get("Resources"))
    current_font = None
    default_font = Font(two_byte=False, to_unicode={})

    pieces: list[str] = []
    # Text positioning state (only the vertical baseline matters for line breaks)
    leading = 0.0
    last_y = None
    pending_newline = False
    line_started = False

    def get_font():
        return current_font if current_font is not None else default_font

    def emit_text(raw: bytes):
        nonlocal pending_newline, line_started
        if pending_newline and line_started:
            pieces.append("\n")
        pending_newline = False
        line_started = True
        pieces.append(get_font().decode(raw))

    def new_line():
        nonlocal pending_newline
        pending_newline = True

    stack: list = []
    tokens = tokenize(content)
    for tok in tokens:
        if tok.kind == "num":
            stack.append(tok.value)
        elif tok.kind == "name":
            stack.append(tok.value)
        elif tok.kind == "str":
            stack.append(tok.value)
        elif tok.kind == "[":
            stack.append("[")
        elif tok.kind == "]":
            # collapse to an array
            arr = []
            while stack and stack[-1] != "[":
                arr.append(stack.pop())
            if stack:
                stack.pop()  # the '['
            arr.reverse()
            stack.append(arr)
        elif tok.kind in ("<<", ">>"):
            # Inline dicts (e.g. BDC properties) -- drop operands harmlessly.
            if tok.kind == "<<":
                stack.append("<<")
            else:
                while stack and stack[-1] != "<<":
                    stack.pop()
                if stack:
                    stack.pop()
        elif tok.kind == "kw":
            op = tok.value
            if op == "Tf":
                if len(stack) >= 2 and isinstance(stack[-2], str):
                    current_font = fonts.get(stack[-2])
            elif op == "Tj":
                if stack and isinstance(stack[-1], (bytes, bytearray)):
                    emit_text(stack[-1])
            elif op in ("'", '"'):
                new_line()
                if stack and isinstance(stack[-1], (bytes, bytearray)):
                    emit_text(stack[-1])
            elif op == "TJ":
                if stack and isinstance(stack[-1], list):
                    font = get_font()
                    out = []
                    for el in stack[-1]:
                        if isinstance(el, (bytes, bytearray)):
                            out.append(font.decode(el))
                        elif isinstance(el, (int, float)) and el < -180:
                            out.append(" ")  # wide negative kern => space
                    if pending_newline and line_started:
                        pieces.append("\n")
                    pending_newline = False
                    line_started = True
                    pieces.append("".join(out))
            elif op == "Td" or op == "TD":
                if len(stack) >= 2 and all(
                    isinstance(v, (int, float)) for v in stack[-2:]
                ):
                    ty = stack[-1]
                    if op == "TD":
                        leading = -ty
                    if abs(ty) > 0.01:
                        new_line()
            elif op == "T*":
                new_line()
            elif op == "TL":
                if stack and isinstance(stack[-1], (int, float)):
                    leading = stack[-1]
            elif op == "Tm":
                if len(stack) >= 6:
                    y = stack[-1]
                    if isinstance(y, (int, float)):
                        if last_y is not None and abs(y - last_y) > 0.5:
                            new_line()
                        last_y = y
            elif op == "BT":
                last_y = None
            stack.clear() if op in (
                "Tj",
                "TJ",
                "'",
                '"',
                "Td",
                "TD",
                "Tm",
                "T*",
                "Tf",
                "TL",
                "BT",
                "ET",
                "cm",
                "Tc",
                "Tw",
                "Tz",
                "Ts",
                "Tr",
                "Do",
                "BDC",
                "BMC",
                "EMC",
                "gs",
            ) else None
            # For any other operator, also clear operands to keep the stack sane.
            if op not in ("Tj", "TJ", "'", '"', "Td", "TD", "Tm", "T*", "Tf", "TL", "BT"):
                stack.clear()

    text = "".join(pieces)
    text = re.sub(r"[ \t]+\n", "\n", text)
    font_problem = any(f.has_glyph_problem for f in fonts.values())
    stats = {
        "chars": len(text.strip()),
        "had_content": bool(content.strip()),
        "font_problem": font_problem,
    }
    return text, stats


def extract_text(path: Path):
    """Extract text from a PDF file. Returns (pages, warnings)."""
    data = Path(path).read_bytes()
    warnings: list[str] = []
    if not data.lstrip().startswith(b"%PDF"):
        warnings.append(f"{path}: does not look like a PDF (missing %PDF header).")
    if b"/Encrypt" in data:
        warnings.append(
            f"{path}: appears to be encrypted; text extraction may be empty. "
            "Decrypt it first (stdlib cannot)."
        )
    doc = PDFDocument(data)
    pages = doc.pages()
    page_texts = []
    total_chars = 0
    glyph_problem_pages = 0
    content_pages = 0
    for page in pages:
        text, stats = extract_page_text(doc, page)
        page_texts.append(text)
        total_chars += stats["chars"]
        if stats["had_content"]:
            content_pages += 1
        if stats["font_problem"]:
            glyph_problem_pages += 1
    if pages and total_chars == 0:
        if content_pages:
            warnings.append(
                f"{path}: pages contain drawing operators but no extractable "
                "text -- this is almost certainly a scanned/image-only PDF and "
                "needs OCR (not possible with the standard library)."
            )
        else:
            warnings.append(f"{path}: no text could be extracted.")
    if glyph_problem_pages:
        warnings.append(
            f"{path}: {glyph_problem_pages} page(s) use embedded fonts with no "
            "ToUnicode map; some characters may be wrong. Treat diffs there as "
            "'review manually'."
        )
    if not pages:
        warnings.append(f"{path}: no pages found (unsupported or corrupt PDF).")
    return page_texts, warnings


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


def _normalize(text: str, ignore_whitespace: bool) -> list[str]:
    lines = text.splitlines()
    if ignore_whitespace:
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in lines]
        lines = [ln for ln in lines if ln != ""]
    return lines


def compare(
    path_a: Path,
    path_b: Path,
    ignore_whitespace: bool = False,
    context: int = 3,
    by_page: bool = False,
):
    pages_a, warn_a = extract_text(path_a)
    pages_b, warn_b = extract_text(path_b)
    warnings = warn_a + warn_b

    sha_a = hashlib.sha256(Path(path_a).read_bytes()).hexdigest()
    sha_b = hashlib.sha256(Path(path_b).read_bytes()).hexdigest()

    result = {
        "file_a": str(path_a),
        "file_b": str(path_b),
        "sha256_a": sha_a,
        "sha256_b": sha_b,
        "bytes_identical": sha_a == sha_b,
        "pages_a": len(pages_a),
        "pages_b": len(pages_b),
        "warnings": warnings,
        "diffs": [],
        "text_identical": False,
    }

    if by_page:
        n = max(len(pages_a), len(pages_b))
        all_identical = len(pages_a) == len(pages_b)
        for i in range(n):
            ta = pages_a[i] if i < len(pages_a) else ""
            tb = pages_b[i] if i < len(pages_b) else ""
            la = _normalize(ta, ignore_whitespace)
            lb = _normalize(tb, ignore_whitespace)
            diff = list(
                difflib.unified_diff(
                    la,
                    lb,
                    fromfile=f"{path_a} (page {i + 1})",
                    tofile=f"{path_b} (page {i + 1})",
                    lineterm="",
                    n=context,
                )
            )
            if diff:
                all_identical = False
                result["diffs"].append({"page": i + 1, "diff": "\n".join(diff)})
        result["text_identical"] = all_identical
    else:
        la = _normalize("\n".join(pages_a), ignore_whitespace)
        lb = _normalize("\n".join(pages_b), ignore_whitespace)
        diff = list(
            difflib.unified_diff(
                la,
                lb,
                fromfile=str(path_a),
                tofile=str(path_b),
                lineterm="",
                n=context,
            )
        )
        result["text_identical"] = not diff
        if diff:
            result["diffs"].append({"page": None, "diff": "\n".join(diff)})

    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdfcompare",
        description="Compare the text content of two PDFs using only the Python "
        "standard library (no pip installs, safe for air-gapped environments).",
    )
    p.add_argument("a", help="first PDF (or the only PDF, with --dump)")
    p.add_argument("b", nargs="?", help="second PDF")
    p.add_argument(
        "-w",
        "--ignore-whitespace",
        action="store_true",
        help="ignore differences in whitespace and blank lines",
    )
    p.add_argument(
        "--by-page",
        action="store_true",
        help="diff page-by-page instead of as one document",
    )
    p.add_argument(
        "-c",
        "--context",
        type=int,
        default=3,
        help="lines of context around each change (default: 3)",
    )
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="highlight added/removed lines in color (default: auto)",
    )
    p.add_argument("--json", action="store_true", help="emit a JSON report")
    p.add_argument(
        "--dump",
        action="store_true",
        help="just print the extracted text of the first PDF and exit",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.dump:
        pages, warnings = extract_text(Path(args.a))
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        for i, text in enumerate(pages):
            print(f"----- page {i + 1} -----")
            print(text)
        return 0

    if not args.b:
        print("error: two PDF paths are required (or use --dump)", file=sys.stderr)
        return 2

    for path in (args.a, args.b):
        if not Path(path).is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2

    try:
        result = compare(
            Path(args.a),
            Path(args.b),
            ignore_whitespace=args.ignore_whitespace,
            context=args.context,
            by_page=args.by_page,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["text_identical"] else 1

    for w in result["warnings"]:
        print(f"warning: {w}", file=sys.stderr)

    if result["bytes_identical"]:
        print("Files are byte-for-byte identical (same SHA-256).")
        return 0

    if result["text_identical"]:
        print(
            "Files differ as bytes, but the extracted text is identical "
            "(e.g. only metadata/formatting changed)."
        )
        return 0

    print(
        f"Text differs between:\n  A: {result['file_a']}  "
        f"({result['pages_a']} pages)\n  B: {result['file_b']}  "
        f"({result['pages_b']} pages)\n"
    )
    use_color = _want_color(args.color)
    for entry in result["diffs"]:
        if entry["page"] is not None:
            print(f"===== page {entry['page']} =====")
        print(_colorize(entry["diff"]) if use_color else entry["diff"])
        print()
    return 1


# ANSI colors for highlighting changed lines in the terminal (stdlib only).
_RED = "\033[31m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def _want_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty()


def _colorize(diff: str) -> str:
    out = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(f"{_GREEN}{line}{_RESET}")
        elif line.startswith("-") and not line.startswith("---"):
            out.append(f"{_RED}{line}{_RESET}")
        elif line.startswith("@@"):
            out.append(f"{_CYAN}{line}{_RESET}")
        else:
            out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
