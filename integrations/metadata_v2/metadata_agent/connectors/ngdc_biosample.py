"""Parse the sample-specific portion of an NGDC BioSample detail page."""
from __future__ import annotations

import re
from typing import Any

from lxml import etree, html

BIOSAMPLE_BROWSE_URL = "https://ngdc.cncb.ac.cn/biosample/browse"
SAMC_PARSER_VERSION = "ngdc-biosample-v1"

# NGDC serves Chinese labels even for English sample values. Keep original
# labels/values alongside these normalized keys for source evidence.
_LABELS = {
    "样本编号": "accession", "biosample accession": "accession", "accession": "accession",
    "样品名称": "sample_name", "样本名称": "sample_name", "sample name": "sample_name",
    "样本标题": "sample_title", "sample title": "sample_title",
    "样品类型": "sample_type", "样本类型": "sample_type", "sample type": "sample_type",
    "物种名称": "organism", "来源生物": "organism", "organism": "organism",
    "organism name": "organism", "scientific name": "organism",
    "描述信息": "description", "description": "description", "public description": "description",
    "项目编号": "bioproject", "project accession": "bioproject", "bioproject accession": "bioproject",
    "年龄": "age", "性别": "sex", "gender": "sex", "组织器官": "tissue", "tissue/organ": "tissue",
    "疾病名称": "disease", "疾病分期": "disease_stage", "健康状况": "health_status",
    "细胞系": "cell_line", "细胞类型": "cell_type", "细胞亚型": "cell_subtype",
    "发育阶段": "development_stage", "基因型": "genotype", "处理方法": "treatment",
    "实验材料提供者": "biomaterial_provider", "品种": "breed", "种": "strain",
    "血统性（民族）": "ethnicity", "种族（人种）": "race", "表型": "phenotype",
    "发布日期": "release_date", "release date": "release_date",
}
_GENERAL_KEYS = {"accession", "sample_name", "sample_title", "sample_type", "organism",
                 "description", "bioproject", "release_date"}
_XREF = re.compile(r"\b(CR[RA]\d{6,10}|SAM[NEDC][A-Z]?\d{6,10})\b")
_PROJECT = re.compile(r"\bPRJCA\d+\b")


def _text(element) -> str:
    return " ".join(" ".join(element.itertext()).split()).strip()


def _key(label: str) -> str:
    label = label.strip().rstrip(":：").casefold()
    return _LABELS.get(label, re.sub(r"[\s-]+", "_", label))


def current_samc_snapshot(record: dict[str, Any]) -> bool:
    raw = record.get("raw", {})
    return isinstance(raw, dict) and raw.get("_samc_parser") == SAMC_PARSER_VERSION and not raw.get("error")


def parse_samc_page(text: str, accession: str) -> dict[str, Any]:
    """Reject error/listing pages; never parse the related-samples sidebar."""
    try:
        document = html.fromstring(text)
    except (ValueError, etree.ParserError):
        return {"error": "empty or invalid NGDC BioSample page"}
    candidates = []
    for table in document.xpath("//table[not(ancestor::table)]"):
        pairs = []
        for row in table.xpath("./tr|./tbody/tr|./thead/tr"):
            cells = row.xpath("./th|./td")
            if len(cells) != 2 or cells[1].xpath(".//table"):
                continue
            label, value = _text(cells[0]), _text(cells[1])
            if label and value:
                pairs.append((label, value))
        ids = [value.upper() for label, value in pairs if _key(label) == "accession"]
        if ids == [accession]:
            candidates.append((table, pairs))
    if len(candidates) != 1:
        return {"error": f"no unique public BioSample detail record for {accession}"}
    table, pairs = candidates[0]
    attributes: dict[str, Any] = {}
    original = []

    def add(label, value):
        key = _key(label)
        original.append({"name": label, "value": value})
        previous = attributes.get(key)
        if previous is None:
            attributes[key] = value
        elif value != previous and (not isinstance(previous, list) or value not in previous):
            attributes[key] = [*previous, value] if isinstance(previous, list) else [previous, value]

    for label, value in pairs:
        if _key(label) in _GENERAL_KEYS:
            add(label, value)
    for nested in table.xpath(".//table[@id='attribute_table']"):
        for row in nested.xpath("./tr|./tbody/tr"):
            cells = row.xpath("./th|./td")
            if len(cells) == 2:
                label, value = _text(cells[0]), _text(cells[1])
                if label and value:
                    add(label, value)
    if not any(attributes.get(k) for k in ("sample_title", "sample_name", "organism")):
        return {"error": f"BioSample detail fields missing for {accession}"}

    # The gwh table is the page's 'data in this sample' resource table. The
    # page-wide SAMC list contains other specimens and is deliberately excluded.
    texts = [" ".join(value for label, value in pairs if _key(label) in _GENERAL_KEYS)]
    texts.extend(_text(node) for node in document.xpath("//table[@id='gwh']"))
    links = [link for link in dict.fromkeys(_XREF.findall(" ".join(texts))) if link != accession]
    raw: dict[str, Any] = {
        "_samc_parser": SAMC_PARSER_VERSION, "_attributes": attributes,
        "attributes": original, "_links": links,
        "sample_accession": accession, "sample_url": f"{BIOSAMPLE_BROWSE_URL}/{accession}",
    }
    title = attributes.get("sample_title") or attributes.get("sample_name")
    if title:
        raw["title"] = title
    if attributes.get("description"):
        raw["description"] = attributes["description"]
    projects = _PROJECT.findall(str(attributes.get("bioproject", "")))
    studies = [link for link in links if link.startswith("CRA")]
    if studies or projects:
        raw["study_accession"] = list(dict.fromkeys([*studies, *projects]))
    return raw
