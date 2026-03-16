"""Settings schema registry for schema-driven Web UI configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

SchemaProvider = Callable[[], dict[str, Any]]


@dataclass
class _Provider:
    name: str
    order: int
    fn: SchemaProvider


class SettingsSchemaRegistry:
    """Collect settings schema fragments from core and extensions."""

    def __init__(self) -> None:
        self._providers: list[_Provider] = []

    def register(self, name: str, fn: SchemaProvider, *, order: int = 100) -> None:
        self._providers = [p for p in self._providers if p.name != name]
        self._providers.append(_Provider(name=name, order=order, fn=fn))

    def build_schema(self) -> dict[str, Any]:
        categories: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        seen_category_ids: set[str] = set()
        for provider in sorted(self._providers, key=lambda p: (p.order, p.name)):
            fragment = provider.fn() or {}
            fragment_categories = fragment.get("categories") or []
            if isinstance(fragment_categories, list):
                for cat in fragment_categories:
                    if not isinstance(cat, dict):
                        continue
                    cat_id = str(cat.get("id", "")).strip()
                    if not cat_id or cat_id in seen_category_ids:
                        continue
                    seen_category_ids.add(cat_id)
                    categories.append(cat)
            fragment_sections = fragment.get("sections") or []
            if isinstance(fragment_sections, list):
                for sec in fragment_sections:
                    if isinstance(sec, dict):
                        sections.append(sec)

        categories.sort(key=lambda c: int(c.get("order", 999)))
        sections.sort(key=lambda s: int(s.get("order", 999)))
        return {
            "version": 1,
            "categories": categories,
            "sections": sections,
        }


def build_core_settings_schema() -> dict[str, Any]:
    """Default schema for built-in settings."""
    return {
        "categories": [
            {
                "id": "security",
                "label": "Security",
                "label_i18n": "config.securityControl",
                "order": 10,
            },
            {
                "id": "memory",
                "label": "Memory",
                "label_i18n": "config.memoryLifecycle",
                "order": 20,
            },
            {
                "id": "intelligence",
                "label": "Intelligence",
                "label_i18n": "config.taskComplexity",
                "order": 30,
            },
            {
                "id": "vision",
                "label": "Vision",
                "label_i18n": "config.visionAnalysis",
                "order": 40,
            },
            {
                "id": "diagnostics",
                "label": "Diagnostics",
                "label_i18n": "config.diagnostics",
                "order": 50,
            },
            {
                "id": "agents",
                "label": "Agents",
                "label_i18n": "config.delegationComplexity",
                "order": 60,
            },
            {
                "id": "integrations",
                "label": "Integrations",
                "label_i18n": "config.integrations",
                "order": 70,
            },
        ],
        "sections": [
            {
                "id": "security-control",
                "category": "security",
                "title": "Security Control",
                "title_i18n": "config.securityControl",
                "order": 10,
                "fields": [
                    {
                        "path": "security.full_access_mode",
                        "type": "boolean",
                        "label": "Full Access Mode",
                        "label_i18n": "config.fullAccessMode",
                        "help": "Bypass approval and sandbox checks. Use only in isolated environments.",
                        "help_i18n": "config.fullAccessModeDesc",
                    },
                ],
            },
            {
                "id": "execution-guard",
                "category": "security",
                "title": "執行防護",
                "title_i18n": "config.executionGuard",
                "order": 15,
                "fields": [
                    {
                        "path": "execution_guard.enabled",
                        "type": "boolean",
                        "label": "啟用執行防護",
                        "label_i18n": "config.egEnabled",
                        "help": "防止重複工具迴圈與高風險的大量操作。",
                        "help_i18n": "config.egEnabledDesc",
                    },
                    {
                        "path": "execution_guard.max_repeat_tool_call",
                        "type": "number",
                        "label": "同一工具重複上限",
                        "label_i18n": "config.egMaxRepeat",
                        "help": "每個任務中，相同工具+相同參數最多允許次數。0 表示不限制。",
                        "help_i18n": "config.egMaxRepeatDesc",
                        "min": 0,
                        "max": 10,
                        "step": 1,
                    },
                    {
                        "path": "execution_guard.max_tool_calls_per_task",
                        "type": "number",
                        "label": "每任務工具呼叫上限",
                        "label_i18n": "config.egMaxToolCalls",
                        "help": "每個任務工具呼叫硬上限。0 表示不限制。",
                        "help_i18n": "config.egMaxToolCallsDesc",
                        "min": 0,
                        "max": 500,
                        "step": 1,
                    },
                    {
                        "path": "execution_guard.max_shell_calls_per_task",
                        "type": "number",
                        "label": "每任務 Shell 呼叫上限",
                        "label_i18n": "config.egMaxShellCalls",
                        "help": "每個任務 shell_execute 呼叫硬上限。0 表示不限制。",
                        "help_i18n": "config.egMaxShellCallsDesc",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                    },
                    {
                        "path": "execution_guard.max_download_ops_per_task",
                        "type": "number",
                        "label": "下載操作上限",
                        "label_i18n": "config.egMaxDownloadOps",
                        "help": "每個任務中下載類 Shell 操作硬上限。0 表示不限制。",
                        "help_i18n": "config.egMaxDownloadOpsDesc",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                    },
                    {
                        "path": "execution_guard.max_destructive_shell_ops_per_task",
                        "type": "number",
                        "label": "破壞性 Shell 操作上限",
                        "label_i18n": "config.egMaxDestructiveOps",
                        "help": "每個任務中刪除/移動/重新命名等 Shell 操作硬上限。0 表示不限制。",
                        "help_i18n": "config.egMaxDestructiveOpsDesc",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                    },
                    {
                        "path": "execution_guard.require_confirm_for_bulk_shell",
                        "type": "boolean",
                        "label": "大量 Shell 操作需再次確認",
                        "label_i18n": "config.egConfirmBulk",
                        "help": "當命令被判定為大量或高風險時，要求第二次確認。",
                        "help_i18n": "config.egConfirmBulkDesc",
                    },
                    {
                        "path": "execution_guard.bulk_shell_score_threshold",
                        "type": "number",
                        "label": "大量操作風險門檻",
                        "label_i18n": "config.egBulkThreshold",
                        "help": "數值越高，越少命令會被視為大量/高風險。",
                        "help_i18n": "config.egBulkThresholdDesc",
                        "min": 1,
                        "max": 10,
                        "step": 1,
                    },
                    {
                        "path": "execution_guard.require_plan_for_high_risk",
                        "type": "boolean",
                        "label": "高風險工具先做計畫檢查",
                        "label_i18n": "config.egPlanCheck",
                        "help": "執行 HIGH/CRITICAL 工具前，先要求模型產生安全計畫。",
                        "help_i18n": "config.egPlanCheckDesc",
                    },
                    {
                        "path": "execution_guard.plan_model",
                        "type": "model",
                        "model_mode": "plain",
                        "label": "計畫檢查模型",
                        "label_i18n": "config.egPlanModel",
                        "help": "用於高風險計畫檢查的模型。留空 = 使用目前模型。",
                        "help_i18n": "config.egPlanModelDesc",
                    },
                    {
                        "path": "execution_guard.plan_max_tokens",
                        "type": "number",
                        "label": "計畫最大 Tokens",
                        "label_i18n": "config.egPlanMaxTokens",
                        "help": "高風險計畫生成時的 Token 上限。",
                        "help_i18n": "config.egPlanMaxTokensDesc",
                        "min": 64,
                        "max": 1000,
                        "step": 16,
                    },
                ],
            },
            {
                "id": "context-compression",
                "category": "memory",
                "title": "Context Compression",
                "title_i18n": "config.contextCompression",
                "order": 20,
                "fields": [
                    {
                        "path": "context_compression.enabled",
                        "type": "boolean",
                        "label_i18n": "config.enabled",
                        "help_i18n": "config.ccDesc",
                    },
                    {
                        "path": "context_compression.token_budget",
                        "type": "number",
                        "label_i18n": "config.tokenBudget",
                        "help_i18n": "config.tokenBudgetDesc",
                        "min": 10000,
                        "max": 1000000,
                        "step": 10000,
                    },
                    {
                        "path": "context_compression.trigger_threshold",
                        "type": "number",
                        "label_i18n": "config.triggerThreshold",
                        "help_i18n": "config.triggerThresholdDesc",
                        "min": 0.5,
                        "max": 0.95,
                        "step": 0.05,
                    },
                    {
                        "path": "context_compression.keep_recent_turns",
                        "type": "number",
                        "label_i18n": "config.keepRecent",
                        "help_i18n": "config.keepRecentDesc",
                        "min": 3,
                        "max": 50,
                        "step": 1,
                    },
                    {
                        "path": "context_compression.summarize_model",
                        "type": "model",
                        "model_mode": "plain",
                        "label_i18n": "config.summarizeModel",
                        "help_i18n": "config.summarizeModelDesc",
                    },
                    {
                        "path": "context_compression.extract_facts",
                        "type": "boolean",
                        "label_i18n": "config.extractFacts",
                        "help_i18n": "config.extractFactsDesc",
                    },
                ],
            },
            {
                "id": "memory-lifecycle",
                "category": "memory",
                "title": "Memory Lifecycle",
                "title_i18n": "config.memoryLifecycle",
                "order": 30,
                "fields": [
                    {
                        "path": "memory_lifecycle.enabled",
                        "type": "boolean",
                        "label_i18n": "config.enabled",
                        "help_i18n": "config.mlDesc",
                    },
                    {
                        "path": "memory_lifecycle.decay_lambda",
                        "type": "number",
                        "label_i18n": "config.decayRate",
                        "help_i18n": "config.decayRateDesc",
                        "min": 0.001,
                        "max": 0.1,
                        "step": 0.001,
                    },
                    {
                        "path": "memory_lifecycle.prune_threshold",
                        "type": "number",
                        "label_i18n": "config.pruneThreshold",
                        "help_i18n": "config.pruneThresholdDesc",
                        "min": 0.01,
                        "max": 0.5,
                        "step": 0.01,
                    },
                    {
                        "path": "memory_lifecycle.consolidation_distance",
                        "type": "number",
                        "label_i18n": "config.consolidationDistance",
                        "help_i18n": "config.consolidationDistanceDesc",
                        "min": 0.05,
                        "max": 0.5,
                        "step": 0.01,
                    },
                    {
                        "path": "memory_lifecycle.daily_maintenance_time",
                        "type": "text",
                        "label_i18n": "config.dailyTime",
                        "help_i18n": "config.dailyTimeDesc",
                        "placeholder": "03:00",
                    },
                    {
                        "path": "memory_lifecycle.memory_md_max_lines",
                        "type": "number",
                        "label_i18n": "config.mdMaxLines",
                        "help_i18n": "config.mdMaxLinesDesc",
                        "min": 50,
                        "max": 1000,
                        "step": 1,
                    },
                    {
                        "path": "memory_lifecycle.pin_user_memories",
                        "type": "boolean",
                        "label_i18n": "config.pinUserMemories",
                        "help_i18n": "config.pinUserMemoriesDesc",
                    },
                ],
            },
            {
                "id": "learning",
                "category": "memory",
                "title": "Experience Learning",
                "title_i18n": "config.experienceLearning",
                "order": 40,
                "fields": [
                    {
                        "path": "learning.enabled",
                        "type": "boolean",
                        "label_i18n": "config.enabled",
                        "help_i18n": "config.leDesc",
                    },
                    {
                        "path": "learning.max_lessons",
                        "type": "number",
                        "label_i18n": "config.maxLessons",
                        "help_i18n": "config.maxLessonsDesc",
                        "min": 5,
                        "max": 100,
                        "step": 1,
                    },
                    {
                        "path": "learning.inject_top_k",
                        "type": "number",
                        "label_i18n": "config.injectTopK",
                        "help_i18n": "config.injectTopKDesc",
                        "min": 1,
                        "max": 20,
                        "step": 1,
                    },
                    {
                        "path": "learning.error_threshold",
                        "type": "number",
                        "label_i18n": "config.errorThreshold",
                        "help_i18n": "config.errorThresholdDesc",
                        "min": 1,
                        "max": 10,
                        "step": 1,
                    },
                    {
                        "path": "learning.analysis_time",
                        "type": "text",
                        "label_i18n": "config.analysisTime",
                        "help_i18n": "config.analysisTimeDesc",
                        "placeholder": "04:00",
                    },
                    {
                        "path": "learning.track_model_performance",
                        "type": "boolean",
                        "label_i18n": "config.trackModelPerf",
                        "help_i18n": "config.trackModelPerfDesc",
                    },
                ],
            },
            {
                "id": "code-feedback",
                "category": "intelligence",
                "title": "Code Feedback Loop",
                "title_i18n": "config.codeFeedback",
                "order": 50,
                "fields": [
                    {
                        "path": "code_feedback.enabled",
                        "type": "boolean",
                        "label_i18n": "config.enabled",
                        "help_i18n": "config.cfDesc",
                    },
                    {
                        "path": "code_feedback.lint_on_write",
                        "type": "boolean",
                        "label_i18n": "config.lintOnWrite",
                        "help_i18n": "config.lintOnWriteDesc",
                    },
                    {
                        "path": "code_feedback.type_check_on_write",
                        "type": "boolean",
                        "label_i18n": "config.typeCheck",
                        "help_i18n": "config.typeCheckDesc",
                    },
                    {
                        "path": "code_feedback.test_on_write",
                        "type": "boolean",
                        "label_i18n": "config.testOnWrite",
                        "help_i18n": "config.testOnWriteDesc",
                    },
                    {
                        "path": "code_feedback.max_auto_fix_rounds",
                        "type": "number",
                        "label_i18n": "config.maxAutoFix",
                        "help_i18n": "config.maxAutoFixDesc",
                        "min": 1,
                        "max": 10,
                        "step": 1,
                    },
                ],
            },
            {
                "id": "vision",
                "category": "vision",
                "title": "Vision / Image Analysis",
                "title_i18n": "config.visionAnalysis",
                "order": 60,
                "fields": [
                    {
                        "path": "vision.image_analysis_mode",
                        "type": "select",
                        "label_i18n": "config.analysisMode",
                        "help_i18n": "config.analysisModeDesc",
                        "options": [
                            {"value": "auto", "label_i18n": "config.viModeAuto"},
                            {"value": "always", "label_i18n": "config.viModeAlways"},
                            {"value": "disabled", "label_i18n": "config.viModeDisabled"},
                        ],
                    },
                    {
                        "path": "vision.fallback_format",
                        "type": "select",
                        "label_i18n": "config.fallbackFormat",
                        "help_i18n": "config.fallbackFormatDesc",
                        "options": [
                            {"value": "text", "label_i18n": "config.viFormatText"},
                            {"value": "svg", "label_i18n": "config.viFormatSvg"},
                        ],
                    },
                    {
                        "path": "vision.fallback_detail_level",
                        "type": "select",
                        "label_i18n": "config.detailLevel",
                        "help_i18n": "config.detailLevelDesc",
                        "options": [
                            {"value": "brief", "label_i18n": "config.viDetailBrief"},
                            {"value": "standard", "label_i18n": "config.viDetailStandard"},
                            {"value": "detailed", "label_i18n": "config.viDetailDetailed"},
                        ],
                    },
                    {
                        "path": "vision.grid_size",
                        "type": "number",
                        "label_i18n": "config.gridSize",
                        "help_i18n": "config.gridSizeDesc",
                        "min": 2,
                        "max": 8,
                        "step": 1,
                    },
                    {
                        "path": "vision.max_elements",
                        "type": "number",
                        "label_i18n": "config.maxElements",
                        "help_i18n": "config.maxElementsDesc",
                        "min": 10,
                        "max": 200,
                        "step": 1,
                    },
                    {
                        "path": "vision.vision_models",
                        "type": "csv",
                        "label_i18n": "config.visionModels",
                        "help_i18n": "config.visionModelsDesc",
                    },
                    {
                        "path": "vision.text_only_models",
                        "type": "csv",
                        "label_i18n": "config.textOnlyModels",
                        "help_i18n": "config.textOnlyModelsDesc",
                    },
                ],
            },
            {
                "id": "diagnostics",
                "category": "diagnostics",
                "title": "Diagnostics & Self-repair",
                "title_i18n": "config.diagnostics",
                "order": 70,
                "fields": [
                    {
                        "path": "diagnostics.enabled",
                        "type": "boolean",
                        "label_i18n": "config.enabled",
                        "help_i18n": "config.diagDesc",
                    },
                    {
                        "path": "diagnostics.repair_model",
                        "type": "model",
                        "model_mode": "auth",
                        "include_main": True,
                        "label_i18n": "config.diagRepairModel",
                        "help_i18n": "config.diagRepairModelDesc",
                    },
                    {
                        "path": "diagnostics.auto_diagnose_on_error",
                        "type": "boolean",
                        "label_i18n": "config.diagAutoDiagnose",
                        "help_i18n": "config.diagAutoDiagnoseDesc",
                    },
                    {
                        "path": "diagnostics.error_threshold",
                        "type": "number",
                        "label_i18n": "config.errorThreshold",
                        "help_i18n": "config.errorThresholdDesc",
                        "min": 1,
                        "max": 20,
                        "step": 1,
                    },
                    {
                        "path": "diagnostics.include_in_agents",
                        "type": "boolean",
                        "label_i18n": "config.diagIncludeAgents",
                        "help_i18n": "config.diagIncludeAgentsDesc",
                    },
                    {
                        "path": "diagnostics.only_matching_model",
                        "type": "boolean",
                        "label_i18n": "config.diagMatchingModel",
                        "help_i18n": "config.diagMatchingModelDesc",
                    },
                ],
            },
            {
                "id": "task-complexity",
                "category": "intelligence",
                "title": "Task Complexity",
                "title_i18n": "config.taskComplexity",
                "order": 80,
                "fields": [
                    {
                        "path": "task_complexity.enabled",
                        "type": "boolean",
                        "label_i18n": "config.enabled",
                        "help_i18n": "config.tcDesc",
                    },
                    {
                        "path": "task_complexity.trigger_mode",
                        "type": "select",
                        "label_i18n": "config.triggerMode",
                        "help_i18n": "config.triggerModeDesc",
                        "options": [
                            {"value": "auto", "label_i18n": "config.triggerAuto"},
                            {"value": "manual", "label_i18n": "config.triggerManual"},
                            {"value": "auto_silent", "label_i18n": "config.triggerSilent"},
                        ],
                    },
                    {
                        "path": "task_complexity.llm_refinement",
                        "type": "boolean",
                        "label_i18n": "config.llmRefinement",
                        "help_i18n": "config.llmRefinementDesc",
                    },
                    {
                        "path": "task_complexity.refinement_model",
                        "type": "model",
                        "model_mode": "plain",
                        "label_i18n": "config.refinementModel",
                        "help_i18n": "config.refinementModelDesc",
                    },
                    {
                        "path": "task_complexity.fast_model",
                        "type": "model",
                        "model_mode": "plain",
                        "label_i18n": "config.fastModel",
                        "help_i18n": "config.fastModelDesc",
                    },
                    {
                        "path": "task_complexity.standard_model",
                        "type": "model",
                        "model_mode": "plain",
                        "label_i18n": "config.standardModel",
                        "help_i18n": "config.standardModelDesc",
                    },
                    {
                        "path": "task_complexity.frontier_model",
                        "type": "model",
                        "model_mode": "plain",
                        "label_i18n": "config.frontierModel",
                        "help_i18n": "config.frontierModelDesc",
                    },
                    {
                        "path": "task_complexity.decomposition_enabled",
                        "type": "boolean",
                        "label_i18n": "config.decomposition",
                        "help_i18n": "config.decompositionDesc",
                    },
                    {
                        "path": "task_complexity.decomposition_threshold",
                        "type": "number",
                        "label_i18n": "config.decomposeThreshold",
                        "help_i18n": "config.decomposeThresholdDesc",
                        "min": 0.5,
                        "max": 1.0,
                        "step": 0.05,
                    },
                    {
                        "path": "task_complexity.max_subtasks",
                        "type": "number",
                        "label_i18n": "config.maxSubtasks",
                        "help_i18n": "config.maxSubtasksDesc",
                        "min": 2,
                        "max": 10,
                        "step": 1,
                    },
                    {
                        "path": "task_complexity.parallel_subtasks",
                        "type": "boolean",
                        "label_i18n": "config.parallelSubtasks",
                        "help_i18n": "config.parallelSubtasksDesc",
                    },
                    {
                        "path": "task_complexity.ml_model_enabled",
                        "type": "boolean",
                        "label_i18n": "config.mlEnabled",
                        "help_i18n": "config.mlEnabledDesc",
                    },
                    {
                        "path": "task_complexity.ml_estimation_mode",
                        "type": "select",
                        "label_i18n": "config.mlMode",
                        "help_i18n": "config.mlModeDesc",
                        "options": [
                            {"value": "hybrid", "label_i18n": "config.mlModeHybrid"},
                            {"value": "ml_only", "label_i18n": "config.mlModeOnly"},
                            {"value": "ml_refine", "label_i18n": "config.mlModeRefine"},
                        ],
                    },
                    {
                        "path": "task_complexity.ml_model_path",
                        "type": "text",
                        "label_i18n": "config.mlModelPath",
                        "help_i18n": "config.mlModelPathDesc",
                        "placeholder": "models/complexity_model_int8.onnx",
                    },
                    {
                        "path": "task_complexity.ml_tokenizer_path",
                        "type": "text",
                        "label_i18n": "config.mlTokenizerPath",
                        "help_i18n": "config.mlTokenizerPathDesc",
                        "placeholder": "models/complexity_tokenizer/",
                    },
                ],
            },
            {
                "id": "delegation-complexity",
                "category": "agents",
                "title": "Delegation Complexity",
                "title_i18n": "config.delegationComplexity",
                "description_i18n": "config.delegationComplexityDesc",
                "order": 90,
                "fields": [
                    {
                        "path": "delegation_complexity.enabled",
                        "type": "boolean",
                        "label_i18n": "config.enabled",
                        "help_i18n": "config.delegationComplexityDesc",
                    },
                    {
                        "path": "delegation_complexity.default_use_complexity",
                        "type": "boolean",
                        "label_i18n": "config.delegationDefaultUse",
                        "help_i18n": "config.delegationDefaultUseDesc",
                    },
                    {
                        "path": "delegation_complexity.allow_auto_select",
                        "type": "boolean",
                        "label_i18n": "config.delegationAutoSelect",
                        "help_i18n": "config.delegationAutoSelectDesc",
                    },
                    {
                        "path": "delegation_complexity.enforce_min_tier",
                        "type": "boolean",
                        "label_i18n": "config.delegationEnforceTier",
                        "help_i18n": "config.delegationEnforceTierDesc",
                    },
                    {
                        "path": "delegation_complexity.tier_models.trivial",
                        "type": "model",
                        "model_mode": "auth",
                        "label_i18n": "config.delegationModelTrivial",
                        "help_i18n": "config.delegationModelTrivialDesc",
                    },
                    {
                        "path": "delegation_complexity.tier_models.simple",
                        "type": "model",
                        "model_mode": "auth",
                        "label_i18n": "config.delegationModelSimple",
                        "help_i18n": "config.delegationModelSimpleDesc",
                    },
                    {
                        "path": "delegation_complexity.tier_models.moderate",
                        "type": "model",
                        "model_mode": "auth",
                        "label_i18n": "config.delegationModelModerate",
                        "help_i18n": "config.delegationModelModerateDesc",
                    },
                    {
                        "path": "delegation_complexity.tier_models.complex",
                        "type": "model",
                        "model_mode": "auth",
                        "label_i18n": "config.delegationModelComplex",
                        "help_i18n": "config.delegationModelComplexDesc",
                    },
                    {
                        "path": "delegation_complexity.tier_boundaries.trivial",
                        "type": "number",
                        "label_i18n": "config.delegationThresholdTrivial",
                        "help_i18n": "config.delegationThresholdTrivialDesc",
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                    },
                    {
                        "path": "delegation_complexity.tier_boundaries.simple",
                        "type": "number",
                        "label_i18n": "config.delegationThresholdSimple",
                        "help_i18n": "config.delegationThresholdSimpleDesc",
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                    },
                    {
                        "path": "delegation_complexity.tier_boundaries.moderate",
                        "type": "number",
                        "label_i18n": "config.delegationThresholdModerate",
                        "help_i18n": "config.delegationThresholdModerateDesc",
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                    },
                    {
                        "path": "delegation_complexity.tier_boundaries.complex",
                        "type": "number",
                        "label_i18n": "config.delegationThresholdComplex",
                        "help_i18n": "config.delegationThresholdComplexDesc",
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                    },
                ],
            },
            {
                "id": "mcp-bridge",
                "category": "integrations",
                "title": "MCP Integration",
                "title_i18n": "config.mcpTitle",
                "description": "Connect external MCP servers and choose which tools to enable.",
                "description_i18n": "config.mcpDesc",
                "order": 100,
                "fields": [
                    {
                        "path": "mcp.enabled",
                        "type": "boolean",
                        "label": "Enable MCP Bridge",
                        "label_i18n": "config.mcpEnabled",
                        "help": "When disabled, no MCP servers are connected and no MCP tools are exposed.",
                        "help_i18n": "config.mcpEnabledDesc",
                    },
                    {
                        "path": "mcp.servers",
                        "type": "mcp_servers",
                        "label": "MCP Servers",
                        "label_i18n": "config.mcpServers",
                        "help": "Configure server command/env and choose enabled tools per server.",
                        "help_i18n": "config.mcpServersDesc",
                    },
                ],
            },
        ],
    }
