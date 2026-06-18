"""Process the fetched report according to the configured parameters."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass


@dataclass
class Processed:
    content: bytes
    ext: str


def _looks_json(content_type: str, data: bytes) -> bool:
    if "json" in content_type.lower():
        return True
    head = data.lstrip()[:1]
    return head in (b"{", b"[")


def _looks_csv(content_type: str) -> bool:
    ct = content_type.lower()
    return "csv" in ct or "text/plain" in ct


def _dig(obj, dotted: str):
    cur = obj
    for key in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _process_json(data: bytes, fields: list[str]) -> bytes:
    obj = json.loads(data.decode("utf-8", errors="replace"))
    if fields:
        obj = {f: _dig(obj, f) for f in fields}
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def _process_csv(data: bytes, columns: list[str]) -> bytes:
    text = data.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not columns or reader.fieldnames is None:
        return data
    keep = [c for c in columns if c in reader.fieldnames]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=keep, extrasaction="ignore")
    writer.writeheader()
    for row in reader:
        writer.writerow({c: row.get(c, "") for c in keep})
    return out.getvalue().encode("utf-8")


def process(data: bytes, content_type: str, process_cfg: dict) -> Processed:
    ptype = (process_cfg.get("type") or "auto").lower()

    if ptype == "auto":
        if _looks_json(content_type, data):
            ptype = "json"
        elif _looks_csv(content_type):
            ptype = "csv"
        else:
            ptype = "raw"

    if ptype == "json":
        return Processed(_process_json(data, process_cfg.get("json_fields") or []), "json")
    if ptype == "csv":
        return Processed(_process_csv(data, process_cfg.get("csv_columns") or []), "csv")
    return Processed(data, "bin")
