"""保守的作品同名判断：只统一空白、全半角和明确写出的季号。"""
from __future__ import annotations

import re
import unicodedata


_CHINESE_NUMBER = r"(?:[一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九])"
_SEASON_SUFFIX = re.compile(
    rf"(?:\s*第\s*([0-9]{{1,3}}|{_CHINESE_NUMBER})\s*季|\s+(?:season\s*|s)([0-9]{{1,3}}))$",
    re.I,
)


def _title_key(value: str) -> tuple[str, int]:
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    season = 1
    match = _SEASON_SUFFIX.search(text)
    if match:
        number = match[1] or match[2]
        if number.isascii() and number.isdigit():
            parsed = int(number)
        else:
            digits = dict(zip("一二两三四五六七八九", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
            if "十" in number:
                tens, _, units = number.partition("十")
                parsed = digits.get(tens, 1) * 10 + digits.get(units, 0)
            else:
                parsed = digits[number]
        if parsed > 0:
            text, season = text[:match.start()], parsed
    return re.sub(r"\s+", "", text), season


def same_title(a: str, b: str) -> bool:
    """无季后缀按第一季比对；续季、剧场版和其他后缀均保留边界。

    本函数只判断同名，不决定预选。调用方应仅在同一来源恰有一个
    同名候选时预选，多个相同名称的不同条目仍需用户判断。
    """
    left, right = _title_key(a), _title_key(b)
    return bool(left[0] and right[0]) and left == right
