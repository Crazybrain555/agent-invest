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

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE disclosure_core.classification_rule"))
        conn.execute(
            text(
                "INSERT INTO disclosure_core.classification_rule"
                " (rule_set, prefix, value, priority, version)"
                " VALUES (:rule_set, :prefix, :value, :priority, :version)"
            ),
            rows,
        )
    class_rows = sum(1 for row in rows if row["rule_set"] == "class")
    print(
        f"loaded {class_rows} class rules ({class_map['version']}, "
        f"{len(class_map['classes'])} classes) + "
        f"{len(rows) - class_rows} facet rules ({facet_map['version']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
