"""
utils/ai_client.py - AI 情绪分析客户端 (V2.0)

功能：
  1. 调用 OpenAI 兼容的 API（支持 OpenAI, DeepSeek, 零一万物等）
  2. 对新闻标题进行情绪分析，返回 [-1, 1] 的情绪分数
  3. 带缓存 + 降级机制，失败时回退到关键词分析
  4. 动态新闻权重：根据数据新鲜度和 AI 分析质量调整权重

配置（环境变量或 config.yaml ai 节）：
  AI_API_KEY:    API 密钥
  AI_BASE_URL:   API 端点（默认 https://api.openai.com/v1）
  AI_MODEL:      模型名（默认 gpt-4o-mini）
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger: logging.Logger = logging.getLogger("ai_client")

# ── 常量定义 ──────────────────────────────────────────────────────────────────
_CONFIG_CACHE_TTL_SEC: int = 60      # 配置缓存有效期（秒）
_MAX_HEADLINES: int = 15             # 情绪分析最多输入标题数
_MAX_RESPONSE_TOKENS: int = 200      # AI 回复最大 token 数
_DEFAULT_CONFIDENCE: float = 0.5     # 默认置信度
_ERROR_BODY_MAX_LEN: int = 200       # HTTP 错误体截断长度

# ── 配置加载 ──────────────────────────────────────────────────────────────────

_config_cache: Dict[str, str] = {}
_config_load_time: float = 0


def _load_ai_config() -> Dict[str, str]:
    """
    加载 AI 客户端配置。优先级：环境变量 > config.yaml ai 节。
    """
    global _config_cache, _config_load_time

    if _config_cache and (time.time() - _config_load_time) < _CONFIG_CACHE_TTL_SEC:
        return _config_cache

    # 从环境变量
    api_key = os.environ.get("AI_API_KEY", "")
    base_url = os.environ.get("AI_BASE_URL", "")
    model = os.environ.get("AI_MODEL", "")

    # 从 config.yaml
    if not api_key:
        try:
            from utils.config_loader import get_config
            ai_cfg = get_config().get("ai", {})
            api_key = api_key or ai_cfg.get("api_key", "")
            base_url = base_url or ai_cfg.get("base_url", "https://api.openai.com/v1")
            model = model or ai_cfg.get("model", "gpt-4o-mini")
        except Exception:
            pass

    if not base_url:
        base_url = "https://api.openai.com/v1"
    if not model:
        model = "gpt-4o-mini"

    # 确保 base_url 不以 / 结尾
    base_url = base_url.rstrip("/")

    _config_cache = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }
    _config_load_time = time.time()
    return _config_cache


def is_ai_configured() -> bool:
    """检查 AI 客户端是否已配置（有 API Key）。"""
    cfg = _load_ai_config()
    return bool(cfg.get("api_key"))


# ── AI 情绪分析 ──────────────────────────────────────────────────────────────

_SENTIMENT_PROMPT = """你是一个加密货币市场情绪分析专家。请分析以下新闻标题，给出一个综合情绪分数。

评分规则：
- 分数范围：-1.0（极度看跌）到 +1.0（极度看涨）
- 0.0 表示中性
- 考虑以下因素：
  * 监管政策（利好/利空）
  * 市场资金流向
  * 技术发展（ETF、Layer2、DeFi等）
  * 宏观经济（加息、通胀、就业等对风险资产的影响）
  * 市场情绪（恐慌/贪婪）

请只返回一个 JSON 对象，格式如下（不要加任何其他内容）：
{"score": 0.3, "confidence": 0.8, "summary": "一句话总结"}

新闻标题：
{headlines}"""


def analyze_sentiment(headlines: List[str], timeout: float = 15.0) -> float:
    """
    调用 AI 对新闻标题进行情绪分析。

    :param headlines: 新闻标题列表
    :param timeout: 超时秒数
    :return: 情绪分数 [-1.0, 1.0]，失败返回 0.0
    """
    if not headlines:
        return 0.0

    cfg = _load_ai_config()
    if not cfg.get("api_key"):
        logger.debug("AI 未配置 API Key，跳过 AI 分析")
        return 0.0

    # 构建 prompt
    headlines_text = "\n".join(f"- {h}" for h in headlines[:_MAX_HEADLINES])
    prompt = _SENTIMENT_PROMPT.format(headlines=headlines_text)

    try:
        result = _chat_completion(
            prompt=prompt,
            max_tokens=_MAX_RESPONSE_TOKENS,
            temperature=0.3,
            timeout=timeout,
        )
        if not result:
            return 0.0

        # 解析 JSON 响应
        parsed = _extract_json(result)
        if parsed and "score" in parsed:
            score = float(parsed["score"])
            confidence = float(parsed.get("confidence", _DEFAULT_CONFIDENCE))
            summary = parsed.get("summary", "")
            logger.info(
                f"AI 情绪分析: score={score:+.2f} conf={confidence:.2f} | {summary}"
            )
            # 将置信度融入分数（低置信度时衰减分数）
            return max(-1.0, min(1.0, score * confidence))
        else:
            logger.warning(f"AI 返回格式异常: {result[:_ERROR_BODY_MAX_LEN]}")
            return 0.0

    except Exception as e:
        logger.warning(f"AI 情绪分析失败: {e}")
        return 0.0


def analyze_sentiment_detailed(headlines: List[str]) -> Optional[Dict[str, Any]]:
    """
    详细版 AI 分析，返回完整结果（供 API 端点使用）。
    
    :return: {"score": float, "confidence": float, "summary": str} 或 None
    """
    if not headlines:
        return None

    cfg = _load_ai_config()
    if not cfg.get("api_key"):
        return None

    headlines_text = "\n".join(f"- {h}" for h in headlines[:15])
    prompt = _SENTIMENT_PROMPT.format(headlines=headlines_text)

    try:
        result = _chat_completion(prompt=prompt, max_tokens=200, temperature=0.3)
        if result:
            parsed = _extract_json(result)
            if parsed and "score" in parsed:
                return {
                    "score": float(parsed["score"]),
                    "confidence": float(parsed.get("confidence", 0.5)),
                    "summary": parsed.get("summary", ""),
                }
    except Exception as e:
        logger.warning(f"AI 详细分析失败: {e}")

    return None


# ── 动态新闻权重计算 ──────────────────────────────────────────────────────────

def calculate_dynamic_news_weight(
    base_weight: float = 0.3,
    age_minutes: float = 0,
    article_count: int = 0,
    ai_available: bool = False,
) -> float:
    """
    V2.0: 动态计算新闻权重。

    根据以下因素调整新闻面在综合评分中的权重：
      1. 数据新鲜度：越新越可信，超过 2h 开始衰减
      2. 新闻数量：数据越多越可信
      3. AI 分析可用性：有 AI 分析的权重可以更高

    :param base_weight: 基础权重（config.yaml 中的 news_weight）
    :param age_minutes: 最新数据距今的分钟数
    :param article_count: 新闻条数
    :param ai_available: 是否有 AI 分析结果
    :return: 调整后的权重 [0, base_weight * 1.5]
    """
    if base_weight <= 0:
        return 0.0

    weight = base_weight

    # 1. 新鲜度衰减
    if age_minutes <= 30:
        freshness = 1.0      # 30 分钟内，全权重
    elif age_minutes <= 120:
        freshness = 1.0 - (age_minutes - 30) / 180  # 线性衰减
    else:
        freshness = 0.3      # 超过 2 小时，大幅衰减

    weight *= freshness

    # 2. 数据量加成
    if article_count >= 10:
        weight *= 1.2   # 新闻多，信息更充分
    elif article_count <= 2:
        weight *= 0.6   # 新闻太少，不太可信

    # 3. AI 加成
    if ai_available:
        weight *= 1.3   # AI 分析比纯关键词更准

    # 上限：不超过基础权重的 1.5 倍
    return min(weight, base_weight * 1.5)


# ── 底层 HTTP 调用 ────────────────────────────────────────────────────────────

def _chat_completion(prompt: str, max_tokens: int = 500,
                     temperature: float = 0.3,
                     timeout: float = 15.0) -> Optional[str]:
    """
    调用 OpenAI 兼容的 Chat Completions API。
    返回助手回复文本，失败返回 None。
    """
    cfg = _load_ai_config()
    api_key = cfg["api_key"]
    base_url = cfg["base_url"]
    model = cfg["model"]

    url = f"{base_url}/chat/completions"

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个专业的金融市场情绪分析师。只返回 JSON 格式的分析结果。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "QuantBot/2.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:_ERROR_BODY_MAX_LEN]
        except Exception:
            pass
        logger.warning(f"AI API HTTP {e.code}: {body}")
        return None
    except Exception as e:
        logger.warning(f"AI API 调用失败: {e}")
        return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 AI 回复中提取 JSON 对象（兼容 markdown 代码块）。"""
    text = text.strip()

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown 代码块提取
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 尝试提取第一个 {...} 块
    match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None
