# OpenClaw News Sync Task Template

用于 OpenClaw 定时任务的标准执行内容。每次执行尽量保持一致，便于结果稳定。

## 目标
抓取最近一段时间内与 BTC/宏观/ETF/加密市场相关的重要新闻，输出统一 JSON，回写到 QuantBot 的 `/api/news-sync/ingest`。

## 固定执行步骤
1. 读取 QuantBot `/api/news-sync/config`
2. 若 `enabled_by_weight=false`，直接结束，不写入
3. 使用固定 query 模板抓新闻：
   - Bitcoin ETF inflows outflows latest
   - BTC macro Fed CPI rates crypto latest
   - Bitcoin regulation exchange crypto market latest
4. 仅关注最近 `lookback_hours` 小时内容
5. 汇总出：
   - regime_hint: bull / bear / ranging / unknown
   - confidence: 0~1
   - combined_score: -1~1
   - crypto_score: -1~1
   - macro_score: -1~1
   - summary_text
   - headlines[]
6. POST 到 `/api/news-sync/ingest`

## 输出约束
- 尽量使用相同判断标准
- 避免随意改变打分口径
- headlines 控制在 6~12 条
- summary 简洁，优先解释“为什么偏多/偏空/震荡”

## 建议初始权重
- news_weight: 0.08
