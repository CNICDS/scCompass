"""Resume v2 metadata reviews from JSONL snapshots of source records."""

from collections import Counter
from contextlib import ExitStack, closing
import gzip
import json

import extraction_run
from metadata_agent.cli.fetch import _build_http_client
from metadata_agent.extract.tiers import TierExtractor
from metadata_agent.store.canonical import defers_canonical_write


PUBLICATION_FIELDS = {"Pubmed ID", "Citation", "Journal", "Year"}


def add_publications(record, publications):
    pmids = [s.strip() for s in str(publications.get("Pubmed ID") or "").split(",")]
    references = ", ".join(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" for pmid in pmids if pmid.isdecimal())
    if not references:
        return
    for name, value in publications.items():
        if name in PUBLICATION_FIELDS and value:
            record[name] = int(value) if name == "Year" else value
            record.setdefault("_field_acquisition", []).append({
                "field_name": name, "tier": "T1", "source_ref": references,
            })


def review_snapshot(args):
    stats = Counter()
    selected = {value.strip().upper() for value in args.accession}
    open_input = gzip.open if args.input.suffix == ".gz" else open
    with ExitStack() as resources:
        source = resources.enter_context(open_input(args.input, "rt", encoding="utf-8"))
        http = resources.enter_context(closing(_build_http_client()))
        pipeline, canonical, cache = extraction_run._build_pipeline(
            True, args.max_llm_calls, http, api_mode=args.llm_api,
        )
        resources.callback(canonical.close)
        resources.callback(cache.close)
        resources.callback(pipeline.close)

        # The snapshot already carries publication evidence. Keep review on
        # the supplied raw records rather than repeating publication lookups.
        pipeline._tier_extractor = TierExtractor(extraction_run.RUN_CONFIG, pipeline._schema)
        for line in source:
            if args.limit is not None and stats["attempted"] >= args.limit:
                break
            if not line.strip():
                continue
            row = json.loads(line)
            accession = row["accession"].strip().upper()
            if selected and accession not in selected:
                continue
            if row.get("record_level") == "study" and not args.include_studies:
                stats["study_skipped"] += 1
                continue
            if row.get("metadata_evidence") == "catalog_only":
                stats["no_primary_metadata"] += 1
                continue
            connector = row["source"].lower()
            source_name = connector.upper()
            if canonical.is_extraction_complete(source_name, accession, pipeline.profile_fingerprint):
                stats["already_reviewed"] += 1
                continue
            stats["attempted"] += 1
            raw = {"source": connector, "accession": accession, "source_url": row["source_url"], "raw": row["raw"]}
            record = pipeline.extract_one(raw, connector_id=connector)
            if record is None:
                raise ValueError(f"No extraction result for {source_name} {accession}")
            record["Source"], record["SourceID"], record["Address"] = source_name, accession, row["source_url"]
            if not record.get("SeriesID"):
                record["SeriesID"] = row.get("series_id")
            if not defers_canonical_write(record.get("_status")):
                add_publications(record, row.get("baseline_publications") or {})
            result = "committed" if canonical.upsert(record) else "pending"
            stats[result] += 1
            print(f"[metadata review] {source_name} {accession}: {result}", flush=True)
    return dict(stats)
