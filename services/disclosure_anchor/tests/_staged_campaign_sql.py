"""Execute production campaign SELECTs in memory with narrow PG dialect shims.

WHERE predicates, join structure, sorting and LIMIT are never replaced. This
does not substitute for the separately authorized PostgreSQL integration gate.
"""
from datetime import UTC, datetime
import json
import re

import sqlalchemy as sa

from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope, M6CorpusEntry
from disclosure_anchor.application.contracts.staged_campaign_v4 import V4CampaignAdmissionScope
from tests import m6_support as m6


NOW = datetime(2026, 9, 14, tzinfo=UTC)


def campaign(pairs=None, *, count=12, carry_in=False):
    if pairs is None:
        entries = [m6.entry(f"selected-{i:02}", pages=2, mode="e2e_publication",
                            origin="carry_in" if carry_in else "fresh") for i in range(count)]
    else:
        entries = [M6CorpusEntry(document_id=document, source_pdf_sha256=source,
                                source_byte_count=1024, source_page_count=2,
                                stratum="native", origin="carry_in" if carry_in else "fresh")
                   for document, source in pairs]
    manifest = m6.manifest("e2e_publication", *entries)
    return V4CampaignAdmissionScope(scope=M6CampaignScope.from_manifest(manifest), manifest=manifest)


class CampaignSqlDatabase:
    def __init__(self):
        self.engine = sa.create_engine("sqlite+pysqlite:///:memory:")
        self.statements = []

        @sa.event.listens_for(self.engine, "connect")
        def functions(connection, _record):
            connection.create_function("clock_timestamp", 0, lambda: NOW.isoformat())
            connection.create_function("jsonb_typeof", 1, lambda raw: None if raw is None else (
                "boolean" if type(json.loads(raw)) is bool else
                "number" if isinstance(json.loads(raw), (int, float)) else "string"))
            connection.create_collation("C", lambda a, b: (a > b)-(a < b))

        @sa.event.listens_for(self.engine, "before_cursor_execute", retval=True)
        def dialect(_connection, _cursor, statement, parameters, _context, many):
            original = statement
            statement = re.sub(r"=\s*ANY\(\?\)", " IN (SELECT value FROM json_each(?))", statement)
            statement = statement.replace(
                "unnest(CAST(? AS text[]), CAST(? AS text[])) AS s(document_id, source_pdf_sha256)",
                "(SELECT j.value AS document_id,k.value AS source_pdf_sha256 "
                "FROM json_each(?) j JOIN json_each(?) k ON j.key=k.key) AS s")
            statement = statement.replace("(sa.result_snapshot->>'byte_count')::numeric::bigint",
                                          "CAST((sa.result_snapshot->>'byte_count') AS INTEGER)")
            statement = statement.replace("CAST(clock_timestamp() AS DATETIME)", "clock_timestamp()")
            if not many:
                parameters = tuple(json.dumps(p) if isinstance(p, (list, tuple)) else p for p in parameters)
            self.statements.append((original, parameters))
            return statement, parameters

        with self.engine.begin() as c:
            c.exec_driver_sql("ATTACH DATABASE ':memory:' AS disclosure_core")
            c.exec_driver_sql("ATTACH DATABASE ':memory:' AS disclosure_ops")
            for sql in (
                "CREATE TABLE disclosure_ops.pending_parse_v1(document_id TEXT,status TEXT,failed_parse_count INTEGER,last_failed_retryable BOOLEAN)",
                "CREATE TABLE disclosure_core.document(document_id TEXT,provider TEXT,provider_document_id TEXT,security_id TEXT,raw_file_relpath TEXT,raw_file_hash TEXT,source_access_id TEXT,company_id TEXT)",
                "CREATE TABLE disclosure_core.security(security_id TEXT,security_code TEXT)",
                "CREATE TABLE disclosure_core.source_access(source_access_id TEXT,result_snapshot TEXT)",
                "CREATE TABLE disclosure_core.tracked_company(company_id TEXT,status TEXT)",
                "CREATE TABLE disclosure_core.processing_run(document_id TEXT,run_kind TEXT,provider_document_relpath TEXT,normalized_ir_relpath TEXT,status TEXT,error TEXT)",
                "CREATE TABLE disclosure_ops.remote_parse_attempt(attempt_id TEXT,document_id TEXT,source_pdf_sha256 TEXT,checkpoint_contract_version INTEGER,state TEXT,is_current BOOLEAN,row_version INTEGER,claim_generation INTEGER,claim_owner_identity TEXT,claim_lease_until DATETIME)",
            ):
                c.exec_driver_sql(sql)
            c.exec_driver_sql("INSERT INTO disclosure_core.security VALUES ('sec-1','000001')")

    def close(self):
        self.engine.dispose()

    def add_document(self, document, source):
        with self.engine.begin() as c:
            c.execute(sa.text("INSERT INTO disclosure_ops.pending_parse_v1 VALUES (:d,'registered',0,1)"), {"d": document})
            c.execute(sa.text("INSERT INTO disclosure_core.document VALUES (:d,'cninfo',:d,'sec-1',:p,:s,NULL,NULL)"),
                      {"d": document, "p": "raw_documents/"+document+".pdf", "s": source})

    def add_head(self, attempt, document, source, *, state="prepared", current=True, version=4, claimed=False):
        with self.engine.begin() as c:
            c.execute(sa.text("INSERT INTO disclosure_ops.remote_parse_attempt VALUES (:a,:d,:s,:v,:st,:cur,0,:gen,:owner,NULL)"),
                      {"a": attempt, "d": document, "s": source, "v": version, "st": state,
                       "cur": current, "gen": int(claimed), "owner": "old-owner" if claimed else None})

    def total_changes(self):
        with self.engine.connect() as c:
            return c.exec_driver_sql("SELECT total_changes()").scalar_one()
