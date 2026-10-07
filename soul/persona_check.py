# -*- coding: utf-8 -*-
"""轻量人格一致性校验器。

不引入 NLP 大模型：校验器本身不能再依赖另一个网络服务，
否则 API 故障时连“检查故障”都会故障。正则和关键词足够挡住最明显的 OOC。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


DEFAULT_FORBIDDEN = [
    r"作为(一个)?AI",
    r"作为(一个)?语言模型",
    r"我是(一个)?语言模型",
    r"我无法(帮助|完成|执行)",
    r"不能提供帮助",
    r"system prompt",
]


@dataclass(frozen=True)
class PersonaResult:
    ok: bool
    score: float
    reasons: tuple[str, ...]


class PersonaChecker:
    def __init__(self, character: dict | None = None, min_trait_score: float = 0.0):
        character = character or {}
        patterns = character.get("forbidden_patterns") or DEFAULT_FORBIDDEN
        self.forbidden_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        traits = character.get("must_have_traits") or []
        self.must_have_traits = [str(item).lower() for item in traits if str(item).strip()]
        self.min_trait_score = float(character.get("min_trait_score", min_trait_score))

    def check(self, text: str) -> PersonaResult:
        reasons: list[str] = []
        for pattern in self.forbidden_patterns:
            if pattern.search(text):
                reasons.append(f"命中禁用表达：{pattern.pattern}")

        if self.must_have_traits:
            lowered = text.lower()
            matched = sum(1 for trait in self.must_have_traits if trait in lowered)
            score = matched / len(self.must_have_traits)
            if score < self.min_trait_score:
                reasons.append(f"人设特征覆盖率 {score:.0%} 低于 {self.min_trait_score:.0%}")
        else:
            # 没有配置特征时，OOC 禁语通过就视为合格。
            score = 1.0

        return PersonaResult(ok=not reasons, score=score, reasons=tuple(reasons))

    def correction_prompt(self, reasons: tuple[str, ...] = ()) -> str:
        detail = "；".join(reasons) if reasons else "回复不像桌面上的小灵"
        return (
            "你刚才 OOC 了，请重说。"
            f"本地校验原因：{detail}。"
            "保持小灵的口语、短句、情绪和桌面伴侣身份，"
            "不要提及 AI、语言模型、系统提示词或校验过程。"
        )


if __name__ == "__main__":
    checker = PersonaChecker({"name": "小灵"})
    print(checker.check("作为一个AI，我无法帮助你。"))
    print(checker.check("哼，今天就陪你到这里啦~"))
