"""xbrlextract — turn a filed XBRL instance into a reviewable Excel workbook.

Public API::

    from xbrlextract import extract
    result = extract("infy-20260331_htm.xml",
                     schema="infy-20260331.xsd",
                     output_xlsx="infy_facts.xlsx")
    print(result.fact_count, "facts ->", result.output_xlsx)

The instance (``*_htm.xml``) supplies the facts, contexts and units; the schema
(``*.xsd``, with embedded linkbases) supplies labels, the statement structure and
the calculation relationships. The schema is optional — without it you still get
facts/contexts/units, just with concept names instead of friendly labels.
"""

from __future__ import annotations

from dataclasses import dataclass

from .instance import Instance, parse_instance
from .taxonomy import Taxonomy, parse_schema
from .workbook import build_workbook

__all__ = ["extract", "ExtractResult", "parse_instance", "parse_schema"]


@dataclass
class ExtractResult:
    instance: Instance
    taxonomy: Taxonomy | None
    output_xlsx: str | None

    @property
    def fact_count(self) -> int:
        return len(self.instance.facts)

    @property
    def context_count(self) -> int:
        return len(self.instance.contexts)

    @property
    def concept_count(self) -> int:
        return len({f.concept for f in self.instance.facts})


def extract(
    instance_path: str,
    schema: str | None = None,
    output_xlsx: str | None = None,
) -> ExtractResult:
    """Parse a filed XBRL instance and optionally write an Excel workbook.

    Args:
        instance_path: path to the ``*_htm.xml`` extracted XBRL instance.
        schema: path to the ``*.xsd`` extension schema (for labels/structure).
        output_xlsx: if given, write the workbook here.
    """
    inst = parse_instance(instance_path)
    tax = parse_schema(schema) if schema else None
    written = None
    if output_xlsx is not None:
        wb = build_workbook(inst, tax, source=instance_path)
        wb.save(output_xlsx)
        written = output_xlsx
    return ExtractResult(inst, tax, written)
