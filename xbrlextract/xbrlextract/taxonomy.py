"""Parse the taxonomy extension schema (``*.xsd``) with its embedded linkbases.

Infosys (like most filers) embeds the label, presentation, calculation and
definition linkbases inside the ``.xsd`` rather than shipping them as separate
files. This module reads:

* **element declarations** — each extension concept's data type, period type and
  debit/credit balance;
* **the label linkbase** — a human-readable label for each concept (and which
  concepts carry a *negated* label, i.e. are displayed with a flipped sign);
* **role definitions** — the statement / disclosure names ("Consolidated Balance
  Sheet", …);
* **the presentation linkbase** — the ordered concept tree for each statement;
* **the calculation linkbase** — parent = Σ(child × weight) footing relations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

XSD = "http://www.w3.org/2001/XMLSchema"
XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XL = "http://www.w3.org/1999/xlink"

ROLE_STD_LABEL = "http://www.xbrl.org/2003/role/label"
LABEL_ROLE_PREFERENCE = (
    "http://www.xbrl.org/2003/role/terseLabel",
    ROLE_STD_LABEL,
    "http://www.xbrl.org/2003/role/verboseLabel",
)


@dataclass
class Concept:
    qname: str
    label: str
    type: str = ""
    period_type: str = ""
    balance: str = ""
    abstract: bool = False
    extension: bool = False
    negated: bool = False


@dataclass
class Role:
    uri: str
    number: str  # sort prefix, e.g. "75010"
    name: str  # e.g. "Consolidated Balance Sheet"
    kind: str  # "Statement" | "Document" | "Disclosure" | ""


@dataclass
class PresNode:
    qname: str
    depth: int
    order: float
    preferred_label: str = ""


@dataclass
class CalcRel:
    parent: str
    child: str
    weight: float
    order: float


@dataclass
class Taxonomy:
    target_ns: str
    concepts: dict[str, Concept]  # qname -> Concept
    roles: dict[str, Role]  # roleURI -> Role
    presentation: dict[str, list[PresNode]]  # roleURI -> ordered nodes
    calculation: dict[str, list[CalcRel]]  # roleURI -> relations

    def label(self, qname: str) -> str:
        c = self.concepts.get(qname)
        return c.label if c and c.label else humanize(qname.split(":", 1)[-1])


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def humanize(localname: str) -> str:
    """'CashAndCashEquivalents' -> 'Cash And Cash Equivalents' (fallback label)."""
    return _CAMEL.sub(" ", localname).replace("_", " ").strip()


def _href_to_qname(href: str, uri_to_prefix: dict[str, str]) -> str | None:
    """'…#infy_Foo' / '…#ifrs-full_Foo' -> 'infy:Foo' / 'ifrs-full:Foo'."""
    if "#" not in href:
        return None
    frag = href.rsplit("#", 1)[1]
    if "_" not in frag:
        return None
    prefix, name = frag.split("_", 1)
    return f"{prefix}:{name}"


def parse_schema(path: str) -> Taxonomy:
    tree = etree.parse(path)
    root = tree.getroot()
    target_ns = root.get("targetNamespace", "")
    uri_to_prefix = {uri: (pfx or "") for pfx, uri in root.nsmap.items()}
    ext_prefix = uri_to_prefix.get(target_ns, "")

    concepts: dict[str, Concept] = {}

    # 1. Extension element declarations (data type, period type, balance).
    for el in root.iter(f"{{{XSD}}}element"):
        name = el.get("name")
        if not name:
            continue
        qname = f"{ext_prefix}:{name}" if ext_prefix else name
        ctype = el.get("type", "")
        if ":" in ctype:
            ctype = ctype.split(":", 1)[1]
        concepts[qname] = Concept(
            qname=qname,
            label="",
            type=ctype,
            period_type=el.get(f"{{{XBRLI}}}periodType", ""),
            balance=el.get(f"{{{XBRLI}}}balance", ""),
            abstract=el.get("abstract", "false") == "true",
            extension=True,
        )

    # 2. Labels (resolve loc -> labelArc -> label resource).
    _attach_labels(root, concepts, uri_to_prefix)

    # 3. Role definitions.
    roles = _parse_roles(root)

    # 4. Presentation & calculation networks.
    presentation = _parse_presentation(root, uri_to_prefix)
    calculation = _parse_calculation(root, uri_to_prefix)

    return Taxonomy(target_ns, concepts, roles, presentation, calculation)


def _ensure(concepts: dict[str, Concept], qname: str) -> Concept:
    c = concepts.get(qname)
    if c is None:
        c = Concept(qname=qname, label="")
        concepts[qname] = c
    return c


def _attach_labels(root, concepts, uri_to_prefix) -> None:
    label_link = root.find(f".//{{{LINK}}}labelLink")
    if label_link is None:
        return
    # loc xlink:label -> concept qname
    loc_to_qname: dict[str, str] = {}
    for loc in label_link.iter(f"{{{LINK}}}loc"):
        q = _href_to_qname(loc.get(f"{{{XL}}}href", ""), uri_to_prefix)
        if q:
            loc_to_qname[loc.get(f"{{{XL}}}label")] = q
    # label xlink:label -> {role: text}
    label_res: dict[str, dict[str, str]] = {}
    for lab in label_link.iter(f"{{{LINK}}}label"):
        key = lab.get(f"{{{XL}}}label")
        label_res.setdefault(key, {})[lab.get(f"{{{XL}}}role", "")] = lab.text or ""
    # arcs connect a loc to its label resources
    for arc in label_link.iter(f"{{{LINK}}}labelArc"):
        qname = loc_to_qname.get(arc.get(f"{{{XL}}}from"))
        roles = label_res.get(arc.get(f"{{{XL}}}to"))
        if not qname or not roles:
            continue
        c = _ensure(concepts, qname)
        if not c.label:
            c.label = _best_label(roles)
        if any("negated" in r.lower() for r in roles):
            c.negated = True


def _best_label(roles: dict[str, str]) -> str:
    for role in LABEL_ROLE_PREFERENCE:
        if roles.get(role):
            return roles[role].strip()
    # otherwise any non-documentation label
    for role, text in roles.items():
        if "documentation" not in role and text.strip():
            return text.strip()
    return ""


def _parse_roles(root) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for rt in root.iter(f"{{{LINK}}}roleType"):
        uri = rt.get("roleURI")
        d = rt.find(f"{{{LINK}}}definition")
        text = (d.text or "").strip() if d is not None else ""
        number, kind, name = "", "", text
        # definitions look like "75010 - Statement - Consolidated Balance Sheet"
        parts = [p.strip() for p in text.split(" - ")]
        if len(parts) >= 3:
            number, kind, name = parts[0], parts[1], " - ".join(parts[2:])
        elif len(parts) == 2:
            number, name = parts[0], parts[1]
        roles[uri] = Role(uri, number, name, kind)
    return roles


def _walk_network(link_el, uri_to_prefix):
    """Yield (from_qname, to_qname, arc) for an extended-link element."""
    loc_to_qname: dict[str, str] = {}
    for loc in link_el.iter(f"{{{LINK}}}loc"):
        q = _href_to_qname(loc.get(f"{{{XL}}}href", ""), uri_to_prefix)
        if q:
            loc_to_qname[loc.get(f"{{{XL}}}label")] = q
    return loc_to_qname


def _parse_presentation(root, uri_to_prefix) -> dict[str, list[PresNode]]:
    nets: dict[str, list[PresNode]] = {}
    for link_el in root.iter(f"{{{LINK}}}presentationLink"):
        role = link_el.get(f"{{{XL}}}role")
        loc_to_qname = _walk_network(link_el, uri_to_prefix)
        children: dict[str, list[tuple[float, str, str]]] = {}
        has_parent: set[str] = set()
        all_nodes: set[str] = set()
        for arc in link_el.iter(f"{{{LINK}}}presentationArc"):
            p = loc_to_qname.get(arc.get(f"{{{XL}}}from"))
            c = loc_to_qname.get(arc.get(f"{{{XL}}}to"))
            if not p or not c:
                continue
            order = float(arc.get("order", "0") or 0)
            pref = arc.get("preferredLabel", "") or ""
            children.setdefault(p, []).append((order, c, pref))
            has_parent.add(c)
            all_nodes.update((p, c))
        roots = [n for n in all_nodes if n not in has_parent]
        ordered: list[PresNode] = []
        seen: set[str] = set()

        def visit(node, depth, order, pref):
            if node in seen:
                return
            seen.add(node)
            ordered.append(PresNode(node, depth, order, pref))
            for o, ch, pr in sorted(children.get(node, []), key=lambda t: t[0]):
                visit(ch, depth + 1, o, pr)

        for r in roots:
            visit(r, 0, 0.0, "")
        if ordered:
            nets[role] = ordered
    return nets


def _parse_calculation(root, uri_to_prefix) -> dict[str, list[CalcRel]]:
    nets: dict[str, list[CalcRel]] = {}
    for link_el in root.iter(f"{{{LINK}}}calculationLink"):
        role = link_el.get(f"{{{XL}}}role")
        loc_to_qname = _walk_network(link_el, uri_to_prefix)
        rels: list[CalcRel] = []
        for arc in link_el.iter(f"{{{LINK}}}calculationArc"):
            p = loc_to_qname.get(arc.get(f"{{{XL}}}from"))
            c = loc_to_qname.get(arc.get(f"{{{XL}}}to"))
            if not p or not c:
                continue
            rels.append(
                CalcRel(
                    parent=p,
                    child=c,
                    weight=float(arc.get("weight", "1") or 1),
                    order=float(arc.get("order", "0") or 0),
                )
            )
        if rels:
            nets[role] = sorted(rels, key=lambda r: (r.parent, r.order))
    return nets
