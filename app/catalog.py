"""画像标签、数据指标、关注点规则的目录。

数据页显示哪些指标、监控哪些关注点，都从这里的目录里选：AI 根据画像推荐一组，用户可以自己增减。
新增指标或规则只需在这里加一项（关注点规则还要在 portfolio.concerns 里写判定）。
"""

TAG_GROUPS = [
    ("投资风格", ["价值投资", "成长投资", "合理价格成长（GARP）", "红利 / 高股息", "趋势投资", "量化投资",
              "逆向投资", "事件驱动", "指数 / 被动投资"]),
    ("持有周期", ["长线（3 年以上）", "中线（1~3 年）", "短线 / 波段"]),
    ("风险偏好", ["保守", "稳健", "积极", "激进"]),
    ("投资名人", ["巴菲特", "芒格", "格雷厄姆", "费雪", "彼得·林奇", "段永平", "李录", "霍华德·马克斯",
              "约翰·邓普顿", "约翰·博格", "达里奥", "索罗斯", "利弗莫尔", "欧奈尔", "西蒙斯"]),
    ("看重什么", ["护城河", "自由现金流", "分红回购", "估值安全边际", "高增长", "行业景气", "管理层",
              "量价 / 技术面", "宏观周期"]),
]
ALL_TAGS = {t for _, tags in TAG_GROUPS for t in tags}

# 数据页指标：key → (名称, 分组, 格式, 说明)。格式：pct 百分比 / x 倍数 / money 金额 / price 价格 / text
METRICS = {
    "industry": ("行业", "基础", "text", ""),
    "weight": ("持仓权重", "仓位", "pct", "按市值算的仓位占比；只填权重时显示你填的权重"),
    "cost_weight": ("成本权重", "仓位", "pct", "按买入成本算的仓位占比"),
    "market_value": ("市值（元）", "仓位", "money", "持仓市值，折算成人民币"),
    "pnl_pct": ("浮动盈亏", "仓位", "pct", "相对买入价的涨跌"),
    "price": ("股价", "行情", "price", "最近一个交易日收盘价"),
    "change_pct": ("日涨跌", "行情", "pct", "最近一个交易日涨跌幅"),
    "chg_20d": ("20 日涨跌", "趋势", "pct", ""),
    "chg_60d": ("60 日涨跌", "趋势", "pct", ""),
    "chg_250d": ("一年涨跌", "趋势", "pct", "约 250 个交易日"),
    "dist_high_52w": ("距 52 周高点", "趋势", "pct", "负数表示从高点回撤了多少"),
    "ma200_gap": ("相对 200 日均线", "趋势", "pct", "正数在均线上方"),
    "pe": ("PE（TTM）", "估值", "x", "市盈率，滚动 12 个月"),
    "pe_pct": ("PE 分位", "估值", "pct", "当前 PE 在近 10 年（A股约 8~9 年）里的位置，越低越便宜"),
    "pb": ("PB", "估值", "x", "市净率"),
    "pb_pct": ("PB 分位", "估值", "pct", "当前 PB 在近 10 年里的位置"),
    "peg": ("PEG", "估值", "x", "PE ÷ 近 3 年净利润复合增速，<1 通常认为增长相对便宜"),
    "dividend_yield": ("股息率", "估值", "pct", "近 12 个月现金分红 ÷ 股价"),
    "fcf_yield": ("自由现金流收益率", "估值", "pct", "最近年度自由现金流 ÷ 市值"),
    "market_cap": ("总市值", "估值", "money", "公司市值（本币）"),
    "roe": ("最新报告 ROE", "盈利质量", "pct", "最新一期报告的 ROE，季报 / 中报为累计值、未年化"),
    "roe_fy": ("年度 ROE", "盈利质量", "pct", "最近一个完整财年的 ROE"),
    "gross_margin": ("毛利率", "盈利质量", "pct", "最新报告"),
    "net_margin": ("净利率", "盈利质量", "pct", "最新报告"),
    "ocf_np": ("现金含量", "盈利质量", "x", "最近年度经营现金流 ÷ 净利润，持续 <1 要留意利润质量"),
    "debt_ratio": ("资产负债率", "财务安全", "pct", ""),
    "rev_yoy": ("营收增速", "成长", "pct", "最新报告同比"),
    "np_yoy": ("净利增速", "成长", "pct", "最新报告同比"),
    "np_cagr3": ("3 年净利复合增速", "成长", "pct", ""),
    "report_period": ("最新报告期", "基础", "text", ""),
}
DEFAULT_METRICS = ["industry", "weight", "price", "change_pct", "pe", "pe_pct", "pb", "pb_pct", "dividend_yield", "roe_fy"]

# 关注点规则：key → (名称, 类型, 默认阈值, 阈值说明, 说明)。类型：risk 风险 / chance 机会
CONCERNS = {
    "overvalued": ("估值偏高", "risk", 80, "PE 分位 ≥", "PE 处在历史高位，安全边际变薄"),
    "pb_high": ("PB 偏高", "risk", 80, "PB 分位 ≥", ""),
    "undervalued": ("估值处于低位", "chance", 20, "PE 分位 ≤", "PE 处在历史低位，值得重新审视是机会还是陷阱"),
    "roe_low": ("ROE 偏低", "risk", 15, "年度 ROE <", "巴菲特常用的门槛是长期 ROE ≥ 15%"),
    "roe_decline": ("ROE 连续下滑", "risk", 2, "连续下降年数 ≥", "盈利能力在走弱"),
    "fcf_negative": ("自由现金流为负", "risk", 0, "", "经营现金流不够覆盖资本开支"),
    "cash_quality": ("利润含金量低", "risk", 0.8, "经营现金流 ÷ 净利润 <", "利润没有变成现金"),
    "profit_decline": ("净利润下滑", "risk", 10, "最新报告净利同比下降 ≥ %", ""),
    "revenue_decline": ("营收下滑", "risk", 0, "最新报告营收同比下降", ""),
    "margin_squeeze": ("毛利率下滑", "risk", 3, "较 3 年前下降 ≥ 个百分点", "定价权或竞争格局可能在变化——护城河信号"),
    "debt_rising": ("负债率上升", "risk", 10, "较 3 年前上升 ≥ 个百分点", ""),
    "moat_news": ("护城河相关利空", "risk", 5, "近 90 天价值框架评分 ≤ -", "资讯里出现削弱护城河的事件"),
    "thesis_weakened": ("投资逻辑被削弱", "risk", 0, "", "近 30 天有信息削弱或证伪了你写的投资逻辑"),
    "drawdown": ("大幅回撤", "risk", 30, "距 52 周高点回撤 ≥ %", ""),
    "below_ma200": ("跌破 200 日均线", "risk", 0, "", "趋势转弱"),
    "dividend_high": ("股息率较高", "chance", 5, "股息率 ≥ %", ""),
}
DEFAULT_CONCERNS = ["overvalued", "undervalued", "roe_low", "roe_decline", "fcf_negative", "cash_quality",
                    "profit_decline", "margin_squeeze", "moat_news", "thesis_weakened"]


def catalog_out() -> dict:
    return {
        "tags": [{"group": g, "tags": tags} for g, tags in TAG_GROUPS],
        "metrics": [{"key": k, "label": v[0], "group": v[1], "fmt": v[2], "desc": v[3]} for k, v in METRICS.items()],
        "concerns": [{"key": k, "label": v[0], "kind": v[1], "threshold": v[2], "unit": v[3], "desc": v[4]}
                     for k, v in CONCERNS.items()],
    }
