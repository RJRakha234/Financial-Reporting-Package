"""Parse an XBRL instance document (the EDGAR ``*_htm.xml`` extracted instance).

The instance stores data in a *normalised* form: every numeric value (a *fact*)
points at a ``context`` (who / when / which dimension) and a ``unit`` by id. This
module re-joins them into flat :class:`Fact` rows that downstream code can write
to a spreadsheet or validate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


@dataclass
class Dimension:
    axis: str  # qname, e.g. "ifrs-full:OperatingSegmentsAxis"
    member: str  # qname for explicit, or the typed value
    explicit: bool = True

    def __str__(self) -> str:
        return f"{self.axis} = {self.member}"


@dataclass
class Context:
    id: str
    entity: str
    scheme: str
    period_type: str  # "instant" | "duration"
    start: str = ""
    end: str = ""
    instant: str = ""
    dimensions: list[Dimension] = field(default_factory=list)

    def period_str(self) -> str:
        if self.period_type == "instant":
            return f"as at {self.instant}"
        return f"{self.start} → {self.end}"

    def dims_str(self) -> str:
        return " ; ".join(str(d) for d in self.dimensions)


@dataclass
class Unit:
    id: str
    measure: str  # friendly, e.g. "INR", "shares", "INR / shares"
    raw: str  # the raw measure qname(s)


@dataclass
class Fact:
    id: str
    concept: str  # qname, e.g. "ifrs-full:Revenue"
    prefix: str
    localname: str
    value: str | None  # raw text; None when xsi:nil
    context_ref: str
    unit_ref: str
    decimals: str
    is_nil: bool = False


@dataclass
class Instance:
    facts: list[Fact]
    contexts: dict[str, Context]
    units: dict[str, Unit]
    nsmap: dict[str, str]  # uri -> prefix

    def context_of(self, fact: Fact) -> Context | None:
        return self.contexts.get(fact.context_ref)

    def unit_of(self, fact: Fact) -> Unit | None:
        return self.units.get(fact.unit_ref)


def _qname(tag: str, uri_to_prefix: dict[str, str]) -> tuple[str, str, str]:
    """Return (qname, prefix, localname) for a Clark-notation ``{uri}local`` tag."""
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        prefix = uri_to_prefix.get(uri, uri)
        return f"{prefix}:{local}", prefix, local
    return tag, "", tag


def _measure_text(measure_el, uri_to_prefix: dict[str, str]) -> str:
    """A <measure> text like 'iso4217:INR' -> friendly 'INR'."""
    raw = (measure_el.text or "").strip()
    if ":" in raw:
        pfx, local = raw.split(":", 1)
        # iso4217:INR -> INR ; xbrli:shares -> shares ; xbrli:pure -> pure
        if pfx in ("iso4217", "xbrli"):
            return local
        return local
    return raw


def parse_instance(path: str) -> Instance:
    tree = etree.parse(path)
    root = tree.getroot()
    uri_to_prefix = {uri: (pfx or "") for pfx, uri in root.nsmap.items()}

    contexts: dict[str, Context] = {}
    units: dict[str, Unit] = {}
    facts: list[Fact] = []

    for el in root.iterchildren():
        tag = el.tag
        if not isinstance(tag, str):
            continue  # comments / PIs
        if tag == f"{{{XBRLI}}}context":
            contexts[el.get("id")] = _parse_context(el)
        elif tag == f"{{{XBRLI}}}unit":
            units[el.get("id")] = _parse_unit(el, uri_to_prefix)
        elif tag == f"{{{XBRLI}}}schemaRef":
            continue
        elif el.get("contextRef") is not None:
            facts.append(_parse_fact(el, uri_to_prefix))

    return Instance(facts, contexts, units, uri_to_prefix)


def _parse_context(el) -> Context:
    ident = el.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
    entity = (ident.text or "").strip() if ident is not None else ""
    scheme = ident.get("scheme") if ident is not None else ""

    period = el.find(f"{{{XBRLI}}}period")
    ptype, start, end, instant = "duration", "", "", ""
    if period is not None:
        inst = period.find(f"{{{XBRLI}}}instant")
        if inst is not None:
            ptype, instant = "instant", (inst.text or "").strip()
        else:
            s = period.find(f"{{{XBRLI}}}startDate")
            e = period.find(f"{{{XBRLI}}}endDate")
            start = (s.text or "").strip() if s is not None else ""
            end = (e.text or "").strip() if e is not None else ""

    dims: list[Dimension] = []
    for em in el.iter(f"{{{XBRLDI}}}explicitMember"):
        dims.append(Dimension(em.get("dimension"), (em.text or "").strip(), True))
    for tm in el.iter(f"{{{XBRLDI}}}typedMember"):
        # typed dimension: record the dimension and a compact dump of the value
        child = next(iter(tm), None)
        val = (child.text or "").strip() if child is not None else ""
        dims.append(Dimension(tm.get("dimension"), val, False))

    return Context(el.get("id"), entity, scheme, ptype, start, end, instant, dims)


def _parse_unit(el, uri_to_prefix: dict[str, str]) -> Unit:
    divide = el.find(f"{{{XBRLI}}}divide")
    if divide is not None:
        num = divide.find(f"{{{XBRLI}}}unitNumerator/{{{XBRLI}}}measure")
        den = divide.find(f"{{{XBRLI}}}unitDenominator/{{{XBRLI}}}measure")
        n = _measure_text(num, uri_to_prefix) if num is not None else "?"
        d = _measure_text(den, uri_to_prefix) if den is not None else "?"
        return Unit(el.get("id"), f"{n} / {d}", f"{num.text if num is not None else ''} / {den.text if den is not None else ''}")
    measure = el.find(f"{{{XBRLI}}}measure")
    friendly = _measure_text(measure, uri_to_prefix) if measure is not None else ""
    raw = (measure.text or "").strip() if measure is not None else ""
    return Unit(el.get("id"), friendly, raw)


def _parse_fact(el, uri_to_prefix: dict[str, str]) -> Fact:
    qname, prefix, local = _qname(el.tag, uri_to_prefix)
    is_nil = el.get(f"{{{XSI}}}nil") == "true"
    value = None if is_nil else (el.text or "").strip()
    return Fact(
        id=el.get("id", ""),
        concept=qname,
        prefix=prefix,
        localname=local,
        value=value,
        context_ref=el.get("contextRef", ""),
        unit_ref=el.get("unitRef", ""),
        decimals=el.get("decimals", ""),
        is_nil=is_nil,
    )
