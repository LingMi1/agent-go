"""Skill 体系：SKILL.md 加载、渐进式披露（L1/L2/L3）、沙箱路径校验、脚本运行器。

与 SOP 正交：SOP 是规划期提示词注入，Skill 是执行期工具。纯逻辑
（frontmatter / sandbox / disclosure / runner.request）与 I/O（registry / tools / runner.docker）分离。
"""


class SkillError(Exception):
    """Skill 相关错误基类。"""


class SkillLoadError(SkillError):
    """SKILL.md 加载/解析失败。"""


class SkillSandboxError(SkillError):
    """路径越出沙箱允许范围。"""
