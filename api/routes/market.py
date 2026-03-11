"""
api/routes/market.py - V2.0/V3.0 市场数据 API

提供：
  1. 当前 regime 状态（含技术面/新闻面/链上数据详情）
  2. 资金费率实时 + 历史
  3. 持仓量 (OI) 实时
  4. 策略绩效统计
  5. 综合市场情绪面板数据
"""
from fastapi import APIRouter, Depends
from api.auth.jwt_handler import get_current_user
from utils.config_loader import get_config

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/regime", summary="当前市场 regime 状态（V2.0）")
def get_regime_status(user=Depends(get_current_user)):
    """
    返回当前 AUTO 选择器的 regime 评估详情。
    包含技术面、新闻面、资金费率、OI 各维度的评分和投票结果。
    """
    from core.user_bot import manager as bot_mgr

    uid = user["id"]
    bs = bot_mgr.bot_status(uid)

    # 尝试从运行中的 selector 获取最新 regime detail
    regime_detail = {}
    try:
        # 从 bot_state 的 extra 数据读取
        selector = bot_mgr.get_user_selector(uid)
        if selector and hasattr(selector, 'last_regime_detail'):
            regime_detail = selector.last_regime_detail
    except Exception:
        pass

    return {
        "running": bs.get("running", False),
        "regime_detail": regime_detail,
    }


@router.get("/funding-rate", summary="资金费率（实时 + 最近历史）")
def get_funding_rate(symbol: str = "BTC/USDT:USDT"):
    """
    获取当前资金费率和最近 24 期历史数据。
    使用 OKX 公开 API，无需用户 API Key。
    """
    try:
        from data.market_extra import (
            fetch_funding_rate,
            fetch_funding_rate_history,
            _symbol_to_inst_id,
        )
        inst_id = _symbol_to_inst_id(symbol)

        current = fetch_funding_rate(inst_id) or {}
        history = fetch_funding_rate_history(inst_id, limit=24)

        return {
            "current": current,
            "history": history,
            "inst_id": inst_id,
        }
    except Exception as e:
        return {"error": str(e), "current": {}, "history": []}


@router.get("/open-interest", summary="持仓量（实时）")
def get_open_interest(symbol: str = "BTC/USDT:USDT"):
    """获取当前持仓量 (OI) 数据。"""
    try:
        from data.market_extra import fetch_open_interest, _symbol_to_inst_id
        inst_id = _symbol_to_inst_id(symbol)
        data = fetch_open_interest(inst_id) or {}
        return {"data": data, "inst_id": inst_id}
    except Exception as e:
        return {"error": str(e), "data": {}}


@router.get("/sentiment", summary="综合市场情绪面板数据")
def get_market_sentiment(symbol: str = "BTC/USDT:USDT"):
    """
    聚合所有情绪维度数据，供前端 Dashboard 使用。
    包含：资金费率信号 + OI 信号 + 新闻情绪 + 综合评分。
    """
    result = {
        "funding": None,
        "oi": None,
        "news": None,
        "ai_available": False,
        "composite": {
            "signal": "neutral",
            "score": 0.0,
        },
    }

    # 资金费率 + OI
    try:
        from data.market_extra import get_market_extra_signals
        extra = get_market_extra_signals(symbol)
        result["funding"] = extra.get("funding")
        result["oi"] = extra.get("oi")
        result["composite"]["score"] = extra.get("composite_score", 0.0)
        result["composite"]["signal"] = extra.get("composite_signal", "neutral")
    except Exception:
        pass

    # 新闻情绪
    try:
        from news.news_fetcher import get_latest_sentiment, get_sentiment_age_minutes
        sentiment = get_latest_sentiment()
        if sentiment:
            age = get_sentiment_age_minutes()
            result["news"] = {
                **sentiment,
                "age_minutes": round(age, 1),
            }
    except Exception:
        pass

    # AI 可用性
    try:
        from utils.ai_client import is_ai_configured
        result["ai_available"] = is_ai_configured()
    except Exception:
        pass

    return result


@router.get("/strategy-performance", summary="策略绩效统计（V2.5）")
def get_strategy_performance(user=Depends(get_current_user)):
    """
    返回各策略最近 N 笔交易的绩效统计：
    胜率、平均盈亏、盈亏比、总笔数。
    """
    from execution.db_handler import get_conn

    uid = user["id"]
    conn = get_conn()

    try:
        rows = conn.execute("""
            SELECT strategy_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                   AVG(pnl) as avg_pnl,
                   SUM(pnl) as total_pnl,
                   AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
                   AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss
            FROM strategy_performance
            WHERE user_id = ?
            GROUP BY strategy_name
            ORDER BY total DESC
        """, (uid,)).fetchall()
    except Exception:
        # 表可能不存在
        return []

    result = []
    for r in rows:
        total = r[1]
        wins = r[2]
        losses = r[3]
        avg_win = r[6] or 0
        avg_loss = abs(r[7] or 0)

        result.append({
            "strategy_name": r[0],
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 3) if total > 0 else 0,
            "avg_pnl": round(r[4] or 0, 2),
            "total_pnl": round(r[5] or 0, 2),
            "profit_factor": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0,
        })

    return result
