"""Versioned unit-builder rules for CN A-share disclosures."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
import re
from dataclasses import dataclass


RULES_VERSION = "ub-2026.07-52"
# Compatibility guard for parser-side aggregate-table proofs. Bump only when
# S5 table re-merge semantics or the structural-furniture veto used by the
# reconciler changes; ordinary heading/QA/semantic rules must not force a
# costly MinerU reparse.
TABLE_BUILDER_SEMANTICS_VERSION = "table-builder-semantics.v2"
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
# 值允许至多四个内部空格段（PDF 抽取会把「万科 A、万科 H 代」
# 和「22 万科 MTN004」拆开）；标签也覆盖交易所封面的双前缀
# 「A 股证券代码」。整行/冒号锚定与每段长度上限共同防止吞正文。
_HEADER_KV_SEG = (
    r"(?:[ABH]\s*股(?:\s*(?:证\s*券|股\s*票))?|证\s*券|股\s*票|"
    r"债\s*券|优\s*先\s*股)\s*(?:代\s*码|简\s*称)\s*[：:]\s*"
    r"\S{1,24}(?:[ \t]+[^\s：:]{1,8}){0,4}"
)
HEADER_KV_LINE_RE = re.compile(rf"^\s*{_HEADER_KV_SEG}\s*$")
# One line may carry several KV segments plus the announcement number
# ("证券代码：600519 证券简称：贵州茅台 公告编号：临 2026-027"): strip the KV
# segments, keep the announcement number (unique information).
HEADER_KV_COMBO_RE = re.compile(
    rf"^\s*(?:{_HEADER_KV_SEG}\s*){{1,4}}(?P<keep>公告编号\s*[：:].{{1,30}}?)?\s*$"
)
ANNOUNCEMENT_NUMBER_LINE_RE = re.compile(
    r"^\s*公告编号\s*[：:]\s*[^\s，。；]{1,20}"
    r"(?:[ \t][^\s，。；]{1,12})?\s*$"
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
_DECL_CURRENCY = r"(?:人民币|美元|港[币元]|欧元|日元|英镑)?"
_DECL_MAGNITUDE = r"(?:千?百?万?亿?元|千元|百万元|亿元)?"
_DECL_VALUE = rf"{_DECL_CURRENCY}\s*{_DECL_MAGNITUDE}"
_DECL_ONE = rf"(?:{_DECL_LEAD}){_DECL_COPULA}{_DECL_VALUE}"
_DECL_HEDGE = r"(?:除特别(?:注明|说明)外|除另有(?:指明|说明)外|(?:如|若)无特(?:殊|别)(?:说明|注明))"
UNIT_DECLARATION_RES: tuple[re.Pattern[str], ...] = (
    # [（]?前缀? 单位/币种声明（可多段：单位：元 币种：人民币 审计类型：未经审计）[）]?
    re.compile(
        rf"^\s*[（(]?\s*[^，。；,;]{{0,14}}?(?:{_DECL_ONE})"
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


def is_unit_declaration_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(pattern.fullmatch(stripped) for pattern in UNIT_DECLARATION_RES)


# Standalone-noise units (round10 class guard): a text unit whose ENTIRE
# content is a bare colon-terminated label ("其他说明：") or a lone year
# fragment ("2025 年度") carries no retrievable fact — drop counted. These
# patterns apply only to whole-unit text, never to lines inside larger units.
STANDALONE_NOISE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\S{1,8}[：:]$"),
    re.compile(r"^(?:19|20)\d{2}\s*年度?$"),
    # 结尾套话（round11 发现环第一例：9 docs/3 companies）
    re.compile(r"^特此公告[。.！!]?$"),
)


# 发现环第二轮晋级（round11）：公司名参数化的眉头/称呼/落款行。
_COMPANY_BOILERPLATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^编制单位\s*[:：]\s*\S{2,30}$"),  # 编制单位:XX股份有限公司
    re.compile(r"^\S{2,30}全体股东\s*[:：]\s*$"),  # 审计报告称呼行
    re.compile(r"^\S{2,28}公司(?:董事会|监事会)$"),  # 落款行
)


def is_closing_formula_line(line: str) -> bool:
    """套话行：结尾敬语 + 公司名参数化眉头/称呼/落款（发现环晋级）。"""

    stripped = line.strip()
    if re.fullmatch(r"特此公告[。.！!]?", stripped):
        return True
    return any(pattern.fullmatch(stripped) for pattern in _COMPANY_BOILERPLATE_RES)


def is_standalone_noise(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return any(pattern.fullmatch(stripped) for pattern in STANDALONE_NOISE_RES)


def classify_marker_line(line: str) -> str | None:
    """Classify one line as an applicability declaration (end-anchored)."""

    if APPLICABLE_MARK_RE.search(line):
        return "applicable"
    if NOT_APPLICABLE_MARK_RE.search(line):
        return "not_applicable"
    return None


def split_trailing_applicability_marker(text: str) -> tuple[str, str | None]:
    """Split a marker glued to a structural label from the label itself.

    MinerU sometimes returns ``标题 √适用 □不适用`` as one heading. The
    heading tree needs the clean title, while the declaration stage needs the
    marker as its own line so it can preserve the applicability flag. Pure
    marker lines are deliberately left unchanged.
    """

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
# Two real proposal-line styles (Codex round7, 平安/招商 board & shareholder
# resolutions): "N.议案名称：…" and "一、审议通过了《…议案》".
PROPOSAL_ANCHOR_RE = re.compile(r"^\s*\d{1,3}\s*[.、．]?\s*议案名称\s*[：:]")
PROPOSAL_APPROVAL_RE = re.compile(
    r"^\s*[一二三四五六七八九十]+、\s*(?:会议)?审议(?:并)?通过了?\s*《"
)


def match_proposal_anchor(line: str) -> bool:
    return bool(PROPOSAL_ANCHOR_RE.match(line) or PROPOSAL_APPROVAL_RE.match(line))


# QA-mode filings (投资者关系/业绩说明会) whose Q&A transcript got detected as
# a TABLE by MinerU arrive with sentences shredded across cells (observed on
# the real 美的 记录表, Codex round7). Deterministic recovery is impossible at
# build time — such units are flagged needs_review instead of ok so L2 never
# consumes the soup silently (§3.5: mark quality, keep raw, reprocessable).
QA_TABLE_CONTENT_MIN_CHARS = 500
QA_TABLE_MARKER_RE = re.compile(r"[？?]|答\s*[：:]")
SHORT_DOC_CONTENT_CHARS = 8000
COLLAPSIBLE_FILING_TYPES = frozenset(
    {"other", "performance_forecast", "performance_flash"}
)
DOCUMENT_HEADER_ANCHOR = "公告头信息"
# 附件 caption 是正文结构的兄弟节点(round17 语料: 11 个错挂实例, 全部
# 投关记录表): 命中即在标题树里开新顶层分支, 后续延续表随之归属。仅在
# qa_heading_mode 生效——非表单文档的附件可能出现在文中, 栈重置会把其后
# 的正文标题错挂进附件分支(复审 Major#1)。必须带冒号——「附件清单（如
# 有）」是表单字段名, 不是附件标签。
ATTACHMENT_CAPTION_RE = re.compile(r"^附件\s*[0-9一二三四五六七八九十]*\s*[：:]")
# 投关记录表单尾字段(深/沪官方模板固定词表): 整格精确匹配,
# 整表非空首列全部命中才判定为表单残段, 归属文档本身而非最后一个叙事
# 小节。前缀匹配会误伤「日期安排」类业务标签(复审 Major#2)。
QA_FORM_FOOTER_FIELD_RE = re.compile(
    r"^(?:附件清单\s*[（(]\s*如\s*有\s*[）)]|附件清单|日期|"
    r"关于本次活动是否涉及|应披露重大信息的说明|"
    r"活动过程中所使用的演示文稿、提供的文档等附件"
    r"\s*[（(]\s*如\s*有\s*[,，]\s*可作为附件\s*[）)])$"
)
# Native-text recovery for official IR/briefing forms.  The PDF often uses one
# outer table cell for the whole narrative, so layout parsers preserve the
# physical table while losing cross-page business structure.  Recovery is
# gated by a consecutive Chinese-numbered section run and stops before the
# official footer/attachment boundary.
QA_FORM_MAIN_SECTION_RE = re.compile(
    r"^\s*([一二三四五六七八九十百]{1,3}、\s*[^\n]{2,60})\s*$"
)
QA_FORM_NARRATIVE_END_RE = re.compile(
    r"^\s*(?:附件清单\s*[（(]\s*如\s*有\s*[）)]|日期(?:\s|$)|"
    r"关于本次活动是否涉及(?:\s|$)|应披露重大信息的说明(?:\s|$)|"
    r"活动过程中所使用的演示文稿、提供的文档等附件(?:\s|$)|"
    r"附件\s*[0-9一二三四五六七八九十]*\s*[：:])"
)
QA_FORM_NARRATIVE_LABEL_RE = re.compile(
    r"^\s*(?:投资者关系活动(?:主|要内容介绍|主要内容介绍)|要内容介绍)\s*$"
)
QA_FORM_QA_SECTION_RE = re.compile(r"(?:交流问题|问答|提问)")
# Official IR-form narrative labels that prove a table cell is carrying a
# transcript rather than an ordinary business grid.  This narrower cue lets
# the builder fail closed on a short, truncated first-page carrier without
# lowering the general 500-character shredded-table threshold.
QA_FORM_TRANSCRIPT_CUE_RE = re.compile(
    r"(?:投资者提出的?问题(?:及公司回复)?|问题及公司回复情况|问答记录|"
    r"交流内容及具体问答记录)"
)

# A second official-form family prints the transcript directly as ``1. ...？``
# followed by an unlabelled company answer, or as ``Q1：... / 回复：...``.
# Native PDF text preserves this sequence when layout tables shred MinerU's
# columns/page joins.  These expressions are only used behind the strict
# official-form + filing-type gate in builder.py; they never relax generic QA
# parsing or table inference.
QA_DIRECT_EXPLICIT_QUESTION_RE = re.compile(
    r"^\s*Q\s*(?P<ordinal>\d{1,3})\s*[：:、.．]\s*(?P<question>.+?)\s*$",
    re.IGNORECASE,
)
QA_DIRECT_NUMBERED_QUESTION_RE = re.compile(
    r"^\s*(?P<ordinal>\d{1,3})[.．]\s*"
    r"(?P<question>(?:(?:19|20)\d{2}\s*年.+|(?!\d).+?))\s*$"
)
QA_DIRECT_ALT_NUMBERED_QUESTION_RE = re.compile(
    r"^\s*(?P<ordinal>\d{1,3})(?:[、]\s*|[：:]\s*(?!\d))"
    r"(?P<question>.+?)\s*$"
)
QA_DIRECT_FORM_END_RE = re.compile(
    r"^\s*(?:关于本次活动|活动过程中所使用|附件清单|日期(?:\s|$))"
)
QA_DIRECT_TRANSCRIPT_CUE_RE = re.compile(
    r"(?:回答(?:投资者)?提问|交流内容及具体问答|问题及公司回复|问答记录|问答环节)"
)

# Long structured documents group on structural/business boundaries.  These are
# hard safety caps for the resulting mixed unit, never targets used to search
# upward for a shallower ancestor.  The part cap bounds repeated same-title
# structures (for example one goodwill test heading repeated per asset group)
# even when their character total remains deceptively small.
SECTION_GROUP_MAX_CHARS = 8000
SECTION_GROUP_MAX_PARTS = 24

# Each paragraph starts a different acquired asset-group valuation, even though
# the statutory subheading immediately following it repeats verbatim for every
# group.  Treat this exact template as an instance boundary so those valuations
# never collapse into one giant goodwill unit.
GOODWILL_ASSET_GROUP_START_RE = re.compile(
    r"^\s*[（(]\s*\d{1,3}\s*[）)]\s*为商誉减值测试的目的"
)

NOISE_SEPARATOR_RE = re.compile(r"^[\s\-—―=_·•\*~～]{3,}$")
NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = ()

# Table/figure footnote lines ("[注1] 该金额系…", "注：…"). They are footnotes
# of the preceding table, never section headings (observed promoted to a
# root-level unit title on the real audit report, Codex round5).
FOOTNOTE_LINE_RE = re.compile(r"^\s*(?:[\[［]\s*注\s*\d*\s*[\]］]|注\s*[：:])")

# The fixed board-guarantee legalese (§3.5 稳定噪声, user-authorized drop
# 2026-07-06). Anchored and keyword-complete so any sentence carrying real
# content fails the match and is kept (red line).
# The subject clause is a bounded character-class soup: real filings permute
# it freely (本公司及董事会全体成员…, 公司董事会、监事会及董事、监事、高级管理
# 人员…) and an ordered alternation missed the 及-before-董事会 variant.
BOILERPLATE_GUARANTEE_RE = re.compile(
    r"^\s*本?[公司行董事监会高级管理人员全体成及和、\s]{0,30}"
    r"保证(?:本公告|本报告|年度报告|半年度报告|季度报告|报告)?(?:内容)?[^。]{0,40}?"
    r"(?:真实|虚假记载)[^。]{0,80}?(?:重大遗漏|连带责任|法律责任)[。.]?\s*$"
)

# Canonical CN filing hierarchy: 节/章 > 一、 > （一） > 1、 > 1. > （1） > ①.
# Both full-width and half-width parens per level: MinerU flattens audit-note
# headings to heading_level=2, so an unmatched "(1) 明细情况" used to enter at
# level 2 and evict its 科目 parent from the stack (Codex round4 P1#2).
# Digit-paren (（1）/1）) forms were previously unmapped, so MinerU's own
# heading_level placed them right under the 节 and they swallowed sibling
# 1、-level topics as children (round3 P1#11 drift; observed merging 研发投入
# into （8）客户/供应商 on the real 江海 annual). Levels beyond the depth-4
# heading_path cap retain their deepest leaf in ``title`` while the public
# breadcrumb remains bounded.
# cn_a_v6 (round14): 顿号-numbered (17、存货) and dot/space-numbered
# (1. 存货的分类) arabic headings are DIFFERENT levels — CSRC annual-report
# notes use 、 for 科目 headings and . for sub-items within one note. One
# shared level made every first sub-item evict its 科目 parent from the
# stack, so 存货/长期股权投资-class intermediates vanished from children's
# heading_path (observed corpus-wide on the 江海 annual). The dot class
# carries (?!\d) so decimal amounts (1.5亿元…) never read as headings.
HEADING_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (1, re.compile(r"^第[一二三四五六七八九十百]+[节章]")),
    (2, re.compile(r"^[一二三四五六七八九十]+(?:、|\s+)")),
    (3, re.compile(r"^（[一二三四五六七八九十]+）|^\([一二三四五六七八九十]+\)")),
    (4, re.compile(r"^\d{1,3}、")),
    (5, re.compile(r"^\d{1,3}(?:[.．](?!\d)|\s)")),
    (6, re.compile(r"^（\d{1,3}）|^\(\d{1,3}\)|^\d{1,3}[)）]")),
    (7, re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]")),
)
FIXED_L1_TITLES = {
    "重要提示",
    "重大风险提示",
    "释义",
    "目录",
    "备查文件",
    "备查文件目录",
    # Bank/H-share reports use unnumbered major-section headings followed by
    # decimal outlines (3.8 / 3.9.1). MinerU correctly marks these as h1; S2
    # must not leave the previous numbered outline open across the next root.
    "会计数据和财务指标摘要",
    "管理层讨论与分析",
    "环境、社会与治理(ESG)",
    "环境、社会与治理（ESG）",
    "公司治理",
    "重要事项",
    "股份变动及股东情况",
    "财务报告",
    "未经审计财务报表补充资料",
}
SKIP_SECTION_TITLES = {"释义", "目录", "备查文件", "备查文件目录"}
SUPPLEMENTAL_FINANCIAL_INFO_TITLE = "未经审计财务报表补充资料"
STRUCTURAL_PAGE_FURNITURE_TITLES = {SUPPLEMENTAL_FINANCIAL_INFO_TITLE}

# Some exchange-form Q&A PDFs use an explicit numbered MinerU heading for the
# question but end it with a full stop ("4. 请介绍……情况。") rather than a
# question mark. The leading interrogative/request cue is required so ordinary
# numbered agenda headings do not become questions merely because QA mode is on.
_NUMBERED_QA_REQUEST = (
    r"(?:请|能否|可否|烦请|贵公司(?:如何|是否)|公司(?:如何|是否)|"
    r".{0,80}(?:请问|如何|怎样|怎么样|什么|多少|是否|为何|为什么|能否|可否|吗|呢|"
    r"请管理层(?:参考|说明|回复|回答)))"
)
BRACKET_QUESTION_START_RE = re.compile(
    r"^\s*【\s*(?:提问|问题)\s*(?P<ordinal>\d{1,3})"
    r"[^】\n]{0,100}】\s*[：:]\s*(?P<question>.+?)\s*$"
)
BRACKET_SPEAKER_ANSWER_RE = re.compile(
    r"^\s*【\s*(?!(?:提问|问题)\b)[^】\n]{1,50}】\s*[：:]\s*"
)
EXPLICIT_QUESTION_START_RE = re.compile(
    r"^\s*【\s*(?:提问|问题)\s*\d{1,3}[^】\n]{0,100}】\s*[：:]"
    r"|^\s*(?:问题|问|投资者提问|提问)\s*"
    r"(?:\d+\s*[、.．]|\d*\s*[：:])"
    r"|^\s*Q\s*\d*\s*[：:]"
    r"|^\s*Q\s*\d+\s*[、.．：:]",
    re.IGNORECASE,
)
QUESTION_START_RE = re.compile(
    r"^\s*(?:问题|问|投资者提问|提问)\s*"
    r"(?:\d+\s*[、.．]|\d*\s*[：:])\s*.{1,240}$"
    r"|^\s*Q\d*\s*\d*\s*[：:]"
    r"|^\s*Q\s*\d+\s*[、.．]\s*.{1,240}$"
    r"|^\s*\d+[、.．：:]\s*.{2,}[？?]\s*$"
    r"|^\s*\d+[：:]\s*【.{2,2000}$"
    rf"|^\s*\d+[、.．]\s*{_NUMBERED_QA_REQUEST}.{{2,120}}[。；;]?\s*$"
)
ANSWER_START_RE = re.compile(r"^\s*(答|回复|公司回复|A\d*)\s*[：:]")
# One outer investor entry can explicitly ask management to answer several
# numbered subquestions together (``1、三个问题，请按题作答``).  The intro
# itself may end with an exclamation mark, so it needs a bounded, cue-complete
# gate instead of the ordinary question-mark grammar.
QA_COMPOUND_QUESTION_INTRO_RE = re.compile(
    r"^\s*\d+[、.．]\s*[^\n]{0,40}(?:问题|提问)[^\n]{0,40}"
    r"(?:按题|逐题|分别)[^\n]{0,20}(?:作答|回答|回复)[^\n]{0,20}$"
)
# Inside a proven QA carrier MinerU can concatenate the end of a question and
# its answer label (``希望答:`` / ``谢谢公司回复:``). Split only truly glued
# labels. The negative guards keep lexical uses such as 回答、问答、应答、作答、
# 解答 and requests such as 请回复 intact.
INLINE_ANSWER_BOUNDARY_RE = re.compile(
    r"(?<=[^\s])(?<![回问应作解抢])(?=答\s*[：:])"
    r"|(?<=[^\s])(?<!请)(?=公司回复\s*[：:])"
    r"|(?<=[^\s])(?<!请)(?<!公司)(?<!进行了)(?=回复\s*[：:])"
)
UNLABELLED_COMPANY_RESPONSE_START_RE = re.compile(
    r"^\s*(?:尊敬的投资者|投资者(?:您好|你好)|"
    r"(?:您好|你好)[,，!！]|感谢(?:您|投资者)|公司(?:表示|回复))"
)

# A real, controlled fallback concept for evidence that has no narrower
# section/event match.  This is intentionally not ``unknown``: the unit is
# known to be retrievable document content, while its narrower topic remains
# unspecified.  New builder output therefore has one semantic state instead
# of a scalar NULL plus an empty array state.
SEMANTIC_FALLBACK_KEY = "document_content"

# Periodic cover metadata is removable only under the builder's additional
# page/position/preceding-cover proof. This expression by itself is never a
# global noise rule: exact dates elsewhere are filing evidence.
PERIODIC_REPORT_FILING_TYPES = frozenset(
    {"annual_report", "semiannual_report", "quarterly_report"}
)
PERIODIC_COVER_DATE_ONLY_RE = re.compile(
    r"^\s*[【\[]?\s*20\d{2}\s*年\s*(?:0?[1-9]|1[0-2])\s*月\s*"
    r"(?:0?[1-9]|[12]\d|3[01])\s*日\s*[】\]]?\s*$"
)
PERIODIC_REPORT_TITLE_RE = re.compile(
    r"(?:年度报告|半年度报告|第?[一二三四1-4]季度报告)"
)
PERIODIC_COVER_REPORT_TITLE_LINE_RE = re.compile(
    r"^\s*20\d{2}\s*年?\s*"
    r"(?:年度报告|半年度报告|第?[一二三四1-4]季度报告)"
    r"\s*(?:摘要|全文)?\s*$"
)
PERIODIC_COVER_AUXILIARY_LINE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*[（(]?\s*股票代码\s*[：:]\s*[0-9A-Z.]{4,12}\s*[）)]?\s*$"),
    re.compile(
        r"^\s*[二〇○零一二三四五六七八九十]{4}年"
        r"[一二三四五六七八九十]{1,3}月"
        r"(?:[一二三四五六七八九十]{1,3}日)?\s*$"
    ),
    re.compile(
        r"^\s*20\d{2}\s*年\s*(?:0?[1-9]|1[0-2])\s*月"
        r"(?:\s*(?:0?[1-9]|[12]\d|3[01])\s*日)?\s*$"
    ),
)
PERIODIC_REPORT_BANNER_RE = re.compile(
    r"^\s*.{2,80}(?:股份有限公司|有限责任公司)\s*"
    r"20\d{2}\s*年?\s*"
    r"(?:年度报告|半年度报告|第?[一二三四1-4]季度报告)"
    r"\s*(?:摘要|全文)?\s*$"
)

SEMANTIC_LIMITED_FILING_TYPES = {
    "annual_report",
    "semiannual_report",
    "quarterly_report",
    "inquiry_reply",
}

# Unnumbered statutory statement titles are siblings within the 财务报表
# section. MinerU commonly reports all of them as heading_level=1; treating
# each as a child of the previous statement creates a false title chain.
FINANCIAL_STATEMENT_KEYS = frozenset(
    {
        "balance_sheet",
        "balance_sheet_parent",
        "income_statement",
        "income_statement_parent",
        "cash_flow_statement",
        "cash_flow_statement_parent",
        "equity_statement",
        "equity_statement_parent",
    }
)


@dataclass(frozen=True)
class SemanticKeyRule:
    semantic_key: str
    required: tuple[str, ...] = ()
    any_required: tuple[str, ...] = ()
    filing_type_limited: bool = True


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
)


# 附注科目受控词表（design/retrieval-and-semantic-keys.md §4）：标题剥编号后按
# 精确名 → 别名 → 包含式（最长名优先）三级匹配。词表是法定封闭集（编报规则
# 第15号 2023 修订），note_key_map.json 独立版本化。
_NOTE_TITLE_NUMBERING_RE = re.compile(
    r"^\s*(?:[（(]?(?:\d{1,3}|[一二三四五六七八九十百]+)[）)]?\s*[、.．)）]?)+\s*"
)
_NOTE_TITLE_CONTINUATION_RE = re.compile(r"\s*(?:[（(]\s*续\s*[）)]|[-—–－]\s*续)\s*$")

# Short structural labels are unsafe under the normal longest-substring
# fallback.  In particular, ``财务报表`` inside ``注册会计师对财务报表审计的责任``
# is not the statutory statement parent and must not become a semantic key or
# a heading-tree anchor.
EXACT_ONLY_NOTE_KEYS = frozenset({"financial_statements_section"})
NOTE_KEY_MAP_VERSION = "2026-07-r16"
NOTE_KEY_MAP_KEY_COUNT = 173
NOTE_KEY_MAP_LABEL_COUNT = 389
EVENT_KEY_MAP_VERSION = "2026-07-r2"
EVENT_KEY_MAP_EVENT_COUNT = 35
EVENT_KEY_MAP_PATTERN_COUNT = 109


@lru_cache(maxsize=1)
def _note_key_tables() -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
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
) -> dict[str, str]:
    """Build the exact-label table and reject unreachable duplicate labels."""

    exact: dict[str, str] = {}
    for key, entry in entries.items():
        for name in [*entry["names"], *entry.get("aliases", [])]:
            existing = exact.get(name)
            if existing is not None and existing != key:
                raise ValueError(
                    f"duplicate note label {name!r}: {existing!r} and {key!r}"
                )
            exact[name] = key
    return exact


def _note_title_core(title: str) -> str:
    core = _NOTE_TITLE_NUMBERING_RE.sub("", title).strip().rstrip("：: ")
    core = _NOTE_TITLE_CONTINUATION_RE.sub("", core).strip().rstrip("：: ")
    return core


_ACCOUNTING_POLICY_SECTION_CORE_RE = re.compile(
    r"^(?:公司)?(?:主要|重要|其他重要的)会计政策(?:及|和|、)会计估计$"
)


def is_accounting_policy_section_title(title: str | None) -> bool:
    """Recognize the policy chapter independently of the retrieval map.

    Section grouping must not change merely because a descendant label is
    added to ``note_key_map.json``. Numbering/continuation cleanup is shared
    with exact semantic lookup, but the structural family stays explicit.
    """

    return bool(
        title
        and _ACCOUNTING_POLICY_SECTION_CORE_RE.fullmatch(_note_title_core(title))
    )


def exact_note_key_for_title(title: str | None) -> str | None:
    """Map only an exact controlled label (after numbering/续 cleanup)."""

    if not title:
        return None
    core = _note_title_core(title)
    if not core:
        return None
    exact, _ = _note_key_tables()
    return exact.get(core)


def financial_statement_labels() -> tuple[tuple[str, str], ...]:
    """Return longest-first controlled labels for statutory statements.

    Structural parsing needs the same aliases as semantic retrieval, but it
    applies a much stricter prefix/suffix grammar in ``builder.py``. Exposing
    the filtered immutable table here keeps that grammar from duplicating the
    names in ``note_key_map.json``.
    """

    _, by_length = _note_key_tables()
    return tuple(
        (label, key) for label, key in by_length if key in FINANCIAL_STATEMENT_KEYS
    )


_STATEMENT_AUDIT_PREFIX_RE = re.compile(
    r"^\s*(?:[（(]\s*)?(?:未经审计|未经审核|已审计)(?:\s*[）)])?\s*"
)
_STATEMENT_PERIOD_FRAGMENT = (
    r"(?:截至\s*)?(?:19|20)\d{2}\s*年\s*"
    r"(?:上?半年度?|度|\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?|"
    r"第?\s*[一二三四1-4]\s*季度|1\s*[-—–－至]\s*\d{1,2}\s*月)?"
)
_STATEMENT_PERIOD_PREFIX_RE = re.compile(rf"^\s*{_STATEMENT_PERIOD_FRAGMENT}\s*")
_STATEMENT_PERIOD_SUFFIX_RE = re.compile(rf"\s*{_STATEMENT_PERIOD_FRAGMENT}\s*$")
_STATEMENT_ISSUER_PREFIX_RE = re.compile(
    r"^[\u3400-\u9fffA-Za-z0-9·（）()&－—\-\s]{2,64}"
    r"(?:股份有限|有限责任|有限|集团|银行|公司)$"
)
_STATEMENT_SAFE_SUFFIX_RE = re.compile(
    r"^\s*(?:(?:[（(]\s*续\s*[）)]|[-—–－]\s*续)\s*)?"
    r"(?:(?:[-—–－]\s*)?按(?:中国|国际)[^，。；;]{0,24}?准则编制\s*)?"
    r"(?:(?:截至\s*)?(?:19|20)\d{2}\s*年\s*"
    r"(?:\d{1,2}\s*月\s*\d{1,2}\s*日)?"
    r"(?:止\s*(?:六|十二|6|12)\s*个月期间)?\s*)?"
    r"(?:[（(]\s*除特别(?:注明|说明)外[^）)]{0,50}"
    r"(?:金额单位|单位)[^）)]{0,30}[）)])?\s*"
    r"(?:(?:[（(]\s*续\s*[）)]|[-—–－]\s*续)\s*)?$"
)


def _bounded_statement_prefix(prefix: str) -> bool:
    candidate = _STATEMENT_AUDIT_PREFIX_RE.sub("", prefix).strip()
    candidate = _STATEMENT_PERIOD_PREFIX_RE.sub("", candidate).strip()
    if not candidate:
        return True
    if _STATEMENT_ISSUER_PREFIX_RE.fullmatch(candidate):
        return True
    period = _STATEMENT_PERIOD_SUFFIX_RE.search(candidate)
    return bool(
        period is not None
        and _STATEMENT_ISSUER_PREFIX_RE.fullmatch(candidate[: period.start()].strip())
    )


def structural_statement_key(title: str | None) -> str | None:
    """Return a statutory-statement key only for one bounded full title.

    This parser-neutral predicate is shared by the unit builder and MinerU
    table reconciliation. Keeping a single grammar prevents page-local table
    restoration from overlooking a caption that the builder would recover
    geometrically and thereby changing table merge boundaries.
    """

    if not title:
        return None
    key = exact_note_key_for_title(title)
    if key in FINANCIAL_STATEMENT_KEYS:
        return key
    candidate = _STATEMENT_AUDIT_PREFIX_RE.sub("", title)
    candidate = _STATEMENT_PERIOD_PREFIX_RE.sub("", candidate)
    key = exact_note_key_for_title(candidate)
    if key in FINANCIAL_STATEMENT_KEYS:
        return key

    for label, label_key in financial_statement_labels():
        start = candidate.find(label)
        if start < 0 or candidate.find(label, start + 1) >= 0:
            continue
        prefix = candidate[:start].strip()
        suffix = candidate[start + len(label) :].strip()
        if _bounded_statement_prefix(prefix) and _STATEMENT_SAFE_SUFFIX_RE.fullmatch(
            suffix
        ):
            return label_key
    return None


_STRUCTURAL_PAGE_FURNITURE_RE = re.compile(
    r"^(?:(?:19|20)\d{2}年(?:半年度|年度|第[一二三四]季度)?)?"
    r"(?:未经审计)?财务报表(?:附注|补充资料)?$|"
    r"^(?:第[一二三四五六七八九十百\d]+节|"
    r"[一二三四五六七八九十百\d]+、)(?:财务报告|财务报表(?:附注)?)$|"
    r"^(?:财务报告|补充资料)$|"
    r"^(?:合并|母公司|公司)?财务报表"
    r"(?:项目注释|主要项目注释|重要项目附注)$"
)


def is_structural_page_furniture_title(title: str | None) -> bool:
    """Return whether running-furniture treatment could change structure."""

    if not title:
        return False
    compact = re.sub(r"\s+", "", title).rstrip("：:")
    return bool(
        structural_statement_key(title) is not None
        or compact
        in {
            re.sub(r"\s+", "", value).rstrip("：:")
            for value in STRUCTURAL_PAGE_FURNITURE_TITLES
        }
        or _STRUCTURAL_PAGE_FURNITURE_RE.fullmatch(compact)
    )


def note_key_for_title(title: str | None) -> str | None:
    """Map a note-section title to its canonical key, or None."""

    if not title:
        return None
    core = _note_title_core(title)
    if not core:
        return None
    exact, by_length = _note_key_tables()
    hit = exact.get(core)
    if hit is not None:
        return hit
    for name, key in by_length:
        if key in EXACT_ONLY_NOTE_KEYS:
            continue
        if name in core:
            return key
    return None


# 事件键（round12 调研：DuEE-fin/CCKS/FewFC/CFinDEE 并集，8-K item 式监管锚定）。
# 从公告标题派生，文档级语义 → 并入该文档全部单元的 semantic_keys。
@lru_cache(maxsize=1)
def _event_key_table() -> tuple[tuple[str, tuple[str, ...]], ...]:
    payload = json.loads(
        resources.files("disclosure_anchor.adapters.unit_builder")
        .joinpath("event_key_map.json")
        .read_text(encoding="utf-8")
    )
    if payload.get("version") != EVENT_KEY_MAP_VERSION:
        raise ValueError(
            "event_key_map.json version does not match the rule bundle: "
            f"{payload.get('version')!r} != {EVENT_KEY_MAP_VERSION!r}"
        )
    events = payload.get("events")
    if not isinstance(events, dict) or len(events) != EVENT_KEY_MAP_EVENT_COUNT:
        raise ValueError(
            "event_key_map.json event count does not match the rule bundle: "
            f"{len(events) if isinstance(events, dict) else 'invalid'} "
            f"!= {EVENT_KEY_MAP_EVENT_COUNT}"
        )
    table = tuple(
        (key, tuple(str(p) for p in patterns))
        for key, patterns in events.items()
    )
    pattern_count = sum(len(patterns) for _, patterns in table)
    if pattern_count != EVENT_KEY_MAP_PATTERN_COUNT:
        raise ValueError(
            "event_key_map.json pattern count does not match the rule bundle: "
            f"{pattern_count} != {EVENT_KEY_MAP_PATTERN_COUNT}"
        )
    return table


def event_keys_for_document_title(title: str | None) -> tuple[str, ...]:
    if not title:
        return ()
    return tuple(
        key
        for key, patterns in _event_key_table()
        if any(pattern in title for pattern in patterns)
    )
