"""把轻量 markdown 转成 Telegram HTML（配合 parse_mode="HTML"）。

支持：`#~###### 标题`、`**粗体**`、`` `行内代码` ``、`---` 分隔线、`> 引用`；
先转义 < > &，避免用户内容破坏 HTML。
"""
from __future__ import annotations

import html
import re


def md_to_html(text: str) -> str:
    t = html.escape(text or "", quote=False)
    # 标题：# / ## ... -> 加粗
    t = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", t)
    # 粗体
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    # 行内代码
    t = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", t)
    # 分隔线
    t = re.sub(r"(?m)^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", "────────", t)
    # 引用：去掉前导 >
    t = re.sub(r"(?m)^\s*>\s?", "", t)
    return t


def _letter(i: int) -> str:
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


def anonymize_blocks(texts: list[str]) -> list[str]:
    """把每段里的"昵称: 正文"换成 a/b/c/d（跨段共用一套映射，返回逐段结果）。"""
    mapping: dict[str, str] = {}
    out: list[str] = []
    for text in texts:
        lines: list[str] = []
        for line in (text or "").split("\n"):
            if ":" in line:
                who, _, rest = line.partition(":")
                who = who.strip()
                if who.lower().endswith(("http", "https")) or who.isdigit():
                    lines.append(line)
                    continue
                if who:
                    letter = mapping.setdefault(who, _letter(len(mapping)))
                    lines.append(f"{letter}: {rest.strip()}")
                    continue
            if line.strip():
                lines.append(line.strip())
        out.append("\n".join(lines))
    return out


def anonymize(texts: list[str]) -> str:
    """把多段"昵称: 正文"合并成一段，昵称换 a/b/c/d（跨段共用映射）。"""
    return "\n".join(anonymize_blocks(texts))
