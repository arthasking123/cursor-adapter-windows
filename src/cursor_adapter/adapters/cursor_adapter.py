from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from ..utils.io import write_text


@dataclass(frozen=True)
class CursorPromptPackPaths:
    root: Path
    prompts_dir: Path
    scripts_dir: Path


def _build_prompt_templates() -> Dict[str, str]:
    return {
        "01_parser_prompt.md": """# 阶段1：结构化解析（Parser）

请阅读 `docs/output/白皮书_合并版.md`，生成 `docs/PROJECT_BRIEF.md`。

要求：
- 包含：Project Goal、Target User、Core Features (MVP 3-5)、Data Entities、Technical Constraints、NFR
- 增加 Assumptions 与 Open Questions
- 每个 MVP 功能给出至少 1 条 Given/When/Then 验收标准
- 输出为可直接评审的 Markdown
""",
        "02_specifier_prompt.md": """# 阶段2：技术规格生成（Specifier）

基于 `@docs/PROJECT_BRIEF.md` 生成 `docs/TECH_SPEC.md` 与 `docs/TRACEABILITY.md`。

要求：
- TECH_SPEC 包含：文件树、DB Schema、API Contract、组件层级、状态管理、测试策略
- TRACEABILITY 包含：requirement_id -> api/page/test/source_section 映射
- API 命名和响应结构稳定，便于后续直接生成代码
""",
        "03_scaffolder_prompt.md": """# 阶段3：脚手架初始化（Scaffolder）

读取 `@docs/TECH_SPEC.md`，按 File Structure 创建目录与占位文件。

同时生成：
- `package.json`
- `requirements.txt`
- `docker-compose.yml`
- `.env.example`

完成后给出目录校验命令与结果说明。
""",
        "04_builder_prompt.md": """# 阶段4：模块化代码生成（Builder）

基于 `@docs/TECH_SPEC.md` 完成代码实现，顺序：
1. 数据层（模型、迁移、约束）
2. 后端层（路由、服务、校验、错误封装）
3. 前端层（页面、组件、API Client、异常态）

要求：
- 不使用 mock/todo 占位
- 每层完成后提供最小验证方式
""",
        "05_integrator_prompt.md": """# 阶段5：自驱集成与测试（Integrator）

启动项目并执行：
- lint
- typecheck
- unit/integration/e2e（若已配置）

出现错误时：
1) 给出根因
2) 修改相关文件修复
3) 提供复验命令

循环直到主链路可运行。
""",
        "06_feature_audit_prompt.md": """# MVP 覆盖审计

请对照 `@docs/PROJECT_BRIEF.md` 检查当前代码。

输出：
- 已实现 MVP
- 未实现/部分实现 MVP
- 需要补做的具体文件与函数
""",
    }


def _build_runner_ps1() -> str:
    return """Param(
  [string]$ProjectRoot = "."
)

$ErrorActionPreference = "Stop"
Write-Host "[cursor-adapter] project root: $ProjectRoot"
Write-Host "[cursor-adapter] prompts: $ProjectRoot\\cursor_prompts\\prompts"
Write-Host ""
Write-Host "请按顺序在 Cursor Chat/Composer 执行："
Write-Host "1) prompts/01_parser_prompt.md"
Write-Host "2) prompts/02_specifier_prompt.md"
Write-Host "3) prompts/03_scaffolder_prompt.md"
Write-Host "4) prompts/04_builder_prompt.md"
Write-Host "5) prompts/05_integrator_prompt.md"
Write-Host "6) prompts/06_feature_audit_prompt.md"
Write-Host ""
Write-Host "建议每一步完成后提交一次变更记录到 docs/CHANGELOG_AI.md"
"""


def _build_runner_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${1:-.}"

echo "[cursor-adapter] project root: ${PROJECT_ROOT}"
echo "[cursor-adapter] prompts: ${PROJECT_ROOT}/cursor_prompts/prompts"
echo
echo "请按顺序在 Cursor Chat/Composer 执行："
echo "1) prompts/01_parser_prompt.md"
echo "2) prompts/02_specifier_prompt.md"
echo "3) prompts/03_scaffolder_prompt.md"
echo "4) prompts/04_builder_prompt.md"
echo "5) prompts/05_integrator_prompt.md"
echo "6) prompts/06_feature_audit_prompt.md"
echo
echo "建议每一步完成后提交一次变更记录到 docs/CHANGELOG_AI.md"
"""


def create_cursor_prompt_pack(project_root: Path, workspace_dir: Path) -> CursorPromptPackPaths:
    root = workspace_dir / "cursor_prompts"
    prompts_dir = root / "prompts"
    scripts_dir = root / "scripts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    for filename, content in _build_prompt_templates().items():
        write_text(prompts_dir / filename, content)

    write_text(scripts_dir / "run_cursor_prompts.ps1", _build_runner_ps1())
    write_text(scripts_dir / "run_cursor_prompts.sh", _build_runner_sh())

    write_text(
        root / "README.md",
        (
            "# Cursor Prompt Pack\n\n"
            "该目录由生成器自动产生，用于在 Cursor 中按阶段执行工程生成流程。\n\n"
            "## 目录\n"
            "- `prompts/`：阶段化 Prompt 模板\n"
            "- `scripts/`：执行顺序提示脚本（PowerShell/Bash）\n\n"
            "## 使用方式\n"
            "1. 打开 Cursor\n"
            "2. 依次复制 `prompts/` 下文件内容到 Chat/Composer\n"
            "3. 每个阶段完成后更新 `docs/CHANGELOG_AI.md`\n"
        ),
    )

    return CursorPromptPackPaths(root=root, prompts_dir=prompts_dir, scripts_dir=scripts_dir)

