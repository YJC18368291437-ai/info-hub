"""规则层（粗筛）：@主号 / @全体成员 / 通知关键词 / 链接抽取。

规则表 v1.1 优先级（先命中先定）：
  1 @到用户主号                 -> at_me
  2 通知特征(含带@全体成员的通知) -> notice
  3 非通知类的 @全体成员          -> at_me
  4+ 计算机/生科/校园/链接/闲聊   -> 交给 LLM(classify) 判定
"""
from __future__ import annotations

import re
from typing import Any

from info_hub.settings import Settings

# 哪些话题"带语境"时算链接已被承接（无需进 🔗 兜底）
CONTEXT_TOPICS = {"cs", "bio", "notice", "at_me"}


class Rules:
    def __init__(self, settings: Settings):
        self.settings = settings
        r = settings.rules or {}
        self.notice_keywords = [str(k) for k in (r.get("notice_keywords") or [])]
        self.at_all_markers = [str(k) for k in (r.get("at_all_markers") or ["@全体成员"])]
        self.notice_image_groups = {str(g) for g in (r.get("notice_image_groups") or [])}
        self.school_notice_keywords = [str(k) for k in (r.get("school_notice_keywords") or [])]
        self.link_re = re.compile(r.get("link_regex") or r"https?://\S+")

    def image_forces_notice(self, group_id: str) -> bool:
        """发布类群（如"某校消息"）：含图块一律判 📢通知。"""
        return str(group_id) in self.notice_image_groups

    def extract_links(self, text: str) -> list[str]:
        return self.link_re.findall(text or "")

    def has_notice(self, text: str) -> bool:
        t = text or ""
        return any(k in t for k in self.notice_keywords)

    def has_school_notice(self, text: str) -> bool:
        keywords = self.school_notice_keywords or self.notice_keywords
        t = text or ""
        return any(k in t for k in keywords)

    def text_has_at_all(self, text: str) -> bool:
        t = text or ""
        return any(m in t for m in self.at_all_markers)

    def coarse_topic(self, msg: dict) -> str | None:
        """返回强制话题 key（无法硬判则 None，交给 LLM）。

        优先级（用户定，2026-10-12）：
          含 @全体成员 -> 📢通知（一律）
          通知特征     -> 📢通知
          @到主号      -> 🔔@我（无 @全体成员 时）
        """
        text = msg.get("text") or ""
        if msg.get("at_all"):
            return "notice"
        if msg.get("at_me"):
            return "at_me"
        return None

    def link_fallback_topic(self, block_topic: str | None, links: list[str]) -> str | None:
        """链接全覆盖：块里有链接、但话题不承接链接 -> 兜底给 🔗。"""
        if not links:
            return None
        if block_topic in CONTEXT_TOPICS or block_topic == 'needs_review':
            return None
        return "link"


_rules: Rules | None = None


def get_rules() -> Rules:
    global _rules
    if _rules is None:
        from info_hub.settings import get_settings

        _rules = Rules(get_settings())
    return _rules


def message_to_text(text: str, links: list[str]) -> str:
    """展示文本（原样照抄 + 补全被截断的链接）。"""
    t = text or ""
    missing = [l for l in links if l not in t]
    if missing:
        t = (t + " " + " ".join(missing)).strip()
    return t


def source_header(group_name: str, nickname: str, ts: int) -> str:
    import datetime as _dt

    when = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else _dt.datetime.now().strftime("%m-%d %H:%M")
    if nickname:
        return f"[{group_name}] {nickname} · {when}"
    return f"[{group_name}] · {when}"
