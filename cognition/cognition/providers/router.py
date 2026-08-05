"""模型分层路由（按角色选 provider+model）。

M1 只有单 provider；M2 引入 **按角色分层**：planner 与 executor 可各自配置
provider/model（env `COGNITION_PLANNER_PROVIDER/MODEL`、`COGNITION_EXECUTOR_PROVIDER/MODEL`），
未配置则回落到单 provider 设置（`COGNITION_MODEL_PROVIDER` + 对应 model）。

默认 deepseek（性价比）；owner 在最终集成时把 planner 切到 opus（只改 env，不改码）。
这是「最小模型路由」seam：预算/语义缓存/降级链留后续里程碑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.tools import BaseTool

from cognition.config import Settings, get_settings
from cognition.providers.anthropic_provider import build_anthropic_chat
from cognition.providers.deepseek_provider import build_deepseek_chat

# 角色→首选 provider 的 seam（仅在该角色未显式配置且无全局 provider 时作为兜底偏好）。
ROLE_TO_PROVIDER: dict[str, str] = {
    "planner": "deepseek",
    "executor": "deepseek",
    "complex": "anthropic",
    "simple": "deepseek",
    "summary": "deepseek",
}


def _resolve_role(role: str, settings: Settings) -> tuple[str, Optional[str]]:
    """解析角色对应的 (provider, model_override)。

    优先级：角色专属 env（planner_*/executor_*）> 全局 model_provider > 角色偏好默认。
    返回的 model_override 为 None 时表示用该 provider 的默认 model（anthropic_model/deepseek_model）。
    """
    role_provider: Optional[str] = None
    role_model: Optional[str] = None
    if role == "planner":
        role_provider = settings.planner_provider
        role_model = settings.planner_model
    elif role == "executor":
        role_provider = settings.executor_provider
        role_model = settings.executor_model

    if role_provider:
        return role_provider.lower(), role_model

    # 角色未显式配置：用全局 provider；全局也缺省时用角色偏好。
    provider = (settings.model_provider or ROLE_TO_PROVIDER.get(role, "deepseek")).lower()
    return provider, role_model


def resolve_with_fallback(primary, secondary, build_fn):
    """两层交叉兜底（纯逻辑）：先 build_fn(primary)，异常则 build_fn(secondary)，双败抛错。

    build_fn 由调用方注入（真实构造或测试假函数），故本函数可离线测、不触真实 API。
    """
    try:
        return build_fn(primary)
    except Exception as primary_exc:  # noqa: BLE001 — 兜底：primary 失败切 secondary
        if secondary and secondary != primary:
            try:
                return build_fn(secondary)
            except Exception as secondary_exc:  # noqa: BLE001
                raise RuntimeError(
                    f"两个 provider 均不可用: {primary}({primary_exc}) / {secondary}({secondary_exc})"
                ) from secondary_exc
        raise RuntimeError(f"provider 不可用且无兜底: {primary}({primary_exc})") from primary_exc


def _build_provider_model(provider: str, settings: Settings, model_override: Optional[str]):
    kwargs = {"model": model_override} if model_override else {}
    if provider == "deepseek":
        return build_deepseek_chat(settings, **kwargs)
    return build_anthropic_chat(settings, **kwargs)


def select_model(
    role: str,
    *,
    tools: Optional[Sequence["BaseTool"]] = None,
    settings: Optional[Settings] = None,
) -> "BaseChatModel":
    """按角色选择模型；如给定 tools 则 bind_tools。构造失败时交叉兜底到另一 provider。

    role ∈ {"planner", "executor", "complex", "simple", "summary"}。
    """
    settings = settings or get_settings()
    provider, model_override = _resolve_role(role, settings)
    secondary = "anthropic" if provider == "deepseek" else "deepseek"

    # secondary 用该 provider 默认 model（不套用 primary 的 model_override）。
    def _build(p: str):
        return _build_provider_model(p, settings, model_override if p == provider else None)

    model = resolve_with_fallback(provider, secondary, _build)
    if tools:
        model = model.bind_tools(list(tools))
    return model
