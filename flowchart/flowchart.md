```mermaid
graph TD
    Start(每日数据) --> PE{PE 分位}

    PE -->|PE ≤ bx| BuyEx[极寒买入]
    PE -->|bx < PE ≤ bc| BuyCh[低估买入]
    PE -->|PE > bc| Trend{趋势完好}

    BuyEx --> BuyExAct[加仓至 30%]
    BuyCh --> BuyChAct[加仓至 20%]
    BuyExAct --> Done(今日结束)
    BuyChAct --> Done

    Trend -->|是, 站上 MA60| Hold[享受泡沫]
    Trend -->|否, 跌破 MA60| PanicQ{PE > sp}

    Hold --> Done

    PanicQ -->|是| PBp{PB > pp}
    PanicQ -->|否| WarnQ{PE > sw}

    PBp -->|是| PanicSell[清仓至 10%]
    PBp -->|否| InterceptP[拦截 持仓]

    WarnQ -->|是| PBw{PB > pw}
    WarnQ -->|否| Wait[静待时机]

    PBw -->|是| WarnSell[减仓至 20%]
    PBw -->|否| InterceptW[拦截 持仓]

    PanicSell --> Done
    InterceptP --> Done
    WarnSell --> Done
    InterceptW --> Done
    Wait --> Done

    Done --> HalfY{6月30日 或 12月31日}
    HalfY -->|是| Rebalance[强制再平衡]
    HalfY -->|否| Skip(等待下一日)
    Rebalance --> Skip
```

> **参数说明**
>
> | 符号 | 参数名 | 默认值 | 含义 |
> |---|---|---|---|
> | `bx` | buy_extreme | 10 | PE ≤ 此值 → 极寒买入 |
> | `bc` | buy_cheap | 30 | PE ≤ 此值 → 低估买入 |
> | `sw` | sell_warn | 50 | PE > 此值 → 考虑卖出 |
> | `sp` | sell_panic | 70 | PE > 此值 → 清仓逃顶 |
> | `pw` | pb_confirm_warn | 40 | 配合卖出判断，PB 需大于此值 |
> | `pp` | pb_confirm_panic | 50 | 配合清仓判断，PB 需大于此值 |
>
> 所有参数可通过侧边栏「⚙️ 策略阈值」面板实时调整。
