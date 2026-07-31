"""Versioned unit-builder rules for CN A-share disclosures."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
import re
from dataclasses import dataclass
RULES_VERSION = "ub-2026.07-86"
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



def _section_rule(
    semantic_key: str,
    *,
    required: tuple[str, ...] = (),
    any_required: tuple[str, ...] = (),
) -> SemanticKeyRule:
    """A leaf-scoped section key available to every filing type.

    Section keys describe the unit's own slot only, so they never leak onto
    descendants and never displace the coarse periodic-report scalars that
    precede them in the tuple.
    """

    return SemanticKeyRule(
        semantic_key,
        required=required,
        any_required=any_required,
        filing_type_limited=False,
        leaf_only=True,
    )


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
    # 决策记录见 retrieval 设计 §6.3–§6.5）。置于粗粒度业务键之后，
    # 定期报告 scalar 不回退。
    _section_rule(
        "guarantee_overview", any_required=("担保情况概述", "担保事项概述")
    ),
    _section_rule("guarantee_progress", required=("担保进展",)),
    _section_rule("guaranteed_party_profile", required=("被担保人基本情况",)),
    _section_rule("guarantee_agreement_terms", required=("担保协议", "主要内容")),
    _section_rule("cumulative_external_guarantees", required=("累计对外担保",)),
    # 考核/解禁短语并非股权激励独占（年报薪酬节、限售股解禁公告同词），
    # 只收管理办法第九条独占的「层面…考核」与「限售期和解除限售」形态。
    _section_rule(
        "incentive_performance_assessment",
        any_required=(
            "层面业绩考核",
            "层面的业绩考核",
            "层面绩效考核",
            "层面的绩效考核",
        ),
    ),
    _section_rule(
        "incentive_vesting_exercise",
        any_required=("行权安排", "归属安排", "限售期和解除限售"),
    ),
    _section_rule(
        "incentive_plan_overview",
        any_required=("激励计划简介", "激励计划的目的", "激励计划概述"),
    ),
    _section_rule("incentive_recipients", required=("激励对象名单",)),
    _section_rule(
        "incentive_condition_satisfaction",
        any_required=(
            "满足行权条件",
            "满足归属条件",
            "符合行权条件",
            "满足解除限售条件",
        ),
    ),
    # 关联交易家族与跨公告通用节（交易类第 9 号）；定价/协议条款在重组类
    # 公告同为诚实交易节，故取通用键名。
    _section_rule("related_party_overview", required=("关联交易概述",)),
    _section_rule(
        "related_party_profile",
        any_required=("关联方基本情况", "关联人基本情况"),
    ),
    # 「定价政策」单独出现也见于 MD&A 产品定价叙述，只认交易语境形态。
    _section_rule(
        "transaction_pricing_basis",
        any_required=("定价政策及定价依据", "定价依据"),
    ),
    _section_rule(
        "transaction_agreement_terms",
        any_required=("关联交易协议", "交易协议的主要内容"),
    ),
    _section_rule(
        "cumulative_related_party_transactions", required=("累计已发生", "关联")
    ),
    _section_rule("decision_procedures", any_required=("决策程序", "审批程序")),
    _section_rule(
        "intermediary_opinion",
        any_required=("独立财务顾问", "法律意见", "核查意见"),
    ),
    # 募集资金家族（再融资类第 1/2/3 号模板节名；细键与 note_key_map 的
    # fundraising_usage 零短语重叠，定期报告 scalar 不受影响）。
    _section_rule("fundraising_overview", required=("募集资金基本情况",)),
    _section_rule(
        "fundraising_custody",
        any_required=(
            "募集资金存放",
            "募集资金专户",
            "募集资金存储",
            "三方监管协议",
            "四方监管协议",
        ),
    ),
    _section_rule(
        "fundraising_repurposing", any_required=("改变募集资金", "变更募集资金")
    ),
    _section_rule(
        "fundraising_replacement",
        any_required=("募集资金置换", "置换先期投入", "置换预先投入"),
    ),
    _section_rule("fundraising_use_plan", required=("募集资金的使用计划",)),
    _section_rule(
        "fundraising_project_status",
        any_required=(
            "募投项目基本情况",
            "募集资金投资项目基本情况",
            "结项",
            "节余募集资金",
        ),
    ),
    # 法定章节键（证监会年报格式准则第 2 号固定章节 + 交易所公告格式指引的
    # 通用"提示"节；数据见 retrieval 设计 §6.3）。章节键只描述单元自身所在
    # 部位，不沿路径污染子孙；置于元组末尾使更窄的业务概念优先成为 scalar。
    _section_rule(
        "important_notice",
        any_required=("重要提示", "重要内容提示", "特别提示"),
    ),
    # 公告类同样有备查文件/释义节，故不限文类。
    _section_rule("reference_documents", required=("备查文件",)),
    _section_rule("definitions", required=("释义",)),
    # 目录页是脉络基线。置于 reference_documents 之后：「备查文件目录」的
    # scalar 仍归 reference_documents，本键只入数组。
    _section_rule("table_of_contents", required=("目录",)),
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
# is not the statutory statement parent and must not become a semantic key.
EXACT_ONLY_NOTE_KEYS = frozenset({"financial_statements_section"})
NOTE_KEY_MAP_VERSION = "2026-07-r18"
NOTE_KEY_MAP_KEY_COUNT = 173
NOTE_KEY_MAP_LABEL_COUNT = 391
@lru_cache(maxsize=1)
def _note_key_tables() -> tuple[
    dict[str, tuple[str, ...]], tuple[tuple[str, tuple[str, ...]], ...]
]:
    payload = json.loads(
        resources.files("disclosure_anchor.application.services.unit_builder")
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
