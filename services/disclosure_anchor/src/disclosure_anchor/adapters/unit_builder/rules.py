"""Versioned unit-builder rules for CN A-share disclosures."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
import re
from dataclasses import dataclass
import unicodedata


RULES_VERSION = "ub-2026.07-62"
HEADING_RULESET_ID = "cn_a_v6"
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
# Ambiguous OCR splits are preserved. A metadata cleaner must fail closed:
# permissive free-text values can swallow a real sentence following the code.
_HEADER_SECURITY_PREFIX = (
    r"(?:[ABH]\s*股(?:\s*(?:证\s*券|股\s*票))?|证\s*券|股\s*票)"
)
_HEADER_CODE_SEG = (
    rf"{_HEADER_SECURITY_PREFIX}\s*代\s*码\s*[：:]\s*"
    r"[0-9A-Z][0-9A-Z.\-]{1,19}"
)
_HEADER_NAME_SEG = (
    rf"{_HEADER_SECURITY_PREFIX}\s*简\s*称\s*[：:]\s*"
    r"[^\s：:，。；,;]{1,24}"
)
_HEADER_KV_SEG = rf"(?:{_HEADER_CODE_SEG}|{_HEADER_NAME_SEG})"
# One line may carry several KV segments plus the announcement number
# ("证券代码：600519 证券简称：贵州茅台 公告编号：临 2026-027"): strip the KV
# segments, keep the announcement number (unique information).
HEADER_KV_COMBO_RE = re.compile(
    rf"^\s*(?:{_HEADER_KV_SEG}\s*){{1,4}}(?P<keep>公告编号\s*[：:].{{1,30}}?)?\s*$"
)
HEADER_CODE_VALUE_RE = re.compile(
    rf"{_HEADER_SECURITY_PREFIX}\s*代\s*码\s*[：:]\s*"
    r"(?P<value>[0-9A-Z][0-9A-Z.\-]{1,19})"
)
HEADER_NAME_VALUE_RE = re.compile(
    rf"{_HEADER_SECURITY_PREFIX}\s*简\s*称\s*[：:]\s*"
    r"(?P<value>[^\s：:，。；,;]{1,24})"
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


def parse_header_kv_line(
    line: str,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None] | None:
    """Parse a closed header line without deciding whether it is redundant."""

    match = HEADER_KV_COMBO_RE.fullmatch(line)
    if match is None:
        return None
    codes = tuple(item.group("value") for item in HEADER_CODE_VALUE_RE.finditer(line))
    names = tuple(item.group("value") for item in HEADER_NAME_VALUE_RE.finditer(line))
    keep = match.group("keep")
    return codes, names, keep.strip() if keep else None


@dataclass(frozen=True)
class RegisteredHeaderMatch:
    replacement: str
    metadata_value_count: int


def match_registered_security_header(
    line: str,
    *,
    security_code: str | None,
    security_name: str | None,
    document_title: str | None,
) -> RegisteredHeaderMatch | None:
    """Match only header values proven equal to registered document facts."""

    parsed = parse_header_kv_line(line)
    if parsed is None:
        return None
    line_codes, line_names, keep = parsed
    codes = {_compact_registered_value(security_code)}
    codes.discard("")
    names = {_compact_registered_value(security_name)}
    if document_title:
        title_prefix = re.split(r"[：:]", document_title, maxsplit=1)[0].strip()
        if title_prefix and title_prefix != document_title.strip():
            names.add(_compact_registered_value(title_prefix))
    names.discard("")
    code_match = not line_codes or (
        bool(codes)
        and all(_compact_registered_value(value) in codes for value in line_codes)
    )
    name_match = not line_names or (
        bool(names)
        and all(_compact_registered_value(value) in names for value in line_names)
    )
    if not code_match or not name_match:
        return None
    return RegisteredHeaderMatch(
        replacement=keep or "",
        metadata_value_count=len(line_codes) + len(line_names),
    )


def _compact_registered_value(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def is_exact_page_number_metadata(
    text: str,
    *,
    raw_kind: str | None,
    page_no: int | None,
) -> bool:
    """Return true for a closed, parser-labelled printed page-number block.

    ``page_no`` is the physical PDF page while the printed number may exclude
    covers or front matter.  Equality between them is therefore not evidence;
    the bounded MinerU block type plus a full-line page-number grammar is.
    """

    if raw_kind != "page_number" or page_no is None or page_no < 1:
        return False
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if re.fullmatch(r"(?:第)?\d{1,5}(?:页)?", compact):
        return True
    match = re.fullmatch(
        r"(?:第)?(?P<current>\d{1,5})(?:页)?[/／](?:共)?(?P<total>\d{1,5})(?:页)?",
        compact,
    )
    if match is None:
        return False
    current = int(match.group("current"))
    total = int(match.group("total"))
    return total >= current


# Standalone table-unit declarations ("单位：元" on its own line). S5 already
# extracts the value into table payload `unit` from the element stream, so a
# text unit carrying only this line is pure duplication (audited: 194 of 908
# units in a real annual report).
# Generalized to a pattern FAMILY (round11, user directive: 泛化能力): unit /
# currency declarations vary freely across filing formats but reduce to the
# same metadata duplication — table units carry the unit value themselves.
# Family members are full-line anchored with bounded free spans (red line:
# a sentence carrying real content must never match). Residual new variants
# surface via the tiny-orphan sweep (independent-review-guide §2), get added
# here, and bump RULES_VERSION.
# Compositional axes (StudyOnCompany-style token grammar; round11 research):
# lead-in(单位|金额单位|币种|货币单位|计量单位) x copula(：|为|均为|均以|指) x
# hedge(除特别注明外|如无特殊说明|除另有指明外) x currency x magnitude
# (元|千元|万元|百万元|亿元) x verb(列示|表示|为单位), optional （…） wrapping,
# and multi-declaration lines (单位：元 币种：人民币). New variants are
# DISCOVERED by the offline corpus-frequency audit
# (scripts/audit_boilerplate_candidates.py), promoted here, and bump
# RULES_VERSION — slice time stays deterministic.
_DECL_LEAD = r"(?:货\s*币|金\s*额|计\s*量)?\s*单\s*位|币\s*种"
_DECL_COPULA = r"(?:均)?\s*(?:[为是指]|以)?\s*[：:]?\s*"
_DECL_CURRENCY = r"(?:人民币|美元|港[币元]|欧元|日元|英镑)"
_DECL_MAGNITUDE = r"(?:元|千元|万元|百万元|亿元)"
_DECL_VALUE = rf"(?:{_DECL_CURRENCY}(?:\s*{_DECL_MAGNITUDE})?|{_DECL_MAGNITUDE})"
_DECL_ONE = rf"(?:{_DECL_LEAD}){_DECL_COPULA}{_DECL_VALUE}"
_DECL_HEDGE = r"(?:除特别(?:注明|说明)外|除另有(?:指明|说明)外|(?:如|若)无特(?:殊|别)(?:说明|注明))"
UNIT_DECLARATION_RES: tuple[re.Pattern[str], ...] = (
    # [（]?[本表]单位/币种声明（可多段：单位：元 币种：人民币）[）]?
    # No arbitrary prefix is accepted: ``营业收入单位：万元`` is a business
    # label, not disposable table metadata.
    re.compile(
        rf"^\s*[（(]?\s*(?:本\s*表\s*)?(?:{_DECL_ONE})"
        rf"(?:\s+{_DECL_ONE}|\s*币\s*种{_DECL_COPULA}\S{{1,8}}|\s*审计类型\s*[：:]\s*\S{{1,8}})*"
        r"\s*[）)]?\s*[。.]?\s*$"
    ),
    # [（]?除特别注明外，……以人民币[百万]元列示/为单位/计价[）]?
    re.compile(
        rf"^\s*[（(]?\s*{_DECL_HEDGE}[，,]?[^，。；]{{0,24}}?"
        rf"(?:人民币|美元|港[币元]|欧元)[^，。；]{{0,10}}?"
        r"(?:列示|表示|为单位|计价)?\s*[）)]?\s*[。.]?\s*$"
    ),
    # 本报告中如无特殊说明，货币单位均为人民币元。
    re.compile(
        rf"^\s*本[^，。；]{{0,10}}{_DECL_HEDGE}[，,]?"
        r"[^，。；]{0,14}(?:单位|币种|金额)[^，。；]{0,14}[。.]?\s*$"
    ),
)
UNIT_DECLARATION_RE = UNIT_DECLARATION_RES[0]
# Value extraction for a line already proven to be a unit declaration: capture
# the currency/magnitude token so the builder never re-encodes this vocabulary.
UNIT_DECLARATION_VALUE_RE = re.compile(
    r"(?:货币|金额|计量)?\s*单位\s*(?:均)?(?:为|是|指|以)?\s*[：:]?\s*"
    rf"({_DECL_CURRENCY}?\s*{_DECL_MAGNITUDE})"
)


def is_unit_declaration_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(pattern.fullmatch(stripped) for pattern in UNIT_DECLARATION_RES)


def classify_marker_line(line: str) -> str | None:
    """Classify one line as an applicability declaration (end-anchored)."""

    if APPLICABLE_MARK_RE.search(line):
        return "applicable"
    if NOT_APPLICABLE_MARK_RE.search(line):
        return "not_applicable"
    return None


def split_trailing_applicability_marker(text: str) -> tuple[str, str | None]:
    """Split a declaration glued to a structural label, if present."""

    for pattern in (APPLICABLE_MARK_RE, NOT_APPLICABLE_MARK_RE):
        match = pattern.search(text)
        if match is None or match.start() == 0:
            continue
        title = text[: match.start()].rstrip()
        if title:
            return title, text[match.start() :].strip()
    return text, None


def is_pure_marker_line(line: str) -> bool:
    """True when the whole line is nothing but the declaration itself."""

    stripped = line.strip()
    if not stripped:
        return False
    match = APPLICABLE_MARK_RE.search(stripped) or NOT_APPLICABLE_MARK_RE.search(
        stripped
    )
    return match is not None and match.start() == 0


# Yes/no checkbox answers ("是 □否", "□是 √否"). They are disclosure answers,
# not applicability declarations: the source line stays in text/caption, but
# it must never become a heading or public unit title.
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


# 附件 caption 是正文结构的兄弟节点(round17 语料: 11 个错挂实例, 全部
# 投关记录表): 命中即在标题树里开新顶层分支, 后续延续表随之归属。仅在
# qa_heading_mode 生效——非表单文档的附件可能出现在文中, 栈重置会把其后
# 的正文标题错挂进附件分支(复审 Major#1)。必须带冒号——「附件清单（如
# 有）」是表单字段名, 不是附件标签。
ATTACHMENT_CAPTION_RE = re.compile(r"^附件\s*[0-9一二三四五六七八九十]*\s*[：:]")
# 投关记录表单尾字段(深/沪官方模板固定词表): 整格精确匹配,
# 整表非空首列全部命中才判定为表单残段, 归属文档本身而非最后一个叙事
# 小节。前缀匹配会误伤「日期安排」类业务标签(复审 Major#2)。
# Native-text recovery for official IR/briefing forms.  The PDF often uses one
# outer table cell for the whole narrative, so layout parsers preserve the
# physical table while losing cross-page business structure.  Recovery is
# gated by a consecutive Chinese-numbered section run and stops before the
# official footer/attachment boundary.
QA_FORM_NARRATIVE_LABEL_RE = re.compile(
    r"^\s*投资者关系活动(?:主要内容介绍|内容介绍)\s*$"
)
# Official IR-form narrative labels that prove a table cell is carrying a
# transcript rather than an ordinary business grid.  This narrower cue lets
# the builder fail closed on a short, truncated first-page carrier without
# lowering the general 500-character shredded-table threshold.
NOISE_SEPARATOR_RE = re.compile(r"^[\s\-—―=_·•\*~～]{3,}$")

# Table/figure footnote lines ("[注1] 该金额系…", "注：…"). They are footnotes
# of the preceding table, never section headings (observed promoted to a
# root-level unit title on the real audit report, Codex round5).
FOOTNOTE_LINE_RE = re.compile(r"^\s*(?:[\[［]\s*注\s*\d*\s*[\]］]|注\s*[：:])")

# Canonical CN filing hierarchy: 节/章 > 一、 > （一） > 1、 > 1. > （1） > ①.
# Both full-width and half-width parens per level: MinerU flattens audit-note
# headings to heading_level=2, so an unmatched "(1) 明细情况" used to enter at
# level 2 and evict its 科目 parent from the stack (Codex round4 P1#2).
# Digit-paren (（1）/1）) forms were previously unmapped, so MinerU's own
# heading_level placed them right under the 节 and they swallowed sibling
# 1、-level topics as children (round3 P1#11 drift; observed merging 研发投入
# into （8）客户/供应商 on the real 江海 annual). Levels beyond the depth-4
# breadcrumb preserves the complete source hierarchy; occurrence identity is
# maintained separately inside the builder so repeated textual paths never
# become one section by accident.
# cn_a_v6 (round14): 顿号-numbered (17、存货) and dot/space-numbered
# (1. 存货的分类) arabic headings are DIFFERENT levels — CSRC annual-report
# notes use 、 for 科目 headings and . for sub-items within one note. One
# shared level made every first sub-item evict its 科目 parent from the
# stack, so 存货/长期股权投资-class intermediates vanished from children's
# heading_path (observed corpus-wide on the 江海 annual). The dot class
# carries (?!\d) so decimal amounts (1.5亿元…) never read as headings.
HEADING_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"^第[一二三四五六七八九十百]+[节章]")),
    (2, re.compile(r"^[一二三四五六七八九十]+、")),
    (3, re.compile(r"^（[一二三四五六七八九十]+）|^\([一二三四五六七八九十]+\)")),
    (4, re.compile(r"^\d{1,3}、")),
    (5, re.compile(r"^\d{1,3}(?:[.．](?!\d)|\s)")),
    (6, re.compile(r"^（\d{1,3}）|^\(\d{1,3}\)|^\d{1,3}[)）]")),
    (7, re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]")),
)
# Financial statements and international-report sections commonly use a
# decimal outline (3.1 -> 3.1.1) followed by Latin/Roman subclauses.  MinerU
# often flattens every one of these headings to source level 1, so their token
# depth is necessary structural evidence.  Requiring a separator after the
# numeric chain keeps ordinary decimal amounts out even if upstream styles are
# noisy.
DOTTED_CHAIN_HEADING_RE = re.compile(
    r"^(?P<token>\d{1,3}(?:[.．]\d{1,3})+)(?=\s|[、:：])"
)
DOT_NUMBER_HEADING_RE = re.compile(
    r"^(?P<token>\d{1,3})[.．](?!\d)"
)
PAREN_ALPHA_HEADING_RE = re.compile(
    r"^[（(](?P<token>[a-z]{1,7})[）)](?=\s|\S)", re.IGNORECASE
)
# Structural vocabularies may label or rank units, but they never authorize
# destructive section deletion. Even a 释义/备查文件 branch can carry unique
# evidence needed by downstream retrieval.
FIXED_L1_TITLES = {"重要提示", "释义", "目录", "备查文件", "备查文件目录"}
QUESTION_START_RE = re.compile(
    r"^\s*(问题|问|Q\d*|投资者提问|提问)\s*\d*\s*[：:]"
    r"|^\s*\d+[、.．]\s*.{2,}[？?]\s*$"
)
# A real, controlled fallback concept for evidence that has no narrower
# section/event match.  This is intentionally not ``unknown``: the unit is
# known to be retrievable document content, while its narrower topic remains
# unspecified.  New builder output therefore has one semantic state instead
# of a scalar NULL plus an empty array state.
SEMANTIC_FALLBACK_KEY = "document_content"


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
    # Structural section keys describe the unit's OWN slot, so they match only
    # the title/leaf heading — full-path matching would leak a combined
    # section title ("重要提示、目录和释义") onto every descendant and demote
    # narrower business scalars (observed on the phase00 excerpt fixture).
    leaf_only: bool = False


SEMANTIC_KEY_RULES: tuple[SemanticKeyRule, ...] = (
    SemanticKeyRule(
        "receivable_aging", required=("应收账款",), any_required=("账龄", "坏账")
    ),
    SemanticKeyRule(
        "inventory_breakdown", required=("存货",), any_required=("分类", "构成", "跌价")
    ),
    SemanticKeyRule(
        "goodwill_impairment",
        required=("商誉",),
        any_required=("减值", "测试", "损失"),
    ),
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
    SemanticKeyRule(
        "rd_investment", required=("研发",), any_required=("投入", "费用", "人员")
    ),
    SemanticKeyRule(
        "customer_concentration",
        required=(),
        any_required=(
            "前五名客户",
            "前5名客户",
            "主要客户",
            "前五名供应商",
            "前5名供应商",
            "主要供应商",
        ),
    ),
    SemanticKeyRule(
        "cash_flow",
        required=(),
        any_required=(
            "现金流量表",
            "现金流量净额",
            "现金流量情况",
            "现金流量分析",
            "经营活动产生的现金流量",
            "投资活动产生的现金流量",
            "筹资活动产生的现金流量",
            "经营性现金流",
        ),
    ),
    SemanticKeyRule(
        "debt_financing",
        required=(),
        any_required=("短期借款", "长期借款", "应付债券", "有息负债", "银行授信"),
    ),
    SemanticKeyRule(
        "capex_projects",
        required=(),
        any_required=(
            "在建工程",
            "募投项目",
            "募集资金投资项目",
            "投资进展",
            "产能建设",
        ),
    ),
    SemanticKeyRule(
        "dividend",
        required=(),
        any_required=("利润分配", "权益分派", "现金分红", "分红"),
        filing_type_limited=False,
    ),
    SemanticKeyRule(
        "share_buyback",
        required=("回购",),
        any_required=("股份", "股票"),
        filing_type_limited=False,
    ),
    SemanticKeyRule(
        "equity_incentive",
        required=(),
        any_required=("股权激励", "限制性股票", "员工持股"),
        filing_type_limited=False,
    ),
    SemanticKeyRule(
        "meeting_resolution",
        required=("议案",),
        any_required=("审议", "表决"),
        filing_type_limited=False,
    ),
    SemanticKeyRule(
        "shareholding_change",
        required=(),
        any_required=("增持", "减持"),
        filing_type_limited=False,
    ),
    SemanticKeyRule(
        "litigation",
        required=(),
        any_required=("诉讼", "仲裁"),
        filing_type_limited=False,
    ),
    SemanticKeyRule(
        "accounting_policy",
        required=("会计政策",),
        any_required=("变更", "估计", "重要"),
    ),
    SemanticKeyRule(
        "risk_factors",
        required=("风险",),
        any_required=("提示", "因素", "应对"),
    ),
    SemanticKeyRule(
        "segment_performance",
        required=(),
        any_required=("分部报告", "分部信息", "经营分部"),
    ),
    SemanticKeyRule(
        "impairment",
        required=("减值",),
        any_required=("准备", "测试", "损失"),
    ),
    # 公告节键：短语逐字取自交易所公告格式指引与股权激励管理办法（数据与
    # 决策记录见 retrieval 设计 §6.3/§6.4）。全部 leaf_only、不限文类；
    # 置于粗粒度业务键之后，定期报告 scalar 不回退。
    SemanticKeyRule(
        "guarantee_overview",
        any_required=("担保情况概述", "担保事项概述"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "guarantee_progress",
        required=("担保进展",),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "guaranteed_party_profile",
        required=("被担保人基本情况",),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "guarantee_agreement_terms",
        required=("担保协议", "主要内容"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "cumulative_external_guarantees",
        required=("累计对外担保",),
        filing_type_limited=False,
        leaf_only=True,
    ),
    # 考核/解禁短语并非股权激励独占（年报薪酬节、限售股解禁公告同词），
    # 只收管理办法第九条独占的「层面…考核」与「限售期和解除限售」形态。
    SemanticKeyRule(
        "incentive_performance_assessment",
        any_required=(
            "层面业绩考核",
            "层面的业绩考核",
            "层面绩效考核",
            "层面的绩效考核",
        ),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "incentive_vesting_exercise",
        any_required=("行权安排", "归属安排", "限售期和解除限售"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "incentive_plan_overview",
        any_required=("激励计划简介", "激励计划的目的", "激励计划概述"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "incentive_recipients",
        required=("激励对象名单",),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "incentive_condition_satisfaction",
        any_required=("满足行权条件", "满足归属条件", "符合行权条件", "满足解除限售条件"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    # 关联交易家族与跨公告通用节（交易类第 9 号）；定价/协议条款在重组类
    # 公告同为诚实交易节，故取通用键名。
    SemanticKeyRule(
        "related_party_overview",
        required=("关联交易概述",),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "related_party_profile",
        any_required=("关联方基本情况", "关联人基本情况"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "transaction_pricing_basis",
        any_required=("定价政策", "定价依据"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "transaction_agreement_terms",
        any_required=("关联交易协议", "交易协议的主要内容"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "cumulative_related_party_transactions",
        required=("累计已发生", "关联"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "decision_procedures",
        any_required=("决策程序", "审批程序"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "intermediary_opinion",
        any_required=("独立财务顾问", "法律意见", "核查意见"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    # 法定章节键（证监会年报格式准则第 2 号固定章节 + 交易所公告格式指引的
    # 通用"提示"节；数据见 retrieval 设计 §6.3）。
    # 三条均 leaf_only：章节键只描述单元自身所在部位，不沿路径污染子孙；置于元组
    # 末尾使更窄的业务概念优先成为 scalar。important_notice 不限文类。
    SemanticKeyRule(
        "important_notice",
        required=(),
        any_required=("重要提示", "重要内容提示", "特别提示"),
        filing_type_limited=False,
        leaf_only=True,
    ),
    # 公告类同样有备查文件/释义节，故不限文类。
    SemanticKeyRule(
        "reference_documents",
        required=("备查文件",),
        filing_type_limited=False,
        leaf_only=True,
    ),
    SemanticKeyRule(
        "definitions",
        required=("释义",),
        filing_type_limited=False,
        leaf_only=True,
    ),
    # 目录页是脉络基线。置于 reference_documents 之后：「备查文件目录」的
    # scalar 仍归 reference_documents，本键只入数组。
    SemanticKeyRule(
        "table_of_contents",
        required=("目录",),
        filing_type_limited=False,
        leaf_only=True,
    ),
)


# 附注科目受控词表（design/retrieval-and-semantic-keys.md §4）：标题剥编号后按
# 精确名 → 别名 → 包含式（最长名优先）三级匹配。词表是法定封闭集（编报规则
# 第15号 2023 修订），note_key_map.json 独立版本化。
_CN_ORDINAL = r"[一二三四五六七八九十百]+"
_NOTE_TITLE_NUMBERING_RE = re.compile(
    rf"^\s*(?:"
    rf"第{_CN_ORDINAL}[节章]\s*|"
    rf"[（(](?:\d{{1,3}}|{_CN_ORDINAL})[）)]\s*|"
    rf"{_CN_ORDINAL}\s*、\s*|"
    rf"\d{{1,3}}(?:[.．]\d{{1,3}})+\s+|"
    rf"\d{{1,3}}\s*(?:、|[.．](?!\d)|[)）])\s*|"
    rf"\d{{1,3}}\s+(?=\S)"
    rf")"
)
_NOTE_TITLE_CONTINUATION_RE = re.compile(r"\s*(?:[（(]\s*续\s*[）)]|[-—–－]\s*续)\s*$")
_SHORT_NOTE_SUFFIX_RE = re.compile(
    r"^(?:的)?(?:(?:分类|构成|明细|情况|减值|跌价|变动|账面价值|余额|"
    r"账龄|披露|说明|分析|计量|确认|核算|列示|准备|组合|方法|政策|表|一览)){1,4}$"
)

# Short structural labels are unsafe under the normal longest-substring
# fallback.  In particular, ``财务报表`` inside ``注册会计师对财务报表审计的责任``
# is not the statutory statement parent and must not become a semantic key or
# a heading-tree anchor.
EXACT_ONLY_NOTE_KEYS = frozenset({"financial_statements_section"})
NOTE_KEY_MAP_VERSION = "2026-07-r18"
NOTE_KEY_MAP_KEY_COUNT = 173
NOTE_KEY_MAP_LABEL_COUNT = 391
@lru_cache(maxsize=1)
def _note_key_tables() -> tuple[
    dict[str, tuple[str, ...]], tuple[tuple[str, tuple[str, ...]], ...]
]:
    payload = json.loads(
        resources.files("disclosure_anchor.adapters.unit_builder")
        .joinpath("note_key_map.json")
        .read_text(encoding="utf-8")
    )
    if payload.get("version") != NOTE_KEY_MAP_VERSION:
        raise ValueError(
            "note_key_map.json version does not match the rule bundle: "
            f"{payload.get('version')!r} != {NOTE_KEY_MAP_VERSION!r}"
        )
    entries = payload.get("keys")
    if not isinstance(entries, dict) or len(entries) != NOTE_KEY_MAP_KEY_COUNT:
        raise ValueError(
            "note_key_map.json key count does not match the rule bundle: "
            f"{len(entries) if isinstance(entries, dict) else 'invalid'} "
            f"!= {NOTE_KEY_MAP_KEY_COUNT}"
        )
    exact = _unique_note_label_table(entries)
    if len(exact) != NOTE_KEY_MAP_LABEL_COUNT:
        raise ValueError(
            "note_key_map.json unique label count does not match the rule bundle: "
            f"{len(exact)} != {NOTE_KEY_MAP_LABEL_COUNT}"
        )
    by_length = tuple(
        sorted(exact.items(), key=lambda item: len(item[0]), reverse=True)
    )
    return exact, by_length


def _unique_note_label_table(
    entries: dict[str, dict[str, list[str]]],
) -> dict[str, tuple[str, ...]]:
    """Build an ordered label table, allowing intentional compound facets."""

    labels: dict[str, list[str]] = {}
    for key, entry in entries.items():
        for name in [*entry["names"], *entry.get("aliases", [])]:
            keys = labels.setdefault(name, [])
            if key not in keys:
                keys.append(key)
    return {name: tuple(keys) for name, keys in labels.items()}


def _note_title_core(title: str) -> str:
    core = _NOTE_TITLE_NUMBERING_RE.sub("", title).strip().rstrip("：: ")
    core = _NOTE_TITLE_CONTINUATION_RE.sub("", core).strip().rstrip("：: ")
    return core


def note_keys_for_title(title: str | None) -> tuple[str, ...]:
    """Map a note title to every ordered, controlled retrieval facet."""

    if not title:
        return ()
    core = _note_title_core(title)
    if not core:
        return ()
    exact, by_length = _note_key_tables()
    hit = exact.get(core)
    if hit is not None:
        return hit
    for name, keys in by_length:
        if any(key in EXACT_ONLY_NOTE_KEYS for key in keys):
            continue
        # Accounting labels are common phrases in unrelated business prose.
        # A non-exact match is accepted only when the remaining title is a
        # complete structural disclosure qualifier, independent of label
        # length; arbitrary business prose never becomes a note key.
        qualifier = core[len(name) :] if core.startswith(name) else ""
        if qualifier and _SHORT_NOTE_SUFFIX_RE.fullmatch(qualifier):
            return keys
    return ()


def note_key_for_title(title: str | None) -> str | None:
    """Return the primary key for scalar compatibility."""

    keys = note_keys_for_title(title)
    return keys[0] if keys else None
