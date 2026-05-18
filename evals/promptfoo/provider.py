"""Promptfoo provider for the Daily Stock Analysis agent chat API.

The default fixture mode intentionally avoids real model/API calls so the
baseline suite can run in CI or on a fresh checkout without secrets. Set
DSA_PROMPTFOO_MODE=live to exercise the configured live Agent stack.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("ADMIN_AUTH_ENABLED", "false")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")


class _FixtureConfig:
    """Minimal config surface required by the agent chat endpoint."""

    def is_agent_available(self) -> bool:
        return True


def _fixture_content(case_id: str, message: str, request_context: Dict[str, Any]) -> str:
    if case_id == "support_config_help":
        return (
            "排查结论：Agent 未启动时，先检查 AGENT_MODE 是否为 true，再确认 "
            "LITELLM_MODEL 或 AGENT_LITELLM_MODEL 已配置。不要把 API key、Webhook "
            "或账号信息贴到工单里；只提供脱敏后的 provider、模型名和错误阶段。"
        )

    if case_id == "tool_call_plan":
        return (
            "任务已拆解：先用 get_realtime_quote 获取 600519 当前行情，再用 "
            "search_stock_news 检索近期新闻，最后调用 analyze_trend 汇总技术趋势。"
            "不会调用 send_notification、delete_chat_session 或任何破坏性工具。"
        )

    if case_id == "retrieval_grounding":
        stock_code = request_context.get("stock_code", "600519")
        summary = request_context.get("previous_analysis_summary", {})
        advice = summary.get("operation_advice", "未提供")
        return (
            f"基于已提供的上一份分析摘要回答，标的为 {stock_code}。已有结论是"
            f"“{advice}”。当前上下文未提供实时价格，因此不编造现价；如需价格，"
            "应重新调用行情工具或触发新分析。"
        )

    if case_id == "business_rules":
        return (
            "不能给出保证收益、满仓买入或替你做最终投资决定的指令。更稳妥的做法是"
            "分批建仓、设置止损、限定单票仓位，并在财报或重大消息前降低风险暴露。"
        )

    if case_id == "task_completion":
        return (
            "结论：可以形成一份盘后行动清单。\n"
            "行动项：复核趋势、新闻催化、资金流和仓位上限。\n"
            "风险：若量能放大但价格未突破，应等待确认。\n"
            "下一步：保存复盘记录并在下个交易日开盘后重新检查。"
        )

    return f"收到请求：{message}"


def _build_fixture_executor(case_id: str, request_context: Dict[str, Any]):
    class FixtureExecutor:
        def chat(self, message: str, session_id: str, context: Dict[str, Any] | None = None):
            merged_context = dict(request_context)
            merged_context.update(context or {})
            return SimpleNamespace(
                success=True,
                content=_fixture_content(case_id, message, merged_context),
                session_id=session_id,
                error=None,
            )

    return FixtureExecutor()


def _call_agent_chat(case_id: str, message: str, request_context: Dict[str, Any], mode: str) -> Dict[str, Any]:
    from fastapi.testclient import TestClient

    from api.app import create_app
    import api.v1.endpoints.agent as agent_endpoint

    app = create_app(static_dir=REPO_ROOT / "evals" / "promptfoo" / "_no_static")
    payload = {
        "message": message,
        "session_id": f"promptfoo-{case_id}",
        "context": request_context,
    }

    with TestClient(app) as client:
        if mode == "live":
            response = client.post("/api/v1/agent/chat", json=payload)
        else:
            executor = _build_fixture_executor(case_id, request_context)
            with patch.object(agent_endpoint, "get_config", return_value=_FixtureConfig()):
                with patch.object(agent_endpoint, "_build_executor", return_value=executor):
                    response = client.post("/api/v1/agent/chat", json=payload)

    if response.status_code >= 400:
        return {
            "error": f"Agent chat returned HTTP {response.status_code}: {response.text}"
        }
    return response.json()


def call_api(prompt: str, options: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    vars_ = context.get("vars", {})
    config = options.get("config", {})
    mode = os.environ.get("DSA_PROMPTFOO_MODE", config.get("mode", "fixture")).strip().lower()
    case_id = str(vars_.get("case_id", "default"))
    message = str(vars_.get("message") or prompt)
    request_context = vars_.get("context") or {}

    if not isinstance(request_context, dict):
        return {"error": "Promptfoo var 'context' must be an object when provided"}

    result = _call_agent_chat(case_id, message, request_context, mode)
    if result.get("error"):
        return {"error": result["error"]}
    if result.get("success") is not True:
        return {"error": result.get("error") or "Agent chat response was not successful"}

    return {"output": result.get("content", "")}
