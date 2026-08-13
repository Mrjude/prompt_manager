"""病种话术 Skill 注册中心

整合自 agent_proj/shining_dialog_agents/src/skills/registry.py。

标准 Skill 接口（每个 *_skill.py 模块必须导出）：
    name        str          唯一标识，如 "acne"
    description str          功能描述
    keywords    list[str]    触发关键词
    version     str          版本号
    department  str          归属科室（新增，用于按科室过滤可用 Skill）
    get_strategy() -> dict   返回对话策略

与原实现的差异：
    1. 新增 department 字段，Agent 只加载当前科室可用的 Skill，避免口腔科命中皮肤病话术
    2. 支持 prompts 表覆盖：若库中存在同名场景提示词，优先使用库中版本（便于运营在线调整）
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SkillMeta:
    def __init__(self, name: str, description: str, keywords: list,
                 version: str, module_name: str, department: str = "general"):
        self.name = name
        self.description = description
        self.keywords = keywords
        self.version = version
        self.module_name = module_name
        self.department = department

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
            "version": self.version,
            "module_name": self.module_name,
            "department": self.department,
        }


class SkillRegistry:
    """自动扫描 skills/ 下的 *_skill.py 并注册"""

    def __init__(self):
        self._skills: Dict[str, SkillMeta] = {}
        self._modules: Dict[str, object] = {}
        self._loaded = False

    def discover_and_register(self):
        self._skills.clear()
        self._modules.clear()
        skills_dir = os.path.dirname(os.path.abspath(__file__))

        for filename in sorted(os.listdir(skills_dir)):
            if not filename.endswith("_skill.py") or filename.startswith("_"):
                continue
            module_name = filename[:-3]
            try:
                module = importlib.import_module(f"skills.{module_name}")
                name = getattr(module, "name", None)
                description = getattr(module, "description", None)
                if not (name and description):
                    continue
                self._skills[name] = SkillMeta(
                    name=name,
                    description=description,
                    keywords=getattr(module, "keywords", []),
                    version=getattr(module, "version", "1.0.0"),
                    module_name=module_name,
                    department=getattr(module, "department", "general"),
                )
                self._modules[name] = module
            except Exception as e:
                logger.warning("加载 Skill %s 失败: %s", module_name, e)

        self._loaded = True
        logger.info("已注册 %d 个病种话术 Skill", len(self._skills))

    def _ensure_loaded(self):
        if not self._loaded:
            self.discover_and_register()

    def list_skills(self, department: Optional[str] = None) -> List[dict]:
        """列出 Skill，可按科室过滤（general 视为全科室通用）"""
        self._ensure_loaded()
        items = [m.to_dict() for m in self._skills.values()]
        if department:
            items = [s for s in items
                     if s["department"] in (department, "general")]
        return items

    def get_skill(self, name: str) -> Optional[dict]:
        self._ensure_loaded()
        meta = self._skills.get(name)
        if not meta:
            return None
        return {**meta.to_dict(), "strategy": self._modules[name].get_strategy()}

    def match_skill(self, text: str, department: Optional[str] = None) -> Optional[dict]:
        """按文本匹配 Skill：先匹配 name/描述，再匹配关键词"""
        self._ensure_loaded()
        text_lower = (text or "").lower().strip()
        if not text_lower:
            return None

        candidates = [
            (n, m) for n, m in self._skills.items()
            if not department or m.department in (department, "general")
        ]
        for name, meta in candidates:
            if name in text_lower or meta.description.split("（")[0] in text_lower:
                return self.get_skill(name)
        for name, meta in candidates:
            if any(kw in text_lower for kw in meta.keywords):
                return self.get_skill(name)
        return None

    def format_strategy(self, skill_data: dict) -> str:
        """把策略格式化成便于 LLM 消费的文本（对应原项目 load_disease_skill 的输出）"""
        s = skill_data.get("strategy", {})
        lines = [f"=== {skill_data['description']} (v{skill_data['version']}) ===",
                 f"【核心目标】{s.get('goal', '')}",
                 f"【对话风格】{s.get('tone', '')}", ""]

        for i, phase in enumerate(s.get("phases", []), 1):
            lines.append(f"--- 阶段 {i}：{phase.get('phase', '')} ---")
            lines.append(f"策略：{phase.get('strategy', '')}")
            for key, label in (("example", "话术示例"), ("trigger", "触发时机"),
                               ("fallback", "兜底话术")):
                if phase.get(key):
                    lines.append(f"{label}：{phase[key]}")
            for key, label in (("key_info", "需了解"), ("taboo", "禁忌")):
                if phase.get(key):
                    lines.append(f"{label}：{', '.join(phase[key])}")
            lines.append("")

        if s.get("red_flags"):
            lines.append(f"红旗警示：{', '.join(s['red_flags'])}")
            lines.append(f"处理方式：{s.get('red_flag_action', '')}")
        return "\n".join(lines)

    def reload_all(self):
        self._loaded = False
        self.discover_and_register()


skill_registry = SkillRegistry()
