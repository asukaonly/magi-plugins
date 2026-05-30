"""Manifest helper builders for the chrome-history plugin."""

from __future__ import annotations

from magi_plugin_sdk.contracts import (
    ActivationFlowField,
    ActivationFlowFieldOption,
    ActivationFlowSpec,
)


def build_activation_flow() -> ActivationFlowSpec:
    return ActivationFlowSpec(
        title="启用 Chrome 浏览历史",
        description="Chrome 浏览历史属于敏感的本地数据。请选择首次同步如何初始化时间线，再启用该来源。",
        enabled_key="enabled",
        configured_key="configured",
        authorize_on_confirm=True,
        fields=[
            ActivationFlowField(
                key="first_sync_scope",
                label="首次同步范围",
                type="select",
                default="recent",
                options=[
                    ActivationFlowFieldOption(value="recent", label="同步最近几天"),
                    ActivationFlowFieldOption(value="all", label="同步全部历史"),
                    ActivationFlowFieldOption(value="none", label="不回填，从现在开始"),
                ],
                visible_when=None,
            ),
            ActivationFlowField(
                key="first_sync_days",
                label="同步最近天数",
                type="number",
                default=30,
                visible_when={"field": "first_sync_scope", "equals": "recent"},
            ),
        ],
        title_i18n=None,
        description_i18n=None,
    )
