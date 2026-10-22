"""QQ 采集：nonebot2 监听群消息 -> 落库（独立 DB，不动答疑逻辑）。

- 图片：记录 url（供转发），文本不再出现 [图片]
- 表情：转 emoji（未收录的不显示）
- 卡片(json)：解析出 标题/摘要/链接
- 合并转发(forward)：调 get_forward_msg 展开成文本
- 语音/视频/文件：记录到 files（供转发；视频/文件归 📎文件 分类）
- 群公告等 notice：先原样打印以便探测格式
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp
from nonebot import on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from info_hub.db import get_db
from info_hub.faces import face_to_emoji
from info_hub.rules import get_rules
from info_hub.settings import get_settings, set_group_names
from info_hub.sources import accepts_source

logger = logging.getLogger("info_hub.qq")

matcher = on_message(priority=100, block=False)
notice_matcher = on_notice(priority=100, block=False)

_http: aiohttp.ClientSession | None = None
_names_loaded = False
DIAGNOSTIC_GROUPS = ("123456792", "123456795", "123456783", "123456800")


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    for name in ("model_dump", "dict"):
        fn = getattr(value, name, None)
        if callable(fn):
            try:
                out = fn()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(out, dict):
                return out
    return {}


async def _fetch_group_list(bot: Bot) -> list[dict]:
    groups = await bot.get_group_list()
    raw: list[dict] = []
    for g in groups or []:
        d = _as_dict(g)
        if not d:
            d = {k: getattr(g, k) for k in
                 ("group_id", "group_name", "member_count", "max_member_count") if hasattr(g, k)}
        if d:
            raw.append(d)
    return raw


def _names_from_raw(raw: list[dict]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for d in raw:
        gid, nm = d.get("group_id"), d.get("group_name")
        if gid is not None and nm:
            mapping[str(gid)] = str(nm)
    return mapping


def _write_group_diagnostic(bot: Bot, raw: list[dict]) -> dict[str, bool]:
    """把当前账号真实群列表落盘，供确认“配置了但不在群”的群。"""
    from info_hub.settings import get_settings

    s = get_settings()
    ids = sorted(str(d.get("group_id")) for d in raw if d.get("group_id") is not None)
    id_set = set(ids)
    present = {gid: gid in id_set for gid in DIAGNOSTIC_GROUPS}
    payload = {
        "self_id": str(getattr(bot, "self_id", "")),
        "fetched_at": int(time.time()),
        "count": len(raw),
        "group_ids": ids,
        "diagnostics": present,
        "configured_but_present": [g for g in sorted(s.groups) if g in id_set],
        "configured_but_missing": [g for g in sorted(s.groups) if g not in id_set],
        "groups": raw,
    }
    path = s.base_dir / "data" / "current_groups.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("群列表诊断已写入 %s（%d 个群）", path, len(raw))
    return present


async def dump_group_list(bot: Bot) -> dict[str, bool]:
    """显式抓取并落盘当前账号群列表；启动连接和首次消息都会调用。"""
    global _names_loaded
    raw = await _fetch_group_list(bot)
    mapping = _names_from_raw(raw)
    if mapping:
        set_group_names(mapping)
    present = _write_group_diagnostic(bot, raw)
    _names_loaded = True
    return present


async def _ensure_group_names(bot: Bot) -> None:
    """首次收到群消息时，从 NapCat 拉一次群列表，把群号->群名写入本地，
    同时落盘当前账号真实群列表（懒加载，只拉一次）。"""
    global _names_loaded
    if _names_loaded:
        return
    try:
        await dump_group_list(bot)
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉取群名失败: %s", exc)
        return
    logger.info("已从 NapCat 同步群名与群列表诊断")


async def _download(url: str) -> bytes:
    from info_hub.safe_download import download
    return await download(url, kind='audio')


def _role_filters(s) -> dict:
    return ((getattr(s, "newspaper", None) or {}).get("notice", {})
            .get("role_filter_groups") or {})


def _role_allowed(s, group_id: str, role: str) -> bool:
    """某校咨询等群只收 owner/admin；未配置 role 过滤的群一视同仁。"""
    allowed = _role_filters(s).get(str(group_id))
    if not allowed:
        return True
    return str(role or "").lower() in {str(x).lower() for x in allowed}


async def _resolve_role(bot: Bot, event: GroupMessageEvent, group_id: str, role: str) -> str:
    """NapCat 有时不填 sender.role；需要过滤的群再查一次群成员信息。"""
    if role or str(group_id) not in _role_filters(get_settings()):
        return role
    try:
        info = await bot.call_api("get_group_member_info", group_id=int(group_id),
                                  user_id=int(event.user_id), no_cache=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询群成员角色失败（%s）: %s", group_id, type(exc).__name__)
        return role
    d = _as_dict(info)
    return str(d.get("role") or getattr(info, "role", "") or role)


def _at_targets(event: GroupMessageEvent) -> set[str]:
    targets: set[str] = set()
    for seg in event.get_message():
        if seg.type == "at":
            qq = str(seg.data.get("qq", ""))
            if qq:
                targets.add(qq)
    return targets


def _segments_text(content) -> str:
    if isinstance(content, str):
        return content
    d = _as_dict(content)
    if d and not isinstance(content, list):
        if "type" in d or "data" in d:
            return _segments_text([d])
        for key in ("content", "message"):
            if key in d:
                return _segments_text(d[key])
        if isinstance(d.get("text"), str):
            return d["text"]
        return ""
    parts: list[str] = []
    if isinstance(content, list):
        for raw in content:
            s = _as_dict(raw)
            if not s:
                if isinstance(raw, str):
                    parts.append(raw)
                continue
            t = s.get("type")
            data = _as_dict(s.get("data"))
            if t == "text":
                parts.append(str(data.get("text") or s.get("text") or ""))
            elif t == "at":
                parts.append("@" + str(data.get("qq", "")))
            elif t == "image":
                parts.append("[图]")
            elif t == "face":
                parts.append(face_to_emoji(data.get("id")))
            elif t in ("forward", "node"):
                parts.append(_segments_text(
                    data.get("content") or data.get("message") or s.get("content") or data))
            elif isinstance(data.get("text"), str):
                parts.append(data["text"])
            elif isinstance(s.get("text"), str):
                parts.append(s["text"])
    return "".join(parts)


def _card_text(data: dict) -> str:
    raw = data.get("data")
    obj = None
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
    elif isinstance(raw, dict):
        obj = raw
    if not isinstance(obj, dict):
        return "[卡片]"
    found: dict[str, str] = {}

    def walk(d) -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                if k == "title" and isinstance(v, str) and "title" not in found:
                    found["title"] = v
                elif k in ("desc", "description") and isinstance(v, str) and "desc" not in found:
                    found["desc"] = v
                elif k in ("url", "qqdocurl", "jumpUrl") and isinstance(v, str) and "url" not in found:
                    found["url"] = v
                walk(v)
        elif isinstance(d, list):
            for x in d:
                walk(x)

    walk(obj.get("meta"))
    bits = [found.get("title", ""), found.get("desc", ""), found.get("url", "") or obj.get("prompt", "")]
    text = " ".join(b for b in bits if b)
    return f"【卡片】{text}" if text else "[卡片]"


def _forward_nodes(res) -> list:
    """兼容 NapCat get_forward_msg 的 messages/message/data 三种外层。"""
    if isinstance(res, list):
        return res
    d = _as_dict(res)
    for key in ("messages", "message"):
        v = d.get(key)
        if isinstance(v, list):
            return v
    if isinstance(d.get("data"), list):
        return d["data"]
    data = _as_dict(d.get("data"))
    for key in ("messages", "message"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return [data] if data else []


def _forward_line(node) -> tuple[str, str]:
    n = _as_dict(node)
    if not n:
        return "", ""
    d = n
    if n.get("type") == "node" or "data" in n:
        inner = _as_dict(n.get("data"))
        if inner:
            d = inner
    sender = _as_dict(d.get("sender")) or _as_dict(n.get("sender"))
    name = (sender.get("nickname") or sender.get("card")
            or d.get("nickname") or d.get("sender_name") or "")
    content = d.get("content")
    if content is None:
        content = d.get("message")
    if content is None:
        inner = _as_dict(d.get("data"))
        content = inner.get("content") if inner else None
        if content is None and inner:
            content = inner.get("message")
    if content is None:
        content = d.get("text")
    return str(name or ""), _segments_text(content)


async def _forward_text(bot: Bot, seg) -> str:
    fid = seg.data.get("id") or seg.data.get("resid")
    if not fid:
        return "[合并转发]"
    res = None
    for kw in ("id", "message_id"):
        try:
            res = await bot.call_api("get_forward_msg", **{kw: fid})
            if res:
                break
        except Exception:  # noqa: BLE001
            continue
    nodes = _forward_nodes(res)
    lines: list[str] = []
    for node in nodes[:50]:
        name, txt = _forward_line(node)
        if name or txt:
            lines.append(f"{name}: {txt}".strip(": "))
    if lines:
        return "〖转发〗\n" + "\n".join(lines)
    evidence = (json.dumps(res, ensure_ascii=False, default=str)
                if res is not None else "no response")
    return "[合并转发·无法解析] " + evidence[:1200]


async def _parse_message(bot: Bot, event: GroupMessageEvent) -> tuple[str, list[str], list[dict]]:
    parts: list[str] = []
    images: list[str] = []
    files: list[dict] = []
    for seg in event.get_message():
        t = seg.type
        d = seg.data
        if t == "text":
            parts.append(str(d.get("text", "")))
        elif t == "at":
            parts.append("@" + str(d.get("qq", "")))
        elif t == "image":
            summary = str(d.get("summary") or "")
            if str(d.get("sub_type") or "0") == "1" or "表情" in summary:
                continue  # 表情包/动画表情不转发
            url = d.get("url") or d.get("file")
            if url:
                images.append(str(url))
        elif t == "face":
            emo = face_to_emoji(d.get("id"))
            if emo:
                parts.append(emo)
        elif t == "record":
            url = d.get("url") or d.get("file")
            transcribed = False
            s = get_settings()
            if s.asr_enabled and url and str(url).startswith("http"):
                try:
                    audio = await _download(str(url))
                    from info_hub.asr import transcribe_silk

                    txt = await asyncio.to_thread(
                        transcribe_silk, audio, s.whisper_model, s.whisper_device
                    )
                    if txt:
                        parts.append(f"[语音] {txt}")
                        transcribed = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("语音转文字失败: %s", type(exc).__name__)
            if not transcribed and url:
                files.append({"url": str(url), "name": d.get("file") or "语音", "kind": "record"})
        elif t == "video":
            url = d.get("url") or d.get("file")
            if url:
                files.append({"url": str(url), "name": d.get("file") or "视频", "kind": "video"})
        elif t == "file":
            url = d.get("url") or d.get("file")
            if url:
                files.append({"url": str(url), "name": d.get("name") or d.get("file") or "文件", "kind": "file"})
        elif t == "json":
            parts.append(_card_text(d))
        elif t == "forward":
            parts.append(await _forward_text(bot, seg))
        elif t == "reply":
            continue
    return "".join(parts).strip(), images, files


@matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent) -> None:
    s = get_settings()
    group_id = str(event.group_id)
    if not accepts_source(s, str(event.self_id), group_id):
        return

    await _ensure_group_names(bot)

    role = str(getattr(event.sender, "role", "") or "")
    role = await _resolve_role(bot, event, group_id, role)
    if not _role_allowed(s, group_id, role):
        return

    targets = _at_targets(event)
    main = set(s.main_qq)
    at_me = bool(targets & main)
    at_all = "all" in targets

    text, images, files = await _parse_message(bot, event)
    links = get_rules().extract_links(text)

    msg = {
        "account_id": str(event.self_id),
        "msg_id": int(event.message_id),
        "group_id": group_id,
        "user_id": str(event.user_id),
        "role": role,
        "nickname": event.sender.card or event.sender.nickname or str(event.user_id),
        "ts": int(getattr(event, "time", None) or time.time()),
        "text": text,
        "links": links,
        "images": images,
        "files": files,
        "at_me": at_me or at_all,
        "at_all": at_all,
        "reply_to": int(event.reply.message_id) if getattr(event, "reply", None) else None,
        "raw": {"self_id": str(event.self_id)},
    }
    try:
        get_db().add_message(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("入库失败: %s", type(exc).__name__)


@notice_matcher.handle()
async def _on_notice(event) -> None:
    s = get_settings()
    gid = str(getattr(event,'group_id',''))
    if not accepts_source(s,str(getattr(event,'self_id','')),gid):
        return
    # A notice without readable body is evidence, not silently dropped.
    import hashlib
    raw = event.model_dump() if hasattr(event,'model_dump') else event.dict()
    safe = {k:v for k,v in raw.items() if k in ('notice_type','sub_type','time','group_id','message_id')}
    text = '[群事件待确认] ' + json.dumps(safe, ensure_ascii=False)
    msg_id = -int(hashlib.sha256(text.encode()).hexdigest()[:14],16)
    get_db().add_message(dict(account_id=s.self_qq,msg_id=msg_id,group_id=gid,user_id='',
                             ts=int(getattr(event,'time',None) or time.time()),text=text,at_all=True))
