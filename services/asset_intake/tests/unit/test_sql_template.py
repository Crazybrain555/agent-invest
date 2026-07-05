import unittest

from asset_intake.providers.registry import SafetySpec, SqlTemplate, SqlTemplateParam
from asset_intake.providers.sql_template import validate_template

SAFETY = SafetySpec(
    deny_tokens=[";", "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "EXEC", "OPENROWSET"],
    default_max_rows=10000,
)


def template(statement: str, **kwargs) -> SqlTemplate:
    defaults = dict(
        params={"wind_code": SqlTemplateParam(type="wind_code", source="query.security"),
                "max_rows": SqlTemplateParam(type="int", default=100)},
        required_predicates=["S_INFO_WINDCODE"],
        max_rows=100,
        timeout_seconds=30,
    )
    defaults.update(kwargs)
    return SqlTemplate(statement=statement, **defaults)


GOOD = """
SELECT TOP (:max_rows) S_INFO_WINDCODE, TRADE_DT
FROM {{table}}
WHERE S_INFO_WINDCODE = :wind_code
"""


class SqlTemplateValidationTests(unittest.TestCase):
    def test_good_template_passes(self) -> None:
        self.assertEqual(validate_template(template(GOOD), SAFETY), [])

    def test_hardcoded_table_rejected(self) -> None:
        issues = validate_template(
            template(GOOD.replace("{{table}}", "dbo.AShareEODPrices")), SAFETY
        )
        self.assertTrue(any("{{table}}" in i for i in issues))

    def test_deny_tokens_and_semicolon(self) -> None:
        issues = validate_template(template(GOOD + "; DELETE FROM {{table}}"), SAFETY)
        text = " ".join(issues)
        self.assertIn("single statement", text)
        self.assertIn("DELETE", text)

    def test_select_star_rejected(self) -> None:
        issues = validate_template(
            template("SELECT * FROM {{table}} WHERE S_INFO_WINDCODE = :wind_code AND TOP (:max_rows) = 1"),
            SAFETY,
        )
        self.assertTrue(any("SELECT *" in i for i in issues))

    def test_undeclared_and_unused_params(self) -> None:
        issues = validate_template(
            template(GOOD + " AND TRADE_DT >= :mystery"), SAFETY
        )
        self.assertTrue(any("mystery" in i for i in issues))
        issues = validate_template(
            template(GOOD, params={
                "wind_code": SqlTemplateParam(type="wind_code"),
                "max_rows": SqlTemplateParam(type="int"),
                "ghost": SqlTemplateParam(type="int"),
            }),
            SAFETY,
        )
        self.assertTrue(any("ghost" in i for i in issues))

    def test_missing_required_predicate(self) -> None:
        issues = validate_template(
            template("SELECT TOP (:max_rows) TRADE_DT FROM {{table}} WHERE TRADE_DT = :wind_code"),
            SAFETY,
        )
        self.assertTrue(any("S_INFO_WINDCODE" in i for i in issues))

    def test_max_rows_over_provider_cap(self) -> None:
        issues = validate_template(template(GOOD, max_rows=999999), SAFETY)
        self.assertTrue(any("exceeds" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
