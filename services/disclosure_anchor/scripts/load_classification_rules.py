"""Load class_map.json + facet_map.json into disclosure_core.classification_rule.

The versioned JSON files in the repo are the source of truth; the table is a
query-side copy the views and worker predicates join against. Vocabulary
upgrade = edit JSON, bump version, `make load-rules` — every view-derived
classification follows immediately, no stale rows, no reclassify tool.
Idempotent: TRUNCATE + INSERT in one transaction.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text

from disclosure_anchor.adapters.sources.cninfo.mapper import (
    load_class_map,
    load_facet_map,
    load_filing_type_rule_bundle,
)


def main() -> int:
    class_map = load_class_map()
    facet_map = load_facet_map()
    rows: list[dict[str, object]] = []
    for name, spec in class_map["classes"].items():
        for prefix in spec["prefixes"]:
            rows.append(
                {
                    "rule_set": "class",
                    "prefix": prefix,
                    "value": name,
                    "priority": spec["priority"],
                    "version": class_map["version"],
                }
            )
    for rule in facet_map["rules"]:
        for prefix in rule["prefixes"]:
            rows.append(
                {
                    "rule_set": "facet",
                    "prefix": prefix,
                    "value": rule["facet"],
                    "priority": rule["priority"],
                    "version": facet_map["version"],
                }
            )
    # Title keyword rules: the code-less channel's ONLY classification path
    # (0017 — no materialized filing_type anywhere). File order = priority
    # (semiannual before annual: substring shadowing), '%' joins an
    # all-keywords rule into one LIKE pattern.
    bundle = load_filing_type_rule_bundle()
    for position, rule in enumerate(bundle.rules):
        pattern = "%".join(rule.keywords) if rule.match == "all" else None
        for keyword in ([pattern] if pattern else rule.keywords):
            rows.append(
                {
                    "rule_set": "title",
                    "prefix": keyword,
                    "value": rule.filing_type,
                    "priority": 1000 - position,
                    "version": bundle.version,
                }
            )
    # Topic rules (0021): additive title hits consulted for coded AND
    # code-less documents — they fill provider-code blind spots. Priority is
    # the class's own class_map priority so the filing_type argmax compares
    # code hits and topic hits on one scale.
    for topic_rule in bundle.topic_rules:
        class_priority = class_map["classes"][topic_rule.class_name]["priority"]
        for keyword in topic_rule.keywords:
            rows.append(
                {
                    "rule_set": "title_topic",
                    "prefix": keyword,
                    "value": topic_rule.class_name,
                    "priority": class_priority,
                    "version": bundle.version,
                }
            )

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        # TRUNCATE takes ACCESS EXCLUSIVE; a long-running view reader would
        # queue us and everything behind us. Fail fast instead of wedging.
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text("TRUNCATE disclosure_core.classification_rule"))
        conn.execute(
            text(
                "INSERT INTO disclosure_core.classification_rule"
                " (rule_set, prefix, value, priority, version)"
                " VALUES (:rule_set, :prefix, :value, :priority, :version)"
            ),
            rows,
        )
    counts = {"class": 0, "facet": 0, "title": 0, "title_topic": 0}
    for row in rows:
        counts[str(row["rule_set"])] += 1
    print(
        f"loaded {counts['class']} class rules ({class_map['version']}, "
        f"{len(class_map['classes'])} classes) + {counts['facet']} facet rules "
        f"({facet_map['version']}) + {counts['title']} title rules "
        f"+ {counts['title_topic']} title_topic rules ({bundle.version})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
