"""报纸邮件：通知报（8/12/18 + 紧急课程）与技术情报报（18:00，24h 强制）。"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import html as _h
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from info_hub.db import Database
from info_hub.settings import Settings
from info_hub.textfmt import anonymize

logger = logging.getLogger("info_hub.newspaper")

TZ = ZoneInfo("Asia/Shanghai")
IMAGE_FRESH_SECONDS = 30 * 60
MEDIA_CACHE_MAX_AGE = 30 * 24 * 3600
NOTICE_MAX_IMAGES = 9
NOTICE_IMAGE_MAX_BYTES = 10 * 1024 * 1024
NOTICE_TOTAL_MAX_BYTES = 20 * 1024 * 1024
CACHE_SCAN_LIMIT = 50
_STICKER_MARKERS = ("gxh.vip.qq.com", "/club/item/", "qzonestyle.gtimg.cn", "qzone/em")
COURSE_SUBS = {
    "class_change": "上课 / 调课 / 停课",
    "exam": "考试 / 测验",
    "homework": "作业 / 提交",
    "lab": "实验 / 实践",
    "enrollment": "选课 / 教务",
    "other": "其他课程",
}
EVERYONE_SUBS = {
    "survey": "调查 / 测评",
    "registration": "注册 / 缴费 / 材料",
    "health": "体测 / 体检 / 安全",
    "class_affairs": "班级事务",
    "other": "其他必做",
}
COURSE_ORDER = list(COURSE_SUBS)
EVERYONE_ORDER = list(EVERYONE_SUBS)

URGENT_RE = re.compile(
    r"停课|调课|停上|补课|取消|改期|提前|推迟|换教室|改教室|"
    r"教室.{0,8}(?:改|变|调整)|时间.{0,8}(?:改|变|调整)|"
    r"紧急|截止|ddl|DDL")
DEADLINE_RE = re.compile(r"今天|今晚|明天|明早|后天|本周|这周|下周|周[一二三四五六日天]")
ACTION_RE = re.compile(
    r"交|提交|补交|截止|ddl|DDL|带|准备|考试|测验|上课|调课|停课|"
    r"补课|取消|改期|提前|推迟|集合|签到|点到|作业|实验|报告")
QUESTION_RE = re.compile(
    r"[?？]|请问|想问|问一下|谁知道|谁有|有没有|哪些|哪几|"
    r"什么(?:作业|时候|内容|要求)|怎么办|怎么交|怎么弄|是否|"
    r"吗\s*[。.!！]?$|呢\s*[。.!！]?$")
URGENT_CONTEXT_BEFORE = 600
URGENT_CONTEXT_AFTER = 120
COURSE_RE = re.compile(
    r"上课|停课|调课|课时|教室|课程|考试|测验|作业|实验|选课|教务|"
    r"提交|ddl|DDL|课代表|任课老师")
SURVEY_RE = re.compile(r"调查|问卷|测评|心理|填写|统计|收集")
REGISTER_RE = re.compile(r"注册|缴费|材料|报名|提交|表格|盖章|证件|确认")
HEALTH_RE = re.compile(r"体测|体检|安全|疫苗|医保|健康|消防")
AFFAIR_RE = re.compile(r"班会|班委|班级|请假|考勤|宿舍|选举|通知群")
EXAM_RE = re.compile(r"考试|测验|月考|期中|期末|quiz|Quiz")
HOMEWORK_RE = re.compile(r"作业|实验报告|提交|截止|ddl|DDL")
LAB_RE = re.compile(r"实验|实践|上机")
ENROLL_RE = re.compile(r"选课|教务|退课|补选|学分")


def _tz_now() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def _start_ts(s: Settings) -> int:
    raw = (s.newspaper or {}).get("start_at") or "2026-10-19T00:00:00+08:00"
    dt = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp())


def _notice_cfg(s: Settings) -> dict:
    return (s.newspaper or {}).get("notice") or {}


def _tech_cfg(s: Settings) -> dict:
    return (s.newspaper or {}).get("tech") or {}


def _image_text_cfg(s: Settings) -> dict:
    raw = _notice_cfg(s).get("image_text") or {}
    return raw if isinstance(raw, dict) else {}


def _tg_originals(s: Settings) -> bool:
    """图片原图是否走 Telegram（而不是塞邮件附件）。"""
    return bool(getattr(s, "tg_image_enabled", False)
                and _image_text_cfg(s).get("tg_originals", True))


def _group_map(cfg: dict, key: str) -> dict[str, str]:
    raw = cfg.get(key) or {}
    return {str(k): (v if isinstance(v, str) else str(v.get("name") or k))
            for k, v in raw.items()} if isinstance(raw, dict) else {}


def _image_groups(s: Settings) -> dict[str, dict]:
    raw = _notice_cfg(s).get("image_forward_groups") or {}
    out: dict[str, dict] = {}
    for gid, val in raw.items():
        if isinstance(val, dict):
            out[str(gid)] = {"name": str(val.get("name") or gid),
                             "max_images": max(1, int(val.get("max_images") or 9)),
                             "max_bytes": max(1024, int(val.get("max_bytes") or 10 * 1024 * 1024))}
        else:
            out[str(gid)] = {"name": str(val), "max_images": 9,
                             "max_bytes": 10 * 1024 * 1024}
    return out


def _special_groups(s: Settings) -> dict[str, dict]:
    raw = _tech_cfg(s).get("special_groups") or {}
    out = {}
    for gid, val in raw.items():
        if isinstance(val, dict):
            out[str(gid)] = {"name": str(val.get("name") or gid),
                             "user_id": str(val.get("user_id") or ""),
                             "column_title": str(val.get("column_title") or val.get("name") or gid)}
        else:
            out[str(gid)] = {"name": str(val), "user_id": "", "column_title": str(val)}
    return out


def notice_groups(s: Settings) -> dict[str, str]:
    cfg = _notice_cfg(s)
    out = {}
    out.update(_group_map(cfg, "course_groups"))
    out.update(_group_map(cfg, "course_hint_groups"))
    out.update(_group_map(cfg, "mandatory_groups"))
    return out


def source_for_group(s: Settings, gid: str) -> str | None:
    cfg = _notice_cfg(s)
    if gid in _group_map(cfg, "course_groups"):
        return "course"
    if gid in _group_map(cfg, "course_hint_groups"):
        return "course_hint"
    if gid in _group_map(cfg, "mandatory_groups"):
        return "everyone"
    return None


def latest_slot(now: _dt.datetime, hours: list[int]) -> _dt.datetime:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slots = [today.replace(hour=int(h)) for h in hours if int(h) <= now.hour]
    slots.append(today - _dt.timedelta(days=1) + _dt.timedelta(hours=max(int(h) for h in hours)))
    return max(slots)


def _cn_date(dt: _dt.datetime) -> str:
    week = "一二三四五六日"[dt.weekday()]
    return f"{dt.year}年{dt.month}月{dt.day}日 星期{week}"


def _esc(value: Any) -> str:
    return _h.escape(str(value or ""), quote=True)


def _nl2br(value: Any) -> str:
    return _esc(value).replace("\n", "<br>")


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if re.match(r"^https?://", url, re.I) else ""


def _short(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _one_sentence(text: str, limit: int = 100) -> str:
    """Keep the first sentence only; used by the compact AI preview."""
    text = re.sub(r"\s+", " ", (text or "").replace("\r\n", "\n")).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？!?])\s*", text)
    sentence = (parts[0] if parts else text).strip()
    if len(sentence) > limit:
        sentence = sentence[:limit].rstrip("，；,;。 ") + "…"
    return sentence


def _strip_nick(line: str) -> str:
    return re.sub(r"^[^\n:：]{1,40}[:：]\s*", "", line or "").strip()


def _images(block) -> list[str]:
    try:
        rows = json.loads(block["images"] or "[]")
    except (KeyError, TypeError, ValueError):
        rows = []
    return [str(x) for x in rows if _safe_url(x)]


def _files(block) -> list[dict]:
    try:
        rows = json.loads(block["files"] or "[]")
    except (KeyError, TypeError, ValueError):
        rows = []
    return [x for x in rows if isinstance(x, dict)]


def _is_sticker(url: object) -> bool:
    u = str(url)
    return any(m in u for m in _STICKER_MARKERS)


def _image_ext_mime(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".jpg", "image/jpeg"


def _block_time(block) -> str:
    ts = int(block["end_ts"] or block["created_ts"] or 0)
    return _dt.datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M") if ts else ""


def _subcategory(text: str, category: str) -> str:
    if category == "course":
        if EXAM_RE.search(text):
            return "exam"
        if HOMEWORK_RE.search(text):
            return "homework"
        if LAB_RE.search(text):
            return "lab"
        if ENROLL_RE.search(text):
            return "enrollment"
        if re.search(r"停课|调课|上课|教室|课表|补课|地点|时间", text):
            return "class_change"
        return "other"
    if SURVEY_RE.search(text):
        return "survey"
    if HEALTH_RE.search(text):
        return "health"
    if REGISTER_RE.search(text):
        return "registration"
    if AFFAIR_RE.search(text):
        return "class_affairs"
    return "other"


def _is_urgent_text(text: str) -> bool:
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or QUESTION_RE.search(line):
            continue
        if URGENT_RE.search(line):
            return True
        if DEADLINE_RE.search(line) and ACTION_RE.search(line):
            return True
    return False


def _merge_blocks(blocks) -> dict:
    lines: list[str] = []
    images: list[str] = []
    files: list[dict] = []
    for block in blocks:
        for line in (block["text"] or "").splitlines():
            line = line.strip()
            if line and line not in lines:
                lines.append(line)
        for url in _images(block):
            if url not in images:
                images.append(url)
        for f in _files(block):
            if f not in files:
                files.append(f)
    return {"text": "\n".join(lines), "images": images, "files": files}


def fallback_notice_item(block, source: str, group_name: str) -> dict:
    text = (block["text"] or "").strip()
    category = ("course" if source in ("course", "course_hint") else "everyone")
    if source == "course_hint" and not COURSE_RE.search(text):
        category = "activity"
    title = (block["title"] or "").strip() or _strip_nick(_first_line(text))
    if not title:
        title = "[图片/文件通知]" if (_images(block) or _files(block)) else "[待确认通知]"
    summary = _short(text, 120) or "（仅图片/文件，请查看原文）"
    return {
        "id": int(block["id"]),
        "category": category,
        "subcategory": _subcategory(text, category),
        "title": _short(title, 40),
        "summary": summary,
        "urgent": bool(category == "course" and _is_urgent_text(text)),
        "group": group_name,
        "time": _block_time(block),
        "images": _images(block),
        "files": _files(block),
    }


def _item_from_llm(block, row: dict, source: str, group_name: str) -> dict:
    base = fallback_notice_item(block, source, group_name)
    cat = str(row.get("category") or "")
    if cat not in ("course", "everyone", "activity", "ignore"):
        return base
    base["category"] = cat
    sub = str(row.get("subcategory") or "").strip()
    if cat == "course" and sub not in COURSE_SUBS:
        sub = base["subcategory"] if base["subcategory"] in COURSE_SUBS else "other"
    if cat == "everyone" and sub not in EVERYONE_SUBS:
        sub = base["subcategory"] if base["subcategory"] in EVERYONE_SUBS else "other"
    if cat not in ("course", "everyone"):
        sub = "other"
    base["subcategory"] = sub
    title = _short(row.get("title"), 40)
    summary = _short(row.get("summary"), 120)
    if title:
        base["title"] = title
    if summary:
        base["summary"] = summary
    base["urgent"] = bool(base["urgent"] or row.get("urgent") is True)
    return base


def _grouped(items: list[dict]) -> list[tuple[str, list[tuple[str, list[dict]]]]]:
    out = []
    for cat, subs, labels in (("course", COURSE_ORDER, COURSE_SUBS),
                              ("everyone", EVERYONE_ORDER, EVERYONE_SUBS)):
        by: dict[str, list[dict]] = {}
        for item in items:
            if item["category"] == cat:
                by.setdefault(item["subcategory"], []).append(item)
        groups = [(sub, by[sub]) for sub in subs if sub in by]
        if groups:
            out.append((cat, groups))
    return out


def _item_html(item: dict, accent: str) -> str:
    parts = [f'<div style="font-size:11px;line-height:1.5;color:#6b7280;padding-bottom:3px;">'
             f'{_esc(item["group"])} · {_esc(item["time"])}</div>']
    if item.get("urgent"):
        parts.append(f'<span style="display:inline-block;background:{accent};color:#fff;'
                     f'font-size:10px;letter-spacing:1px;padding:1px 6px;border-radius:2px;">紧急</span>')
    parts.append(f'<div style="font-size:16px;line-height:1.45;font-weight:bold;color:#111827;'
                 f'padding:5px 0 4px;">{_esc(item["title"])}</div>')
    parts.append(f'<div style="font-size:13px;line-height:1.65;color:#374151;">'
                 f'{_nl2br(item["summary"])}</div>')
    ai = item.get("ai_preview") or {}
    if ai.get("text"):
        if not ai.get("verified", True):
            warn = "；仅单模型识别，未交叉校验"
        elif not ai.get("agree", True):
            warn = "；两版识别不一致，关键数字请以原图为准"
        else:
            warn = ""
        parts.append(
            '<div style="margin-top:6px;padding:7px 8px;background:#f3f4f6;'
            'border-left:3px solid #9ca3af;font-size:11px;line-height:1.55;color:#4b5563;">'
            f'<div style="font-weight:bold;color:#6b7280;">AI 试读（可能有误{warn}）</div>'
            f'<div style="white-space:pre-wrap;font-family:Consolas,monospace;">{_esc(ai["text"])}</div>'
            '</div>')
    links = []
    for url in item.get("images") or []:
        links.append(f'<a href="{_esc(url)}" style="color:{accent};text-decoration:none;">查看原图</a>')
    for f in item.get("files") or []:
        name = f.get("name") or "文件"
        url = _safe_url(f.get("url"))
        if url:
            links.append(f'<a href="{_esc(url)}" style="color:{accent};text-decoration:none;">'
                         f'附件：{_esc(name)}</a>')
        else:
            links.append(f'<span style="color:#6b7280;">群文件：{_esc(name)}'
                         '（请在 QQ 群内查看或手动下载）</span>')
    if links:
        parts.append('<div style="font-size:11px;line-height:1.7;padding-top:4px;">'
                     + ' · '.join(links) + '</div>')
    return "".join(parts)


def _two_col(items: list[dict], accent: str) -> str:
    if len(items) == 1:
        return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                f'<tr><td width="100%" valign="top" style="padding:0 0 18px;">'
                f'{_item_html(items[0], accent)}</td></tr></table>')
    rows = []
    for i in range(0, len(items), 2):
        cells = [f'<td width="50%" valign="top" style="padding:0 14px 18px 0;">'
                 f'{_item_html(items[i], accent)}</td>']
        if i + 1 < len(items):
            cells.append(f'<td width="50%" valign="top" style="padding:0 0 18px 14px;">'
                         f'{_item_html(items[i + 1], accent)}</td>')
        else:
            cells.append('<td width="50%" valign="top"></td>')
        rows.append('<tr>' + ''.join(cells) + '</tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            + ''.join(rows) + '</table>')


def _watermark(font_size: int = 86) -> str:
    return ('<div aria-hidden="true" style="position:absolute;right:16px;top:12px;font-family:Arial,'
            f'Helvetica,sans-serif;font-size:{font_size}px;line-height:1;font-weight:800;'
            'letter-spacing:2px;color:#edf1f6;white-space:nowrap;z-index:0;">INFOHUB</div>')


def render_notice(slot: _dt.datetime, items: list[dict], total: int) -> tuple[str, str]:
    sections = _grouped(items)
    parts = [
        '<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#efece3">',
        '<tr><td align="center" style="padding:22px 8px;">',
        '<table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#fffdf8" style="width:680px;max-width:680px;">',
        '<tr><td style="padding:0;border-top:8px solid #111827;">',
        '<div style="position:relative;padding:30px 28px 6px;overflow:hidden;">',
        _watermark(),
        '<div style="position:relative;z-index:1;">',
        '<div style="font-family:Georgia,\'Times New Roman\',\'Songti SC\',serif;font-size:38px;'
        'line-height:1.1;font-weight:bold;letter-spacing:3px;color:#111827;">某校消息</div>',
        '<div style="font-size:14px;letter-spacing:10px;color:#6b7280;padding:8px 0 0;">通 知 报</div>',
        f'<div style="border-top:1px solid #d1d5db;margin-top:16px;padding-top:9px;font-size:12px;'
        f'color:#6b7280;">{_cn_date(slot)} · {slot:%H:%M} 版 · 本期 {total} 条</div>',
        '</div></div></td></tr>',
    ]
    for cat, groups in sections:
        title = "第一类 · 我的课程" if cat == "course" else "第二类 · 每个同学都要做"
        accent = "#9a3412" if cat == "course" else "#1d4ed8"
        parts.append(f'<tr><td style="padding:26px 28px 0;">')
        parts.append(f'<div style="font-size:19px;font-weight:bold;letter-spacing:1px;color:#111827;">'
                     f'{title}</div>')
        parts.append('<div style="border-top:2px solid #111827;margin:9px 0 16px;"></div>')
        for sub, rows in groups:
            label = (COURSE_SUBS if cat == "course" else EVERYONE_SUBS)[sub]
            parts.append(f'<div style="font-size:11px;letter-spacing:3px;color:{accent};'
                         f'padding:2px 0 8px;">{label}</div>')
            parts.append(_two_col(rows, accent))
        parts.append('</td></tr>')
    parts += [
        '<tr><td style="padding:12px 28px 30px;">',
        '<div style="border-top:1px solid #d1d5db;padding-top:12px;font-size:11px;line-height:1.7;'
        'color:#9ca3af;">本期由 info-hub 自动整理；请以群内原文为准。'
        '邮件中的链接来自原群消息，打开前请自行核对。</div>',
        '</td></tr></table></td></tr></table></body></html>',
    ]
    html = "".join(parts)
    lines = [f"某校消息 · 通知报", f"{slot:%Y-%m-%d %H:%M} · 本期 {total} 条", ""]
    for cat, groups in sections:
        lines.append("第一类 · 我的课程" if cat == "course" else "第二类 · 每个同学都要做")
        for sub, rows in groups:
            lines.append(f"[{(COURSE_SUBS if cat == 'course' else EVERYONE_SUBS)[sub]}]")
            for item in rows:
                lines.append(f"- {item['title']}（{item['group']} · {item['time']}）")
                lines.append(f"  {item['summary']}")
                for f in item.get("files") or []:
                    name = f.get("name") or "文件"
                    url = _safe_url(f.get("url"))
                    if url:
                        lines.append(f"  [附件] {name}: {url}")
                    else:
                        lines.append(f"  [群文件] {name}（请在 QQ 群内查看或手动下载）")
                ai = item.get("ai_preview") or {}
                if ai.get("text"):
                    flag = ("（仅单模型）" if not ai.get("verified", True)
                            else ("（两版不一致）" if not ai.get("agree", True) else ""))
                    lines.append(f"  [AI 试读{flag}] {ai['text'][:300]}")
        lines.append("")
    return html, "\n".join(lines)


def render_urgent(items: list[dict] | dict) -> tuple[str, str]:
    if isinstance(items, dict):
        items = [items]
    html, text = render_notice(_tz_now(), items, len(items))
    html = html.replace("通 知 报", "紧 急 课 程 提 醒", 1)
    text = text.replace("通 知 报", "紧急课程提醒", 1)
    return html, text


def render_tech(now: _dt.datetime, report: dict, forced: bool, s: Settings) -> tuple[str, str]:
    cfg = _tech_cfg(s)
    specials = _special_groups(s)
    titles = {}
    for gid, name in _group_map(cfg, "groups").items():
        if gid == "123456796":
            titles["aia"] = name
        elif gid == "123456797":
            titles["aihut"] = name
    for gid, info in specials.items():
        titles["guotie"] = info["column_title"]
    titles.setdefault("aia", "NJU_AIA学社交流群")
    titles.setdefault("aihut", "卡普迪姆的 AI 交流小屋")
    titles.setdefault("guotie", "某技术群")
    incomplete = (not report.get("ready")) or forced
    parts = [
        '<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#eef2f7">',
        '<tr><td align="center" style="padding:22px 8px;">',
        '<table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#ffffff" style="width:680px;max-width:680px;">',
        '<tr><td style="padding:0;border-top:8px solid #0f172a;">',
        '<div style="position:relative;padding:30px 28px 6px;overflow:hidden;">',
        _watermark(),
        '<div style="position:relative;z-index:1;">',
        '<div style="font-family:Georgia,\'Times New Roman\',\'Songti SC\',serif;font-size:38px;'
        'line-height:1.1;font-weight:bold;letter-spacing:3px;color:#0f172a;">某校消息</div>',
        '<div style="font-size:14px;letter-spacing:8px;color:#64748b;padding:8px 0 0;">技 术 情 报 报</div>',
        f'<div style="border-top:1px solid #cbd5e1;margin-top:16px;padding-top:9px;font-size:12px;'
        f'color:#64748b;">{_cn_date(now)} · {now:%H:%M} 版 · AI 研究整理</div>',
        '</div></div></td></tr>',
    ]
    if incomplete:
        missing = "；".join(report.get("missing") or []) or "部分前置知识或验证尚未完成"
        parts.append('<tr><td style="padding:16px 28px 0;">'
                     '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                     'bgcolor="#fef3c7"><tr><td style="padding:10px 12px;font-size:12px;line-height:1.6;'
                     f'color:#92400e;">本期资料尚不完整，标注“待补充”：{_esc(missing)}</td></tr></table>'
                     '</td></tr>')
    for sec in report.get("sections") or []:
        key = sec.get("key")
        if key not in titles:
            continue
        accent = "#0f766e" if key == "guotie" else "#0f172a"
        parts.append('<tr><td style="padding:26px 28px 0;">')
        parts.append(f'<div style="font-size:19px;font-weight:bold;letter-spacing:1px;color:{accent};">'
                     f'{_esc(titles[key])}</div>')
        parts.append(f'<div style="border-top:2px solid {accent};margin:9px 0 16px;"></div>')
        for item in sec.get("items") or []:
            parts.append(f'<div style="padding:0 0 20px;">')
            meta = f'{_esc(titles[key])}'
            parts.append(f'<div style="font-size:11px;color:#94a3b8;padding-bottom:3px;">{meta}</div>')
            labels = item.get("labels") or []
            if labels:
                parts.append('<div style="padding-bottom:3px;">' + " ".join(
                    f'<span style="display:inline-block;background:#e2e8f0;color:#334155;font-size:10px;'
                    f'letter-spacing:1px;padding:1px 6px;border-radius:2px;">{_esc(x)}</span>'
                    for x in labels) + '</div>')
            parts.append(f'<div style="font-size:16px;line-height:1.45;font-weight:bold;color:#0f172a;'
                         f'padding:4px 0;">{_esc(item.get("title"))}</div>')
            if item.get("summary"):
                parts.append(f'<div style="font-size:13px;line-height:1.7;color:#334155;">'
                             f'{_nl2br(item["summary"])}</div>')
            details = item.get("details") or []
            if details:
                parts.append('<ol style="margin:10px 0 0 20px;padding:0;font-size:13px;line-height:1.75;'
                             'color:#334155;">' + "".join(f'<li style="padding-bottom:14px;">'
                                                          f'{_nl2br(x)}</li>' for x in details) + '</ol>')
            sources = item.get("sources") or []
            if sources:
                parts.append('<div style="font-size:11px;line-height:1.8;padding-top:6px;">' + " · ".join(
                    f'<a href="{_esc(u)}" style="color:#0f766e;text-decoration:none;">来源</a>'
                    for u in sources) + '</div>')
            parts.append('</div>')
        parts.append('</td></tr>')
    if not report.get("sections"):
        parts.append('<tr><td style="padding:30px 28px;font-size:13px;color:#64748b;">'
                     '本期没有可整理的技术内容。</td></tr>')
    parts += [
        '<tr><td style="padding:12px 28px 30px;">',
        '<div style="border-top:1px solid #cbd5e1;padding-top:12px;font-size:11px;line-height:1.7;'
        'color:#94a3b8;">“AI 补充”是编辑补的背景知识；“未验证”表示未能从可靠来源确认。'
        '请把邮件当作学习线索，关键操作前自行核对。</div>',
        '</td></tr></table></td></tr></table></body></html>',
    ]
    text = ["某校消息 · 技术情报报", f"{now:%Y-%m-%d %H:%M}"]
    if incomplete:
        text.append("（本期资料尚不完整，标注待补充）")
    for sec in report.get("sections") or []:
        key = sec.get("key")
        if key not in titles:
            continue
        text.append("")
        text.append(f"== {titles[key]} ==")
        for item in sec.get("items") or []:
            text.append(f"- {item.get('title')}")
            if item.get("summary"):
                text.append(f"  {item.get('summary')}")
            for j, d in enumerate(item.get("details") or [], 1):
                text.append(f"  {j}. {d}")
                text.append("")
            for u in item.get("sources") or []:
                text.append(f"  来源：{u}")
    return "".join(parts), "\n".join(text)


def sample_notice_items(now: _dt.datetime) -> list[dict]:
    base = {"images": [], "files": [], "urgent": False}
    return [
        {**base, "id": 1, "category": "course", "subcategory": "class_change",
         "title": "周四化学实验改到 306", "summary": "本周四实验课教室由 301 调整到 306，请提前十分钟到。",
         "urgent": True, "group": "化学实验基础", "time": "10-19 07:40"},
        {**base, "id": 2, "category": "course", "subcategory": "exam",
         "title": "微积分第 6 周小测", "summary": "周五第 3 节随堂测验，范围到导数应用，可带一张 A4 手写公式。",
         "group": "微积分1(第二层次)5班", "time": "10-18 21:10"},
        {**base, "id": 3, "category": "course", "subcategory": "homework",
         "title": "普通生物学作业截止", "summary": "实验报告请在 10 月 21 日 23:59 前提交到教学网，逾期扣分。",
         "group": "普通生物学", "time": "10-18 19:25"},
        {**base, "id": 4, "category": "everyone", "subcategory": "survey",
         "title": "心理健康普查", "summary": "全体新生需在 10 月 20 日前完成心理测评，约 15 分钟。",
         "group": "2026级某大类群", "time": "10-18 16:02"},
        {**base, "id": 5, "category": "everyone", "subcategory": "registration",
         "title": "学生证补盖章", "summary": "未盖章的同学请于本周三前把学生证交到班级负责人处。",
         "group": "某班", "time": "10-18 12:30"},
    ]


def sample_tech_report() -> dict:
    return {
        "ready": True,
        "summary": "本期讲清楚本地跑开源模型的最小闭环：显存估算、量化、启动与排错。",
        "missing": [],
        "sections": [
            {"key": "aia", "items": [
                {"title": "先用显存公式判断能不能跑",
                 "summary": "参数量 × 每参数字节数 + KV cache 是估算下限；7B 模型 4bit 量化大约 4–5GB 起步。",
                 "details": ["先确认显卡显存：Windows 任务管理器 → 性能 → GPU → 专用 GPU 内存。",
                             "每参数字节数：fp16 约 2，int8 约 1，int4 约 0.5；再留 20% 余量给上下文。"],
                 "sources": ["https://huggingface.co/docs/transformers/main/en/quantization/overview"],
                 "labels": ["AI 补充"]},
                {"title": "Ollama 最小启动流程",
                 "summary": "安装后一条命令拉模型并启动本地 API，适合第一次跑通。",
                 "details": ["安装 Ollama 后运行 `ollama run qwen2.5:7b`。",
                             "默认 API 在 127.0.0.1:11434，可用 curl 测试。"],
                 "sources": ["https://github.com/ollama/ollama"],
                 "labels": []},
            ]},
            {"key": "aihut", "items": [
                {"title": "报错先看是不是显存不足",
                 "summary": "CUDA out of memory 不一定是显卡太小，先降上下文长度或换更小的量化。",
                 "details": ["把上下文从 8k 降到 4k，通常能省下可观的 KV cache。",
                             "关闭其他占显存的程序；笔记本独显还要确认没走核显。"],
                 "sources": [],
                 "labels": ["未验证"]},
            ]},
            {"key": "guotie", "items": [
                {"title": "别急着上微调",
                 "summary": "先把提示词、检索和工具调用跑通，绝大多数需求不需要训练模型。",
                 "details": ["微调需要成对数据和评测集；没有评测集就无法判断是否变好。",
                             "先用 RAG 把资料喂进上下文，成本更低、迭代更快。"],
                 "sources": [],
                 "labels": ["AI 补充"]},
            ]},
        ],
    }


def validate_html(html: str) -> str:
    low = (html or "").lower()
    if len(low) > 500_000:
        raise ValueError("newspaper html too large")
    if "<table" not in low:
        raise ValueError("newspaper html missing table layout")
    for bad in ("<script", "<iframe", "<object", "<embed", "javascript:"):
        if bad in low:
            raise ValueError(f"newspaper html contains {bad}")
    if re.search(r"<[^>]*\son[a-z]+\s*=", low):
        raise ValueError("newspaper html contains event handler")
    return html


class NewspaperDispatcher:
    def __init__(self, settings: Settings, db: Database, classifier, mailer):
        self.s = settings
        self.db = db
        self.classifier = classifier
        self.mailer = mailer
        self._tech_scan_at = 0.0
        self._media_prune_at = 0.0
        self._vision_pending: set[str] = set()
        self._vision_attempts: dict[str, int] = {}
        self._vision_tasks: set[asyncio.Task] = set()
        self._vision_started = 0

    @property
    def ready(self) -> bool:
        return bool(getattr(self.s, "mail_enabled", False) and self.mailer and self.mailer.ready)

    @property
    def official(self) -> bool:
        return bool(getattr(self.s, "newspaper_official_send", False))

    async def tick(self) -> int:
        self.finalize_sweep()
        if not getattr(self.s, "newspaper_enabled", False):
            return 0
        await self.cache_images()
        await self.forward_originals()
        if not self.ready or not self.official:
            return 0
        sent = await self.urgent()
        sent += await self._notice_tick()
        sent += await self._tech_tick()
        return sent

    async def close(self) -> None:
        for task in list(self._vision_tasks):
            task.cancel()
        if self._vision_tasks:
            await asyncio.gather(*self._vision_tasks, return_exceptions=True)
        from info_hub.telegram_files import get_tg_files
        from info_hub.vision import get_vision
        await get_tg_files().close()
        await get_vision(self.s).close()

    # ---------- 发送 ----------
    def finalize_sweep(self) -> int:
        return self.db.finalize_newspaper_manifests()

    async def _send(self, subject: str, html: str, text: str, key: str, kind: str,
                    attachments=(), manifest: dict | None = None) -> bool:
        from info_hub.mailer import build_message
        from info_hub.outbox import Outbox
        try:
            validate_html(html)
        except ValueError as exc:
            logger.error("报纸 HTML 校验失败，拒绝发送：%s", exc)
            return False
        msg = build_message(self.s, subject=subject, text=text, html=html,
                            attachments=attachments)
        msg["X-InfoHub-ReadOnly"] = "true"
        box = Outbox(self.db, self.mailer)
        box.queue(key, msg, kind=kind, newspaper_manifest=manifest)
        await box.send(key)
        row = self.db._query("SELECT state FROM deliveries WHERE key=?", (key,))
        return bool(row and row[0]["state"] == "sent")

    async def _download(self, url: str, max_bytes: int) -> bytes:
        from info_hub.safe_download import download
        return await download(url, kind="image", max_bytes=max_bytes)

    def _media_path(self, url: str) -> Path:
        digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()
        return Path(self.s.base_dir) / "data" / "media" / f"{digest}.bin"

    def _save_media(self, url: str, data: bytes) -> Path:
        path = self._media_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def _sidecar_path(self, url: str, suffix: str) -> Path:
        return self._media_path(url).with_suffix(suffix)

    def _load_extraction(self, url: str) -> dict | None:
        path = self._sidecar_path(url, ".json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("ok") else None

    def _save_extraction(self, url: str, result: dict) -> None:
        path = self._sidecar_path(url, ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _preview_text(self, ext: dict) -> str:
        from info_hub.vision import clean_transcript
        summary = str(ext.get("summary") or "").strip()
        facts = ext.get("facts") or {}
        if summary:
            missing = [str(facts.get(key) or "").strip()
                       for key in ("date", "time")
                       if str(facts.get(key) or "").strip()
                       and str(facts.get(key) or "").strip() not in summary]
            if missing:
                summary = "，".join(missing + [summary])
            return _one_sentence(summary, 120)
        meta = "，".join(
            str(facts.get(key) or "").strip()
            for key in ("date", "time", "place", "deadline")
            if str(facts.get(key) or "").strip()
        )
        transcript = clean_transcript(str(ext.get("transcript") or ""))
        if meta:
            return _one_sentence(meta + ("：" + transcript if transcript else ""), 100)
        return _one_sentence(transcript, 100)

    def _schedule_extraction(self, url: str, data: bytes) -> None:
        from info_hub.vision import get_vision
        if not get_vision(self.s).enabled:
            return
        if url in self._vision_pending or self._load_extraction(url):
            return
        max_per_tick = max(1, int(_image_text_cfg(self.s).get("max_per_tick") or 2))
        if self._vision_started >= max_per_tick:
            return
        self._vision_started += 1
        self._vision_pending.add(url)
        task = asyncio.create_task(self._extract_and_save(url, data))
        self._vision_tasks.add(task)
        task.add_done_callback(self._vision_tasks.discard)

    async def _extract_and_save(self, url: str, data: bytes) -> None:
        try:
            from info_hub.vision import get_vision
            ext, mime = _image_ext_mime(data)
            result = await get_vision(self.s).extract(data, mime)
            if result.get("ok"):
                self._save_extraction(url, result)
                logger.info("课程图片转写完成：kind=%s legible=%s agree=%s",
                            result.get("kind"), result.get("legible"),
                            (result.get("compare") or {}).get("same"))
            else:
                self._vision_attempts[url] = self._vision_attempts.get(url, 0) + 1
                logger.warning("课程图片转写失败（第 %d 次）：%s",
                               self._vision_attempts[url], result.get("error"))
        except Exception as exc:  # noqa: BLE001
            self._vision_attempts[url] = self._vision_attempts.get(url, 0) + 1
            logger.warning("课程图片转写异常：%s: %s", type(exc).__name__, exc)
        finally:
            self._vision_pending.discard(url)

    def _attach_ai_preview(self, item: dict, block) -> None:
        if not _image_text_cfg(self.s):
            return
        previews = []
        for url in item.get("images") or []:
            ext = self._load_extraction(str(url))
            if ext:
                text = self._preview_text(ext)
                if text:
                    previews.append((ext, text))
        if not previews:
            return
        agree = True
        verified = True
        for ext, _text in previews:
            cmp = ext.get("compare") or {}
            if not cmp:
                verified = False
                agree = False
                continue
            if not cmp.get("same", False):
                agree = False
        item["ai_preview"] = {
            "text": "；".join(text for _, text in previews),
            "agree": agree,
            "verified": verified,
            "kinds": [ext.get("kind") for ext, _ in previews],
        }

    def _prune_media(self) -> None:
        now = time.time()
        if now - self._media_prune_at < 3600:
            return
        self._media_prune_at = now
        folder = Path(self.s.base_dir) / "data" / "media"
        if not folder.is_dir():
            return
        for path in folder.iterdir():
            if path.suffix not in (".bin", ".json", ".tg"):
                continue
            try:
                if now - path.stat().st_mtime > MEDIA_CACHE_MAX_AGE:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    async def cache_images(self) -> int:
        """通知群图片在链接过期前落盘并做 AI 试读；某校图片只落盘/转发。"""
        notice = notice_groups(self.s)
        groups = list(dict.fromkeys([*notice, *_image_groups(self.s)]))
        if not groups:
            return 0
        self._prune_media()
        self._vision_started = 0
        cursor = int(self.db.kv_get("newspaper:media:after_id", "0") or "0")
        since = int(time.time()) - IMAGE_FRESH_SECONDS
        marks = ",".join("?" for _ in groups)
        rows = self.db._query(
            f"SELECT id,ts,group_id,images FROM messages WHERE group_id IN ({marks}) "
            "AND id>? AND ts>=? ORDER BY id LIMIT ?",
            (*groups, cursor, since, CACHE_SCAN_LIMIT))
        if not rows:
            return 0
        cached = 0
        budget = 8
        last_done = cursor
        for row in rows:
            try:
                images = json.loads(row["images"] or "[]")
            except (TypeError, ValueError):
                images = []
            gid = str(row["group_id"])
            row_done = True
            for url in images:
                url = str(url)
                if not _safe_url(url) or _is_sticker(url):
                    continue
                path = self._media_path(url)
                if path.exists():
                    if gid in notice and not self._load_extraction(url):
                        try:
                            self._schedule_extraction(url, path.read_bytes())
                        except OSError:
                            pass
                    continue
                if budget <= 0:
                    row_done = False
                    break
                budget -= 1
                try:
                    data = await self._download(url, NOTICE_IMAGE_MAX_BYTES)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("课程图片缓存失败: %s", type(exc).__name__)
                    continue
                if not data or len(data) > NOTICE_IMAGE_MAX_BYTES:
                    continue
                try:
                    self._save_media(url, data)
                except OSError as exc:
                    logger.warning("课程图片写入失败: %s", type(exc).__name__)
                    continue
                if gid in notice:
                    self._schedule_extraction(url, data)
                cached += 1
            if not row_done:
                break
            last_done = max(last_done, int(row["id"]))
        self.db.kv_set("newspaper:media:after_id", str(last_done))
        if cached:
            logger.info("通知图片已缓存：%d 张", cached)
        return cached

    async def _notice_attachments(self, items: list[dict]) -> list[tuple[str, bytes, str]]:
        urls: list[str] = []
        seen: set[str] = set()
        for item in items:
            for url in item.get("images") or []:
                url = str(url)
                if not _safe_url(url) or _is_sticker(url) or url in seen:
                    continue
                seen.add(url)
                urls.append(url)
        attachments: list[tuple[str, bytes, str]] = []
        total = 0
        for i, url in enumerate(urls[:NOTICE_MAX_IMAGES]):
            data = None
            path = self._media_path(url)
            if path.exists():
                try:
                    data = path.read_bytes()
                except OSError:
                    data = None
            if data is None:
                try:
                    data = await self._download(url, NOTICE_IMAGE_MAX_BYTES)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("通知图片下载失败: %s", type(exc).__name__)
                    continue
                try:
                    self._save_media(url, data)
                except OSError:
                    pass
            if not data or len(data) > NOTICE_IMAGE_MAX_BYTES:
                continue
            if total + len(data) > NOTICE_TOTAL_MAX_BYTES:
                break
            ext, mime = _image_ext_mime(data)
            attachments.append((f"notice{i + 1}{ext}", data, mime))
            total += len(data)
        return attachments

    async def forward_originals(self) -> int:
        """把课程群/某校消息的原图发到 Telegram 文件频道，邮件不再塞大附件。"""
        if not _tg_originals(self.s):
            return 0
        from info_hub.telegram_files import get_tg_files
        tg = get_tg_files()
        if not tg.enabled:
            return 0
        cfg = _image_text_cfg(self.s)
        max_send = max(1, int(cfg.get("tg_max_per_tick") or 6))
        notice = notice_groups(self.s)
        image_cfg = _image_groups(self.s)
        groups = list(dict.fromkeys([*notice, *image_cfg]))
        if not groups:
            return 0
        rows = self.db.newspaper_messages(groups, int(time.time()) - 2 * 3600,
                                          after_id=0, limit=200, kind="tg_image")
        sent = 0
        for m in rows:
            gid = str(m["group_id"])
            try:
                images = json.loads(m["images"] or "[]")
            except (TypeError, ValueError):
                images = []
            if not images:
                continue
            name = (notice.get(gid) or (image_cfg.get(gid) or {}).get("name")
                    or self.s.group_name(gid))
            when = _dt.datetime.fromtimestamp(int(m["ts"]), TZ).strftime("%m-%d %H:%M")
            text = (m["text"] or "").strip()
            for i, url in enumerate(images):
                url = str(url)
                if not _safe_url(url) or _is_sticker(url):
                    continue
                path = self._media_path(url)
                mark = path.with_suffix(".tg")
                if mark.exists():
                    continue
                if path.exists():
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                else:
                    try:
                        data = await self._download(url, NOTICE_IMAGE_MAX_BYTES)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Telegram 原图下载失败: %s", type(exc).__name__)
                        continue
                    if not data or len(data) > NOTICE_IMAGE_MAX_BYTES:
                        continue
                    try:
                        self._save_media(url, data)
                    except OSError:
                        pass
                suffix, _ = _image_ext_mime(data)
                caption = f"{name} · {when}"
                if text:
                    caption += "\n" + text[:180]
                ext = self._load_extraction(url)
                if ext:
                    preview = self._preview_text(ext)
                    if preview:
                        caption += "\n\n[AI 试读，可能有误]\n" + preview[:400]
                    cmp = ext.get("compare") or {}
                    if not cmp:
                        caption += "\n（仅单模型识别，未交叉校验）"
                    elif not cmp.get("same", False):
                        caption += "\n（两版识别不一致，请以原图为准）"
                mid = await tg.send_document(
                    data, f"{gid}-{int(m['id'])}-{i + 1}{suffix}", caption)
                if not mid:
                    continue
                try:
                    mark.write_text(str(mid), encoding="utf-8")
                except OSError:
                    pass
                sent += 1
                if sent >= max_send:
                    logger.info("Telegram 原图本轮到上限：%d 张", sent)
                    return sent
        if sent:
            logger.info("Telegram 原图已发送：%d 张", sent)
        return sent

    async def forward_images(self) -> int:
        """某校消息：不做文字分类，图片直接转发（每 30s 巡检一次）。"""
        self.finalize_sweep()
        if _tg_originals(self.s):
            await self.forward_originals()
        if not getattr(self.s, "newspaper_enabled", False) or not self.ready:
            return 0
        groups = _image_groups(self.s)
        if not groups:
            return 0
        after = int(self.db.kv_get("newspaper:image:after_id", "0") or "0")
        fresh_since = max(_start_ts(self.s), int(time.time()) - IMAGE_FRESH_SECONDS)
        rows = self.db.newspaper_messages(list(groups), fresh_since, after_id=after,
                                          limit=30, kind="nanan_image")
        sent = 0
        for m in rows:
            gid = str(m["group_id"])
            cfg = groups.get(gid, {})
            try:
                images = json.loads(m["images"] or "[]")
            except (TypeError, ValueError):
                images = []
            mid = int(m["id"])
            if not images:
                after = mid
                continue
            attachments: list[tuple[str, bytes, str]] = []
            links: list[str] = []
            overflow: list[str] = []
            tg_original = _tg_originals(self.s)
            if not tg_original:
                total_bytes = 0
                max_images = int(cfg.get("max_images") or 9)
                max_bytes = int(cfg.get("max_bytes") or 10 * 1024 * 1024)
                overflow = [str(url) for url in images[max_images:]]
                for i, url in enumerate(images[:max_images]):
                    url = str(url)
                    try:
                        data = await self._download(url, max_bytes)
                        if total_bytes + len(data) > 20 * 1024 * 1024:
                            raise ValueError("mail attachment total too large")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("某校消息图片下载失败: %s", type(exc).__name__)
                        links.append(url)
                        continue
                    total_bytes += len(data)
                    ext, mime = _image_ext_mime(data)
                    attachments.append((f"nanan{i + 1}{ext}", data, mime))
                links.extend(overflow)
            when = _dt.datetime.fromtimestamp(int(m["ts"]), TZ).strftime("%m-%d %H:%M")
            caption = (m["text"] or "").strip()
            link_html = "".join(f'<div style="font-size:12px;padding-top:6px;">'
                                f'<a href="{_esc(u)}">{_esc(u)}</a></div>' for u in links)
            note = ("原图见 Telegram 文件频道。" if tg_original
                    else "图片见附件；下载失败的原图链接保留在正文。")
            html = (
                '<!doctype html><html><head><meta charset="utf-8"></head><body>'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                'bgcolor="#f3f1ea"><tr><td align="center" style="padding:18px 8px;">'
                '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" '
                'bgcolor="#fffdf8" style="width:640px;max-width:640px;">'
                '<tr><td style="padding:0;border-top:8px solid #111827;">'
                '<div style="position:relative;padding:22px 24px;overflow:hidden;">'
                + _watermark(64) +
                '<div style="position:relative;z-index:1;">'
                f'<div style="font-size:24px;font-weight:bold;">{_esc(cfg.get("name") or gid)}</div>'
                f'<div style="font-size:12px;color:#6b7280;padding-top:6px;">{_esc(when)}</div>'
                f'<div style="font-size:14px;line-height:1.7;padding-top:12px;">{_nl2br(caption)}</div>'
                f'{link_html}'
                '<div style="font-size:11px;color:#9ca3af;padding-top:16px;">'
                f'{note}</div>'
                '</div></div></td></tr></table></td></tr></table></body></html>')
            text = f"{cfg.get('name') or gid} · {when}\n{caption}\n"
            if tg_original:
                text += "\n" + note
            if links:
                text += "\n下载失败的原图链接：\n" + "\n".join(links)
            if overflow:
                overflow_note = "超过附件数量上限，以下图片保留原图链接。"
                html = html.replace(link_html, f'<div style="font-size:12px;padding-top:6px;">{overflow_note}</div>' + link_html)
                text += "\n" + overflow_note
            key = f"newspaper:image:{mid}"
            manifest = {"kind": "nanan_image", "ids": [mid], "cursor": {
                "newspaper:image:after_id": str(mid)}}
            if not await self._send(f"{cfg.get('name') or '某校消息'} · 图片转发 · {when}",
                                    html, text, key, "nanan-image", attachments, manifest):
                break
            sent += 1
            logger.info("某校消息图片已转发：message=%s，%d 张", mid, len(attachments))
            break
        self.db.kv_set("newspaper:image:after_id", str(after))
        return sent

    # ---------- 通知报 ----------
    def _filter_role_block(self, block) -> dict | None:
        """按 message 过滤历史块，不让 member 内容随 admin 整块放行。"""
        filters = _notice_cfg(self.s).get("role_filter_groups") or {}
        allowed = filters.get(str(block["group_id"]))
        if not allowed:
            return dict(block)
        allowed = {str(x).lower() for x in allowed}
        rows = self.db.block_messages(int(block["id"]))
        if not rows:
            logger.warning("角色过滤块无原始消息，已记 skipped：block=%s", block["id"])
            self.db.mark_newspaper_items("notice", [int(block["id"])], "role-empty", state="skipped")
            return None
        missing = [r for r in rows if not str(r["role"] or "").strip()]
        kept = [r for r in rows if str(r["role"] or "").lower() in allowed]
        if missing:
            logger.warning("历史消息角色缺失，无法在离线报纸阶段回查，已跳过：block=%s count=%d",
                           block["id"], len(missing))
        if not kept:
            self.db.mark_newspaper_items("notice", [int(block["id"])], "role-filter", state="skipped")
            return None
        out = dict(block)
        out["text"] = "\n".join(str(r["text"] or "") for r in kept if str(r["text"] or "").strip())
        images, files = [], []
        for row in kept:
            for target, key in ((images, "images"), (files, "files")):
                try:
                    target.extend(json.loads(row[key] or "[]"))
                except (TypeError, ValueError):
                    pass
        out["images"] = json.dumps(images, ensure_ascii=False)
        out["files"] = json.dumps(files, ensure_ascii=False)
        return out

    async def _classify(self, rows: list) -> list[dict]:
        cfg = _notice_cfg(self.s)
        batch = max(1, int(cfg.get("classify_batch_size") or 12))
        names = notice_groups(self.s)
        mapped: dict[int, dict] = {}
        for start in range(0, len(rows), batch):
            chunk = rows[start:start + batch]
            payload = []
            for b in chunk:
                gid = str(b["group_id"])
                src = source_for_group(self.s, gid) or "everyone"
                text = (b["text"] or "").strip()
                extra = []
                if _images(b):
                    extra.append("[图片]")
                if _files(b):
                    extra.append("[文件]")
                payload.append({"id": int(b["id"]), "source": src,
                                "group": names.get(gid, gid), "time": _block_time(b),
                                "text": anonymize([text + (" " + " ".join(extra) if extra else "")])})
            out = await self.classifier.newspaper_notice(payload)
            for row in out or []:
                try:
                    mapped[int(row.get("id"))] = row
                except (TypeError, ValueError):
                    continue
        items = []
        tg_original = _tg_originals(self.s)
        for b in rows:
            gid = str(b["group_id"])
            src = source_for_group(self.s, gid) or "everyone"
            row = mapped.get(int(b["id"]))
            item = (_item_from_llm(b, row, src, names.get(gid, gid)) if row
                    else fallback_notice_item(b, src, names.get(gid, gid)))
            if item["category"] in ("course", "everyone"):
                pass
            elif src == "course" and (_images(b) or _files(b)):
                if not (b["text"] or "").strip():
                    item = fallback_notice_item(b, "course", names.get(gid, gid))
                    item["title"] = _short(f"{names.get(gid, gid)} · 图片通知", 40)
                    item["summary"] = ("课程群图片/文件通知，原图见 Telegram。"
                                       if tg_original else "课程群图片/文件通知，原图见邮件附件。")
                item["category"] = "course"
                item["subcategory"] = "other"
            else:
                continue
            if item.get("images"):
                self._attach_ai_preview(item, b)
            items.append(item)
        return items

    async def _build_notice(self, rows: list, slot: _dt.datetime):
        items = await self._classify(rows)
        if not items:
            return None
        html, text = render_notice(slot, items, len(items))
        subject = f"某校消息 · 通知报 · {slot:%m-%d %H:%M}"
        return {"html": html, "text": text, "subject": subject,
                "ids": [it["id"] for it in items], "items": items}

    async def _notice_tick(self) -> int:
        now = _tz_now()
        cfg = _notice_cfg(self.s)
        hours = cfg.get("hours") or [8, 12, 18]
        slot = latest_slot(now, hours)
        last = self.db.kv_get("newspaper:notice:last_slot", "")
        if not last:
            # 首次部署只对齐时段，不补发部署前的积压；第一封等下一个 8/12/18。
            self.db.kv_set("newspaper:notice:last_slot", slot.isoformat())
            return 0
        try:
            if _dt.datetime.fromisoformat(last) >= slot:
                return 0
        except ValueError:
            pass
        after = int(self.db.kv_get("newspaper:notice:after_id", "0") or "0")
        groups = list(notice_groups(self.s))
        raw_rows = self.db.newspaper_blocks(groups, _start_ts(self.s), after_id=after,
                                            limit=int(cfg.get("max_items") or 80))
        rows = [filtered for b in raw_rows if (filtered := self._filter_role_block(b)) is not None]
        if not rows:
            if raw_rows:
                self.db.kv_set("newspaper:notice:after_id",
                               str(max(int(r["id"]) for r in raw_rows)))
            self.db.kv_set("newspaper:notice:last_slot", slot.isoformat())
            return 0
        built = await self._build_notice(rows, slot)
        max_id = max(int(r["id"]) for r in raw_rows)
        if not built:
            self.db.kv_set("newspaper:notice:after_id", str(max_id))
            self.db.kv_set("newspaper:notice:last_slot", slot.isoformat())
            return 0
        key = f"newspaper:notice:{slot:%Y%m%dT%H%M}"
        tg_original = _tg_originals(self.s)
        has_images = any(it.get("images") for it in built.get("items") or [])
        attachments = [] if tg_original else await self._notice_attachments(built.get("items") or [])
        note = ""
        if tg_original and has_images:
            note = "本期图片见 Telegram 文件频道；原图链接保留在正文。"
        elif attachments:
            note = "本期图片见邮件附件；原图链接保留在正文。"
        if note:
            built["text"] += "\n\n" + note
            built["html"] = built["html"].replace(
                '<tr><td style="padding:12px 28px 30px;">',
                '<tr><td style="padding:0 28px 8px;font-size:11px;color:#6b7280;">'
                f'{_esc(note)}</td></tr>'
                '<tr><td style="padding:12px 28px 30px;">', 1)
        manifest = {"kind": "notice", "ids": built["ids"], "cursor": {
            "newspaper:notice:after_id": str(max_id),
            "newspaper:notice:last_slot": slot.isoformat()}}
        if not await self._send(built["subject"], built["html"], built["text"], key,
                                "newspaper", attachments=attachments, manifest=manifest):
            return 0
        logger.info("通知报已发送：%s，%d 条", built["subject"], len(built["ids"]))
        return 1

    async def urgent(self) -> int:
        self.finalize_sweep()
        if not self.official:
            return 0
        cfg = _notice_cfg(self.s)
        groups = list(_group_map(cfg, "course_groups")) + list(_group_map(cfg, "course_hint_groups"))
        if not groups:
            return 0
        cursor = int(self.db.kv_get("newspaper:notice:urgent_id", "0") or "0")
        rows = self.db.newspaper_blocks(groups, _start_ts(self.s), after_id=cursor, limit=50)
        now_ts = int(time.time())
        triggers = []
        newest = cursor
        for b in rows:
            bid = int(b["id"])
            newest = max(newest, bid)
            if now_ts - int(b["end_ts"] or b["created_ts"] or 0) > 12 * 3600:
                continue
            gid = str(b["group_id"])
            src = source_for_group(self.s, gid) or "course"
            text = (b["text"] or "").strip()
            if src == "course_hint" and not COURSE_RE.search(text):
                continue
            if not _is_urgent_text(text):
                continue
            triggers.append(b)
        if not triggers:
            self.db.kv_set("newspaper:notice:urgent_id", str(newest))
            return 0
        sent_ids = self.db.newspaper_sent_ids("notice")
        trigger_ids = {int(b["id"]) for b in triggers}
        by_group: dict[str, list] = {}
        for b in triggers:
            by_group.setdefault(str(b["group_id"]), []).append(b)
        items, ids = [], []
        for gid, blocks in by_group.items():
            start = min(int(b["start_ts"]) for b in blocks) - URGENT_CONTEXT_BEFORE
            end = max(int(b["end_ts"] or b["start_ts"]) for b in blocks) + URGENT_CONTEXT_AFTER
            context = self.db.newspaper_blocks_between(gid, start, end)
            context = [b for b in context
                       if int(b["id"]) in trigger_ids
                       or (int(b["id"]) not in sent_ids
                           and not QUESTION_RE.search((b["text"] or "").strip()))]
            merged = _merge_blocks(context or blocks)
            base = max(blocks, key=lambda b: int(b["id"]))
            name = notice_groups(self.s).get(gid, gid)
            item = fallback_notice_item(base, "course", name)
            item["category"] = "course"
            item["urgent"] = True
            text = merged["text"].strip()
            if text:
                item["summary"] = text[:2000]
            elif merged["images"]:
                item["summary"] = f"（含 {len(merged['images'])} 张图片，具体内容见原图）"
            item["images"] = merged["images"][:9]
            item["files"] = merged["files"][:9]
            self._attach_ai_preview(item, base)
            items.append(item)
            ids.extend(int(b["id"]) for b in blocks)
        ids = sorted(set(ids))
        html, txt = render_urgent(items)
        key = f"newspaper:urgent:{max(ids)}"
        manifest = {"kind": "notice", "ids": ids, "cursor": {
            "newspaper:notice:urgent_id": str(max(ids))}}
        subject = f"某校消息 · 紧急课程提醒 · {items[0]['title']}"
        if not await self._send(subject, html, txt, key, "newspaper-urgent",
                                manifest=manifest):
            return 0
        self.db.kv_set("newspaper:notice:urgent_id", str(max(newest, max(ids))))
        logger.info("紧急课程提醒已发送：%d 条", len(items))
        return 1

    # ---------- 技术报 ----------
    def _tech_candidates(self, rows: list) -> tuple[list[dict], list[int]]:
        cfg = _tech_cfg(self.s)
        specials = _special_groups(self.s)
        keywords = [str(k).lower() for k in (cfg.get("keywords") or [])]
        names = _group_map(cfg, "groups")
        candidates, skipped = [], []

        def technical(text: str) -> bool:
            low = (text or "").lower()
            if len(low.strip()) < 6:
                return False
            if "http://" in low or "https://" in low:
                return True
            return any(k in low for k in keywords)

        for m in rows:
            gid = str(m["group_id"])
            text = (m["text"] or "").strip()
            if gid in specials:
                info = specials[gid]
                if not info["user_id"] or str(m["user_id"]) != info["user_id"]:
                    skipped.append(int(m["id"]))
                    continue
                column = "guotie"
                group = info["name"]
            else:
                column = "aia" if gid == "123456796" else "aihut"
                group = names.get(gid, gid)
            if not technical(text):
                skipped.append(int(m["id"]))
                continue
            candidates.append({
                "id": int(m["id"]), "column": column, "group": group,
                "ts": int(m["ts"]),
                "time": _dt.datetime.fromtimestamp(int(m["ts"]), TZ).strftime("%m-%d %H:%M"),
                "text": text[:2000],
            })
        return candidates, skipped

    def _tech_rows(self) -> list:
        cfg = _tech_cfg(self.s)
        groups = list(_group_map(cfg, "groups")) + list(_special_groups(self.s))
        after = int(self.db.kv_get("newspaper:tech:after_id", "0") or "0")
        return self.db.newspaper_messages(groups, _start_ts(self.s), after_id=after,
                                           limit=int(cfg.get("max_messages") or 400))

    async def _tech_tick(self) -> int:
        now = _tz_now()
        cfg = _tech_cfg(self.s)
        hour = int(cfg.get("hour") or 18)
        today = now.strftime("%Y-%m-%d")
        if time.time() - self._tech_scan_at < 300:
            return 0
        self._tech_scan_at = time.time()
        rows = self._tech_rows()
        candidates, skipped = self._tech_candidates(rows)
        # 24h 判定用消息时间，而不是自增 id。
        oldest = min((int(c.get("ts") or 0) for c in candidates), default=0)
        max_wait = int(cfg.get("max_wait_hours") or 24) * 3600
        forced_due = bool(oldest) and (now.timestamp() - oldest) >= max_wait
        scheduled = now.hour >= hour and self.db.kv_get("newspaper:tech:last_eval", "") != today
        if not scheduled and not forced_due:
            return 0
        if forced_due and not scheduled and self.db.kv_get("newspaper:tech:last_forced", "") == today:
            return 0
        if skipped:
            self.db.mark_newspaper_items("tech", skipped, "noise", state="skipped")
        if not candidates:
            self.db.kv_set("newspaper:tech:last_eval", today)
            if rows:
                self.db.kv_set("newspaper:tech:after_id", str(max(int(r["id"]) for r in rows)))
            return 0
        from info_hub.opencode_runner import run_research
        run_cfg = (self.s.newspaper or {}).get("opencode") or {}
        report = await asyncio.to_thread(
            run_research, candidates, Path(self.s.base_dir),
            agent=str(run_cfg.get("agent") or "info-hub-research"),
            model=str(run_cfg.get("model") or "dpsk/deepseek-flash"),
            timeout_seconds=int(run_cfg.get("timeout_seconds") or 600))
        if report.get("error"):
            if forced_due:
                report = self._fallback_tech(candidates)
            else:
                self.db.kv_set("newspaper:tech:last_eval", today)
                return 0
        if not report.get("ready") and not forced_due:
            self.db.kv_set("newspaper:tech:last_eval", today)
            logger.info("技术报未就绪：%s", report.get("missing") or report.get("error"))
            return 0
        html, text = render_tech(now, report, forced=bool(forced_due), s=self.s)
        key = f"newspaper:tech:{now:%Y%m%d}"
        cursor = {"newspaper:tech:after_id": str(max(int(r["id"]) for r in rows)),
                  "newspaper:tech:last_eval": today}
        if forced_due:
            cursor["newspaper:tech:last_forced"] = today
        manifest = {"kind": "tech", "ids": [c["id"] for c in candidates], "cursor": cursor}
        if not await self._send(f"某校消息 · 技术情报报 · {now:%m-%d}", html, text, key,
                                "newspaper", manifest=manifest):
            return 0
        logger.info("技术报已发送：%d 条候选", len(candidates))
        return 1

    def _fallback_tech(self, candidates: list[dict]) -> dict:
        sections: dict[str, list[dict]] = {}
        for c in candidates[:60]:
            sections.setdefault(c["column"], []).append({
                "title": _short(c["text"], 40) or "群友发言",
                "summary": _short(c["text"], 180),
                "details": [_short(c["text"], 500)] if len(c["text"]) > 180 else [],
                "sources": [], "labels": ["待补充"],
            })
        return {"ready": False,
                "summary": "AI 整理未完成，以下为原始候选。",
                "missing": ["AI 整理超时或失败，本期只保留原始候选，待补充完整解释。"],
                "sections": [{"key": k, "items": v} for k, v in sections.items()]}

    # ---------- 测试样例 ----------
    async def send_sample(self) -> int:
        if not self.ready:
            raise RuntimeError("邮件通道未就绪")
        now = _tz_now()
        items = sample_notice_items(now)
        html, text = render_notice(now, items, len(items))
        stamp = now.strftime("%Y%m%dT%H%M%S")
        n = 0
        if await self._send(f"【版式样例】某校消息 · 通知报 · {now:%m-%d}", html, text,
                            f"newspaper:sample:notice:{stamp}", "sample"):
            n += 1
        report = sample_tech_report()
        tech_html, tech_text = render_tech(now, report, forced=False, s=self.s)
        if await self._send(f"【版式样例】某校消息 · 技术情报报 · {now:%m-%d}",
                            tech_html, tech_text, f"newspaper:sample:tech:{stamp}", "sample"):
            n += 1
        return n
