"""聚块：同一群内，时间窗内的连续消息拼成一块（宽松模式，G32 修订）。

宽松版：只要相邻两条的间隔 <= 时间窗，就并入同一块（不限发送者），
用昵称前缀区分说话人 —— 这样一问一答即使没用"回复"也能保留完整上下文。
另加块内条数上限，避免活跃群拼成超长块。
"""
from __future__ import annotations

import json
import sqlite3


def build_blocks(msgs: list[sqlite3.Row], window_seconds: int,
                 max_messages: int = 25) -> list[list[sqlite3.Row]]:
    blocks: list[list[sqlite3.Row]] = []
    cur: list[sqlite3.Row] = []
    last_ts = 0
    for m in msgs:
        ts = int(m["ts"])
        if not cur:
            cur = [m]
        elif ts - last_ts <= window_seconds and len(cur) < max_messages:
            cur.append(m)
        else:
            blocks.append(cur)
            cur = [m]
        last_ts = ts
    if cur:
        blocks.append(cur)
    return blocks


def block_text(msgs: list[sqlite3.Row]) -> str:
    """块的可读原文（昵称: 正文），供 LLM 判断与投放展示。"""
    lines: list[str] = []
    for m in msgs:
        text = (m["text"] or "").strip()
        if not text:
            continue
        who = m["nickname"] or m["user_id"]
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


def block_images(msgs: list[sqlite3.Row]) -> list[str]:
    """块里所有图片 url。"""
    out: list[str] = []
    for m in msgs:
        try:
            imgs = json.loads(m["images"] or "[]")
        except (KeyError, TypeError, ValueError):
            imgs = []
        for u in imgs:
            if u:
                out.append(str(u))
    return out


def block_files(msgs: list[sqlite3.Row]) -> list[dict]:
    """块里所有文件/视频/语音 [{url,name,kind}]。"""
    out: list[dict] = []
    for m in msgs:
        try:
            fs = json.loads(m["files"] or "[]")
        except (KeyError, TypeError, ValueError):
            fs = []
        for f in fs:
            if f:
                out.append(f)
    return out
