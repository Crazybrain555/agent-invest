"""Versioned unit-builder rules for CN A-share disclosures."""

from __future__ import annotations

import re
from dataclasses import dataclass


RULES_VERSION = "ub-2026.07-6"
HEADING_RULESET_ID = "cn_a_v3"
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


# Announcement header KV lines (证券代码：600519 / 证券简称：贵州茅台). The
# values are already document metadata (security join), so the lines are pure
# duplication — but 公告编号 lines are NOT stripped: the provider announcement
# number exists nowhere else in our metadata.
_HEADER_KV_SEG = r"(?:[ABH]\s*股|证\s*券|股\s*票|债\s*券)\s*(?:代\s*码|简\s*称)\s*[：:]\s*\S{1,24}"
HEADER_KV_LINE_RE = re.compile(rf"^\s*{_HEADER_KV_SEG}\s*$")
# One line may carry several KV segments plus the announcement number
# ("证券代码：600519 证券简称：贵州茅台 公告编号：临 2026-027"): strip the KV
# segments, keep the announcement number (unique information).
HEADER_KV_COMBO_RE = re.compile(
    rf"^\s*(?:{_HEADER_KV_SEG}\s*){{1,4}}(?P<keep>公告编号\s*[：:].{{1,30}}?)?\s*$"
)


def strip_header_kv_line(line: str) -> str | None:
    """Return the replacement for a header KV line, or None to keep it as-is.

    '' means the whole line is metadata duplication and should be dropped;
    a non-empty string keeps only the announcement-number segment.
    """

    match = HEADER_KV_COMBO_RE.fullmatch(line)
    if match is None:
        return None
    keep = match.group("keep")
    return keep.strip() if keep else ""


# Standalone table-unit declarations ("单位：元" on its own line). S5 already
# extracts the value into table payload `unit` from the element stream, so a
# text unit carrying only this line is pure duplication (audited: 194 of 908
# units in a real annual report).
UNIT_DECLARATION_RE = re.compile(r"^\s*单\s*位\s*[：:]\s*\S{1,8}\s*$")


def classify_marker_line(line: str) -> str | None:
    """Classify one line as an applicability declaration (end-anchored)."""

    if APPLICABLE_MARK_RE.search(line):
        return "applicable"
    if NOT_APPLICABLE_MARK_RE.search(line):
        return "not_applicable"
    return None


def is_pure_marker_line(line: str) -> bool:
    """True when the whole line is nothing but the declaration itself."""

    stripped = line.strip()
    if not stripped:
        return False
    match = APPLICABLE_MARK_RE.search(stripped) or NOT_APPLICABLE_MARK_RE.search(stripped)
    return match is not None and match.start() == 0


# Yes/no checkbox answers ("是 □否", "□是 √否"). They are disclosure answers,
# not applicability declarations: the line stays in unit text, but it must
# never become a heading or a table caption/title (observed as a table title
# in the real 江海 annual corpus, 2026-07-06).
YES_NO_ANSWER_RE = re.compile(
    rf"^\s*[{_CHECKED}{_UNCHECKED}]?\s*是\s*[{_CHECKED}{_UNCHECKED}]?\s*否\s*[。.]?\s*$"
)


def is_checkbox_answer_line(line: str) -> bool:
    stripped = line.strip()
    if not any(glyph in stripped for glyph in _CHECKED + _UNCHECKED):
        return False
    return bool(YES_NO_ANSWER_RE.match(stripped))


def is_declaration_line(line: str) -> bool:
    """Any checkbox declaration: applicability marker or yes/no answer."""

    return is_pure_marker_line(line) or is_checkbox_answer_line(line)


# Semantic grouping (ub-2026.07-5). L2-facing units must be business-semantic
# blocks, not MinerU element slices: a meeting proposal (审议结果 + 表决表格 +
# 会议决定) is ONE unit with ordered parts, and a short filing without proposal
# structure is ONE document-level unit. Thresholds follow the phase008 round3
# over-fragmentation audit.
PROPOSAL_ANCHOR_RE = re.compile(r"^\s*\d{1,3}\s*[.、．]?\s*议案名称\s*[：:]")
SHORT_DOC_CONTENT_CHARS = 8000
COLLAPSIBLE_FILING_TYPES = frozenset(
    {"other", "performance_forecast", "performance_flash"}
)
DOCUMENT_HEADER_ANCHOR = "公告头信息"

# Long structured documents (annual reports, long 制度/办法) group at the
# shallowest heading node whose subtree stays within this budget — deep enough
# to be one business topic (研发投入、附注某科目), shallow enough that reading
# the unit answers one business question. An oversized LEAF still merges whole:
# splitting one topic by payload kind is the exact defect round3 P0#1 forbids.
SECTION_GROUP_MAX_CHARS = 8000

NOISE_SEPARATOR_RE = re.compile(r"^[\s\-—―=_·•\*~～]{3,}$")
NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = ()

# Canonical CN filing hierarchy: 节/章 > 一、 > （一） > 1、 > （1） > ①.
# Both full-width and half-width parens per level: MinerU flattens audit-note
# headings to heading_level=2, so an unmatched "(1) 明细情况" used to enter at
# level 2 and evict its 科目 parent from the stack (Codex round4 P1#2).
# Digit-paren (（1）/1）) forms were previously unmapped, so MinerU's own
# heading_level placed them right under the 节 and they swallowed sibling
# 1、-level topics as children (round3 P1#11 drift; observed merging 研发投入
# into （8）客户/供应商 on the real 江海 annual). Levels beyond the depth-4
# heading_path cap simply stay in the unit text — that is the desired shape.
HEADING_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"^第[一二三四五六七八九十百]+[节章]")),
    (2, re.compile(r"^[一二三四五六七八九十]+、")),
    (3, re.compile(r"^（[一二三四五六七八九十]+）|^\([一二三四五六七八九十]+\)")),
    (4, re.compile(r"^\d+([.、．]|\s)")),
    (5, re.compile(r"^（\d+）|^\(\d+\)|^\d+[)）]")),
    (6, re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]")),
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
