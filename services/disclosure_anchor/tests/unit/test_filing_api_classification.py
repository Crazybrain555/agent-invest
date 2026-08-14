from __future__ import annotations

import unittest

from disclosure_anchor.api.routers.classification import get_semantic_routes
from disclosure_anchor.application.services.semantic_taxonomy import (
    load_semantic_route_taxonomy,
)


class SemanticRouteCatalogTests(unittest.TestCase):
    def test_catalog_is_generated_from_the_router_taxonomy(self) -> None:
        taxonomy = load_semantic_route_taxonomy()
        response = get_semantic_routes()

        self.assertEqual(response.contract_version, "semantic_routes_catalog.v1")
        self.assertEqual(response.taxonomy_version, taxonomy.version)
        self.assertEqual(response.route_count, len(taxonomy.definitions))
        self.assertEqual(
            [route.key for route in response.routes],
            [definition.key for definition in taxonomy.definitions],
        )
        self.assertEqual(
            [route.usable_as_section_key for route in response.routes],
            [
                definition.context_container or definition.section_container
                for definition in taxonomy.definitions
            ],
        )

    def test_catalog_exposes_labels_and_scopes_without_private_fallback(self) -> None:
        response = get_semantic_routes()
        by_key = {route.key: route for route in response.routes}

        self.assertNotIn("document_content", by_key)
        self.assertIn("business_review", by_key)
        self.assertTrue(by_key["business_review"].usable_as_section_key)
        self.assertIn("annual_report", by_key["business_review"].scopes)
        self.assertTrue(by_key["business_review"].labels)
        self.assertTrue(by_key["subscription_arrangements"].usable_as_section_key)
        self.assertIn("rights_issue", by_key["subscription_arrangements"].scopes)


if __name__ == "__main__":
    unittest.main()
