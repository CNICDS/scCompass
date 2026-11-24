"""Per-connector discovery-query builders — render the golden-口径 class set into
each source's native search syntax.

The single literal ``"human cell line"`` only ever surfaced cell lines. Instead
we build, per connector, a query that groups the configured ``discovery_terms``
(default ``[organ, cell_line, organoid, induced_cell]``) with ``OR`` **before**
appending the organism and date predicates, so organ / organoid / induced-cell
samples are discovered too. Underscored config names map to source phrases
(``cell_line`` → "cell line"); organism text that could alter query syntax is
rejected.

Discovery is multi-species: an organism list renders one OR-grouped predicate
per connector. The incremental harvest still queries each organism serially
(see ``harvest.runner``) so a per-organism count is observable and one
organism's failure cannot cost the others; the grouped form serves backfill and
any caller that wants the whole species set in a single query.
"""
from __future__ import annotations

import re

DEFAULT_DISCOVERY_TERMS = ["organ", "cell_line", "organoid", "induced_cell"]

# Query-side scientific names, ordered — not the ``OrganismSimple`` vocabulary.
# ``Homo sapiens`` stays first so the human-only recall this widened is kept.
DEFAULT_ORGANISMS = [
    "Homo sapiens",
    "Mus musculus",
    "Rattus norvegicus",
    "Pan troglodytes",
    "Macaca mulatta",
    "Cavia porcellus",
    "Gallus gallus",
    "Danio rerio",
    "Drosophila melanogaster",
    "Arabidopsis thaliana",
]

# Underscored config names → source-side phrases.
_TERM_PHRASES = {
    "organ": "organ",
    "cell_line": "cell line",
    "organoid": "organoid",
    "induced_cell": "induced cell",
}

# Characters / tokens in an organism string that could break out of the query.
_ORG_UNSAFE = re.compile(r'["\[\]()\\\n\r\t]|\b(?:AND|OR|NOT)\b')

# Distinguishes an absent ``organism`` key (take the default) from an explicit
# null (query without an organism constraint).
_UNSET = object()

# Entrez entry-type filter constraining GEO GDS results to sample (GSM) entries.
GEO_ENTRY_TYPE = "gsm[ETYP]"


class QueryConfigError(ValueError):
    """Raised when discovery-query configuration is invalid or unsafe."""


def _phrase(term: str) -> str:
    return _TERM_PHRASES.get(term, term.replace("_", " ").strip())


def _terms(db_conf: dict) -> list[str]:
    terms = db_conf.get("discovery_terms") or DEFAULT_DISCOVERY_TERMS
    return [t for t in terms if isinstance(t, str) and t.strip()]


def _safe_organism(organism: str | None) -> str | None:
    if organism is None:
        return None
    text = str(organism).strip()
    if not text:
        return None
    if _ORG_UNSAFE.search(text):
        raise QueryConfigError(f"organism string could alter query syntax: {organism!r}")
    return text


def resolve_organisms(
    db_conf: dict, organism: list[str] | str | None = None
) -> list[str]:
    """Ordered, de-duplicated, syntax-safe organisms a connector should query.

    ``DEFAULT_ORGANISMS`` applies only when builder-driven discovery is
    configured and the ``organism`` key is *absent*, so a legacy explicit-query
    config keeps its single unconstrained search instead of fanning out ten
    times. An explicit ``None`` (YAML ``organism:``) or ``[]`` is an opt-out and
    stays empty — unsetting the constraint must never widen it.
    """
    value = organism if organism is not None else db_conf.get("organism", _UNSET)
    if value is _UNSET:
        value = DEFAULT_ORGANISMS if db_conf.get("discovery_terms") else None

    values = value if isinstance(value, (list, tuple)) else [value]
    resolved: list[str] = []
    for item in values:
        safe = _safe_organism(item)
        if safe and safe not in resolved:
            resolved.append(safe)
    return resolved


def _explicit_queries(db_conf: dict) -> list[str]:
    return [q for q in (db_conf.get("search_queries") or []) if isinstance(q, str) and q.strip()]


def organism_filters_query(db_conf: dict) -> bool:
    """True when the connector's query is organism-sensitive — i.e. rendered by
    the golden-口径 builder rather than taken from a legacy ``search_queries``
    literal, which ignores the organism entirely. Fanning a species list out
    over a legacy config would just repeat the identical search N times.
    """
    return bool(db_conf.get("discovery_terms")) or not _explicit_queries(db_conf)


def _entrez_token(phrase: str) -> str:
    return f'"{phrase}"' if " " in phrase else phrase


def _entrez_group(db_conf: dict) -> str:
    return "(" + " OR ".join(_entrez_token(_phrase(t)) for t in _terms(db_conf)) + ")"


def _organism_group(organisms: list[str], predicate: str) -> str:
    """``AND``-joined organism constraint: a bare predicate for one organism, an
    OR group for several, nothing at all when unconstrained."""
    if not organisms:
        return ""
    if len(organisms) == 1:
        return f" AND {predicate.format(organisms[0])}"
    return " AND (" + " OR ".join(predicate.format(org) for org in organisms) + ")"


def _build_entrez(
    db_conf: dict, organisms: list[str], *, entry_type: str | None = None
) -> str:
    query = _entrez_group(db_conf) + _organism_group(organisms, '"{}"[Organism]')
    if entry_type:
        query += f" AND {entry_type}"
    return query


def _build_arrayexpress(db_conf: dict, organisms: list[str]) -> str:
    return _entrez_group(db_conf) + _organism_group(organisms, '"{}"')


def _build_ena(db_conf: dict, organisms: list[str]) -> str:
    # ENA Portal (result=sample): class terms as description wildcards; organism
    # as scientific_name. Class group precedes the organism predicate.
    group = "(" + " OR ".join(f'description="*{_phrase(t)}*"' for t in _terms(db_conf)) + ")"
    return group + _organism_group(organisms, 'scientific_name="{}"')


_BUILDERS = {
    "geo": lambda c, o: _build_entrez(c, o, entry_type=GEO_ENTRY_TYPE),
    "sra": _build_entrez,
    "arrayexpress": _build_arrayexpress,
    "ena": _build_ena,
    "gsa": _build_arrayexpress,  # GSA browse takes a free-text keyword group
    "biosample": _build_entrez,
}


def build_query(
    connector: str, db_conf: dict, organism: list[str] | str | None = None
) -> str:
    """Build a golden-口径 discovery query in ``connector``'s native syntax."""
    organisms = resolve_organisms(db_conf, organism)
    builder = _BUILDERS.get(connector, _build_entrez)
    return builder(db_conf, organisms)


def resolve_query(
    connector: str, db_conf: dict, organism: list[str] | str | None = None
) -> str:
    """Query the connector should run: the golden-口径 builder when
    ``discovery_terms``/``organism`` drive discovery, else the explicit
    (legacy-normalized) ``search_queries`` literal for back-compat.
    """
    if db_conf.get("discovery_terms") or organism is not None:
        return build_query(connector, db_conf, organism)
    explicit = _explicit_queries(db_conf)
    if explicit:
        if len(explicit) == 1:
            return explicit[0]
        if connector in ("geo", "sra", "biosample"):
            return " OR ".join(f"({q})" for q in explicit)
        return " ".join(explicit)
    return build_query(connector, db_conf, organism)
