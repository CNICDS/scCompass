"""scCompass commands for v2 metadata collection, review, and CSV export."""

import argparse
from contextlib import closing
import csv
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def build_parser():
    parser = argparse.ArgumentParser(prog="scCompass metadata", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="Fetch accessions and extract sample metadata")
    collect.add_argument("accessions", nargs="*")
    collect.add_argument("--accessions-file", type=Path, help="Text file with one accession per line")
    collect.add_argument("--workers", type=int, choices=range(1, 9), default=3)
    collect.add_argument("--resume", action="store_true")
    collect.add_argument("--skip-harvest", action="store_true", help="Use raw snapshots in the output folder")
    collect.add_argument("--no-enrich", action="store_true")
    collect.add_argument("--with-llm", action="store_true")
    review = commands.add_parser("review", help="Review records from a JSONL or JSONL.gz snapshot")
    review.add_argument("--input", type=Path, required=True)
    review.add_argument("--accession", action="append", default=[], help="Select an accession, repeatable")
    review.add_argument("--include-studies", action="store_true")
    budget = review.add_mutually_exclusive_group(required=True)
    budget.add_argument("--limit", type=positive)
    budget.add_argument("--all", action="store_true")
    export = commands.add_parser("export", help="Export completed rows from an existing canonical store")
    for command in (collect, review, export):
        command.add_argument("--output", type=Path, required=True, help="Workspace for canonical.sqlite and metadata.csv")
    for command in (collect, review):
        command.add_argument("--env-file", type=Path, help="Private dotenv file, loaded only when supplied")
        command.add_argument("--config", type=Path, help="Private metadata_agent YAML override")
        command.add_argument("--llm-api", choices=("messages", "responses", "chat_completions"))
        command.add_argument("--max-llm-calls", type=nonnegative, default=20 if command is collect else 2)
    return parser


def load_run_config(path, review):
    if path:
        from metadata_agent.config.loader import load_config
        config = load_config(path)
    else:
        config = {"extraction": {"tiers": {
            "T0": {"enabled": True, "fields": ["Library", "Assay", "RNASource", "RawFile", "RawSize_GB", "License"]},
            "T1": {"enabled": True, "fields": ["Pubmed ID", "Citation", "Journal", "Year"]},
            "T2": {"enabled": False, "fields": ["AnnotationMethod", "CellNumberRaw", "CellNumberAfterQC",
                                              "CellNumberAfterAnnotation", "MedianUmi", "DoubletsPerc"]},
        }}}
    if review:
        config.setdefault("extraction", {}).setdefault("tiers", {}).setdefault("T1", {})["enabled"] = False
    return config


def export_csv(directory):
    """Read completed canonical rows without modifying the original SQLite store."""
    database = directory / "canonical.sqlite"
    output = directory / "metadata.csv"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.execute("BEGIN")
        metadata_fields = [row[1] for row in db.execute("PRAGMA table_info(canonical)")
                           if row[1] not in {"content_hash", "profile_fingerprint", "ontology_fingerprint"}]
        if not {"Source", "SourceID"} <= set(metadata_fields):
            raise ValueError("No canonical metadata table with Source and SourceID")
        pending = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'extraction_pending'").fetchone()
        query = "SELECT " + ", ".join('c."' + name.replace('"', '""') + '"' for name in metadata_fields)
        query += " FROM canonical AS c"
        if pending:
            query += ' WHERE NOT EXISTS (SELECT 1 FROM extraction_pending AS p WHERE p."Source" = c."Source" AND p."SourceID" = c."SourceID")'
        query += ' ORDER BY c."SourceID", c."Source"'
        source_index = metadata_fields.index("SourceID")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=directory,
                                             prefix=".metadata-", suffix=".csv", delete=False) as stream:
                temporary = Path(stream.name)
                writer = csv.writer(stream)
                writer.writerow(metadata_fields)
                last = None
                count = 0
                for row in db.execute(query):
                    accession = row[source_index]
                    if not accession or accession == last:
                        raise ValueError("Canonical metadata has a blank or duplicate SourceID")
                    writer.writerow(row)
                    last = accession
                    count += 1
            temporary.replace(output)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    print(json.dumps({"rows": count, "csv": str(output)}))


def collect(args, parser):
    import extraction_run
    from metadata_agent.connectors.accession_router import route_batch

    accessions = list(args.accessions)
    if args.accessions_file:
        accessions.extend(line.strip() for line in args.accessions_file.read_text(encoding="utf-8-sig").splitlines()
                          if line.strip())
    if not accessions:
        parser.error("collect requires accession arguments or --accessions-file")
    grouped, errors = route_batch(accessions)
    if errors:
        parser.error(f"{len(errors)} input accession(s) could not be routed: {next(iter(errors.values()))}")
    if not args.skip_harvest:
        extraction_run.step_harvest(grouped, resume=args.resume)
    _, stats = extraction_run.step_extract(
        grouped, with_llm=args.with_llm, max_llm_calls=args.max_llm_calls,
        enrich=not args.no_enrich, resume=args.resume, llm_api=args.llm_api, workers=args.workers,
    )
    return stats


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.output = args.output.expanduser().resolve()
    if args.command == "export":
        export_csv(args.output)
        return 0
    if args.env_file:
        from dotenv import load_dotenv
        env_file = args.env_file.expanduser()
        if not env_file.is_file():
            parser.error("the supplied --env-file does not exist")
        load_dotenv(env_file, override=False)
    if os.environ.get("API_KEY") and not os.environ.get("NCBI_API_KEY"):
        os.environ["NCBI_API_KEY"] = os.environ["API_KEY"]
    from metadata_agent.config.secrets import RedactingFilter, get_llm_api_key
    import extraction_run

    review = args.command == "review"
    config = load_run_config(args.config.expanduser() if args.config else None, review=review)
    if (review or args.with_llm) and args.max_llm_calls > 0:
        from metadata_agent.config.llm import resolve_llm_settings
        settings = resolve_llm_settings(config, api_mode=args.llm_api)
        if not get_llm_api_key(settings.api_mode):
            parser.error("set model credentials in the environment or provide a private --env-file")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    extraction_run.RUN_CONFIG = config
    extraction_run.DATA_DIR = args.output
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".metadata.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(f"metadata workspace is already in use: {args.output}")
        if review:
            from review_snapshot import review_snapshot
            stats = review_snapshot(args)
        else:
            stats = collect(args, parser)
        print(json.dumps({"stats": stats}))
        export_csv(args.output)
    return 2 if any(stats.get(key, 0) for key in ("deferred", "failed", "pending")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
