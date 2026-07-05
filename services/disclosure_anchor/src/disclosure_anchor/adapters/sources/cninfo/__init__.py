"""CNINFO source adapter."""

from disclosure_anchor.adapters.sources.cninfo.client import (
    CninfoClient,
    CninfoClientError,
    CninfoResponse,
    RequestAudit,
    TokenBucket,
    redact_params,
)
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CNINFO_PROVIDER,
    CninfoCompanyProfile,
    CninfoMappingError,
    FilingTypeRuleBundle,
    category_prefix_matches,
    load_filing_type_rule_bundle,
    map_filing_type,
    map_p_info3015_record,
    map_p_stock2100_record,
    split_category_segments,
)

__all__ = [
    "CNINFO_PROVIDER",
    "CninfoClient",
    "CninfoClientError",
    "CninfoCompanyProfile",
    "CninfoMappingError",
    "CninfoResponse",
    "FilingTypeRuleBundle",
    "RequestAudit",
    "TokenBucket",
    "category_prefix_matches",
    "load_filing_type_rule_bundle",
    "map_filing_type",
    "map_p_info3015_record",
    "map_p_stock2100_record",
    "redact_params",
    "split_category_segments",
]
