"""Versioned unit-builder rules for CN A-share disclosures."""

from __future__ import annotations

import re
from dataclasses import dataclass


RULES_VERSION = "ub-2026.07-2"
HEADING_RULESET_ID = "cn_a_v1"
GIBBERISH_RATIO_MAX = 0.30

# A-share applicability declaration lines ("√适用 □不适用" / "□适用 √不适用").
# A not-applicable mark is information (the section is declared exempt), so it
# becomes a structured payload flag for L2 filtering instead of being dropped.
#  is the private-use checked-box glyph MinerU emits for the ticked box
# (observed 154x in the 江海 annual report corpus); the marker may be glued to
# the end of a heading line, so matching is end-anchored.
_CHECKED = "√☑✓"
_UNCHECKED = "□☐"
APPLICABLE_MARK_RE = re.compile(
    rf"[{_CHECKED}]\s*适\s*用\s*[{_UNCHECKED}]\s*不\s*适\s*用\s*[。.]?\s*$"
)
NOT_APPLICABLE_MARK_RE = re.compile(
    rf"[{_UNCHECKED}]\s*适\s*用\s*[{_CHECKED}]\s*不\s*适\s*用\s*[。.]?\s*$"
)


def detect_applicability(text: str) -> str | None:
    """Return 'applicable' / 'not_applicable' when a marker line is present."""

    for line in text.splitlines():
        if APPLICABLE_MARK_RE.search(line):
            return "applicable"
        if NOT_APPLICABLE_MARK_RE.search(line):
            return "not_applicable"
    return None

NOISE_SEPARATOR_RE = re.compile(r"^[\s\-—―=_·•\*~～]{3,}$")
NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = ()

HEADING_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"^第[一二三四五六七八九十百]+[节章]")),
    (2, re.compile(r"^[一二三四五六七八九十]+、")),
    (3, re.compile(r"^（[一二三四五六七八九十]+）")),
    (4, re.compile(r"^\d+([.、．]|\s)")),
    (5, re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]")),
)
FIXED_L1_TITLES = {"重要提示", "释义", "目录", "备查文件"}
SKIP_SECTION_TITLES = {"释义", "目录", "备查文件"}

QUESTION_START_RE = re.compile(
    r"^\s*(问题|问|Q\d*|投资者提问|提问)\s*\d*\s*[：:]"
    r"|^\s*\d+[、.．]\s*.{2,}[？?]\s*$"
)
ANSWER_START_RE = re.compile(r"^\s*(答|回复|公司回复|A\d*)\s*[：:]")

SEMANTIC_LIMITED_FILING_TYPES = {
    "annual_report",
    "semiannual_report",
    "quarterly_report",
    "inquiry_reply",
}


@dataclass(frozen=True)
class SemanticKeyRule:
    semantic_key: str
    required: tuple[str, ...] = ()
    any_required: tuple[str, ...] = ()
    filing_type_limited: bool = True


SEMANTIC_KEY_RULES: tuple[SemanticKeyRule, ...] = (
    SemanticKeyRule("receivable_aging", required=("应收账款",), any_required=("账龄", "坏账")),
    SemanticKeyRule("inventory_breakdown", required=("存货",), any_required=("分类", "构成", "跌价")),
    SemanticKeyRule("goodwill_impairment", required=("商誉",), any_required=()),
    SemanticKeyRule(
        "revenue_breakdown",
        required=(),
        any_required=("分行业", "分产品", "分地区", "营业收入构成"),
    ),
    SemanticKeyRule("guarantee", required=("担保",), any_required=()),
    SemanticKeyRule("related_party", required=(), any_required=("关联交易", "关联方")),
    SemanticKeyRule(
        "shareholder_structure",
        required=("股东",),
        any_required=("结构", "变动", "情况", "前10名", "前十名"),
    ),
    SemanticKeyRule(
        "shareholder_structure",
        required=("股本",),
        any_required=("结构", "变动", "情况", "前10名", "前十名"),
    ),
    SemanticKeyRule(
        "shareholder_structure",
        required=("股份变动",),
        any_required=("结构", "变动", "情况", "前10名", "前十名"),
    ),
    SemanticKeyRule(
        "tariff_exposure",
        required=("关税",),
        any_required=(),
        filing_type_limited=False,
    ),
)
