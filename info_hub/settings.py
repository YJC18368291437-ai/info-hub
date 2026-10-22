"""配置加载：.env + config/*.yaml。"""
from __future__ import annotations

import json
import os
import re
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

TOPIC_KEYS = ("at_me", "notice", "chat", "memo", "cs", "bio", "link", "file")

DEFAULT_TOPIC_LABELS = {
    "at_me": "🔔@我",
    "notice": "📢通知任务",
    "chat": "💬闲聊",
    "memo": "📌备忘",
    "cs": "💻计算机",
    "bio": "🧬生物科学",
    "link": "🔗链接/网站",
    "file": "📎文件",
}


def _s(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return (v if v is not None else default).strip()


def _i(name: str, default: int) -> int:
    v = _s(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f'{name} must be an integer') from None


def _b(name: str, default: bool) -> bool:
    v = _s(name).lower()
    if not v:
        return default
    if v not in ('1','true','yes','on','y','0','false','no','off','n'):
        raise ValueError(f'{name} must be a boolean')
    return v in ("1", "true", "yes", "on", "y")


def _list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    v = _s(name)
    if not v:
        return list(default)
    return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]


@dataclass
class Settings:
    base_dir: Path
    self_qq: str
    main_qq: list[str]
    tg_token: str
    tg_chat_id: int
    tg_proxy: str | None
    threads: dict[str, int]
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    block_window_seconds: int
    block_merge_window_seconds: int
    block_max_messages: int
    pipeline_interval_seconds: int
    summary_hours: list[int]
    fewshot_limit: int
    dry_run: bool
    asr_enabled: bool
    whisper_model: str
    whisper_device: str
    mail_enabled: bool
    mail_address: str
    mail_auth_code: str
    mail_smtp_host: str
    mail_smtp_port: int
    mail_imap_host: str
    mail_imap_port: int
    mail_from_name: str
    mail_send_mode: str
    mail_poll_seconds: int
    mail_weekly_dow: str
    mail_weekly_hour: int
    mail_daily_limit: int
    groups: dict[str, dict]
    group_names: dict[str, str]
    targets: dict[str, tuple[int, int | None]]
    rules: dict[str, Any]
    tg_enabled: bool = False
    tg_image_enabled: bool = False
    glm_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    live_delivery_enabled: bool = False
    llm_enabled: bool = False
    timezone: str = 'Asia/Shanghai'
    uninterested_folder: str = '不感兴趣'
    mail_source_groups: tuple[str, ...] = ()
    mail_topics: tuple[str, ...] = ()
    mail_weekly_enabled: bool = True
    newspaper_enabled: bool = False
    newspaper_official_send: bool = False
    newspaper_legacy_events: bool = True
    newspaper: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.base_dir / "data" / "info_hub.db"

    def group_tag(self, group_id: str) -> str:
        g = self.groups.get(str(group_id)) or {}
        return str(g.get("tag") or "common")

    def group_name(self, group_id: str) -> str:
        g = self.groups.get(str(group_id)) or {}
        return str(g.get("name") or self.group_names.get(str(group_id)) or f"群{group_id}")

    def thread_id(self, topic: str) -> int | None:
        return self.threads.get(topic)

    def resolve_target(self, topic: str) -> tuple[int, int | None]:
        """分类 -> 投放目标 (chat_id, thread_id)。
        多群/多话题模式优先用 config/targets.json 的绑定；未绑定则退回默认群+话题。"""
        t = self.targets.get(topic)
        if t:
            return int(t[0]), (int(t[1]) if t[1] else None)
        return self.tg_chat_id, self.threads.get(topic)

    def known_chat_ids(self) -> set[int]:
        ids = {int(c) for c, _ in self.targets.values()}
        if self.tg_chat_id:
            ids.add(int(self.tg_chat_id))
        return ids

    def topic_label(self, topic: str) -> str:
        return self.rules.get("topics", {}).get(topic) or DEFAULT_TOPIC_LABELS.get(topic, topic)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


NAMES_FILE = BASE_DIR / "config" / "group_names.json"
TARGETS_FILE = BASE_DIR / "config" / "targets.json"


def _load_names() -> dict[str, str]:
    try:
        with NAMES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in (data or {}).items()}
    except (OSError, ValueError):
        return {}


def set_group_name(group_id: str, name: str) -> None:
    """用户自定义群名（Telegram 指令 /name 调用），写入 config/group_names.json。"""
    names = _load_names()
    names[str(group_id)] = name
    NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NAMES_FILE.open("w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, indent=2)
    if _settings is not None:
        _settings.group_names = names


def set_group_names(mapping: dict[str, str]) -> None:
    """批量写入群名（NapCat get_group_list 自动拉取），一次落盘。"""
    names = _load_names()
    names.update({str(k): str(v) for k, v in mapping.items()})
    NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NAMES_FILE.open("w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, indent=2)
    if _settings is not None:
        _settings.group_names = names


def _load_targets() -> dict[str, tuple[int, int | None]]:
    try:
        with TARGETS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out: dict[str, tuple[int, int | None]] = {}
    for k, v in (data or {}).items():
        if isinstance(v, dict) and v.get("chat"):
            out[str(k)] = (int(v["chat"]), int(v["thread"]) if v.get("thread") else None)
        elif isinstance(v, (int, str)) and str(v).lstrip("-").isdigit():
            out[str(k)] = (int(v), None)
    return out


def set_target(topic: str, chat_id: int, thread_id: int | None = None) -> None:
    """把 (群, 话题) 绑定为某分类的接收目标（Telegram 指令 /here 调用）。"""
    targets = _load_targets()
    targets[str(topic)] = (int(chat_id), int(thread_id) if thread_id else None)
    TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TARGETS_FILE.open("w", encoding="utf-8") as f:
        json.dump({k: {"chat": c, "thread": t} for k, (c, t) in targets.items()},
                  f, ensure_ascii=False, indent=2)
    if _settings is not None:
        _settings.targets = targets


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    cfg_dir = BASE_DIR / "config"
    document = _load_yaml(cfg_dir / 'groups.yaml')
    if not isinstance(document,dict):
        raise ValueError('config/groups.yaml must be a mapping with a nonempty whitelist')
    groups_raw = document.get('groups') or {}
    rules = _load_yaml(cfg_dir / "rules.yaml")
    if not isinstance(rules,dict):
        raise ValueError('config/rules.yaml must be a mapping')
    newspaper = _load_yaml(cfg_dir / "newspaper.yaml")
    if not isinstance(newspaper, dict):
        raise ValueError('config/newspaper.yaml must be a mapping')
    if _i('PORT',8082) != 8082:
        raise ValueError('PORT must be 8082 for the configured OneBot endpoint')

    raw_threads = {
        "at_me": _i("TG_THREAD_AT_ME", 0),
        "notice": _i("TG_THREAD_NOTICE", 0),
        "chat": _i("TG_THREAD_CHAT", 0),
        "memo": _i("TG_THREAD_MEMO", 0),
        "cs": _i("TG_THREAD_CS", 0),
        "bio": _i("TG_THREAD_BIO", 0),
        "link": _i("TG_THREAD_LINK", 0),
        "file": _i("TG_THREAD_FILE", 0),
    }
    threads = {k: v for k, v in raw_threads.items() if v}

    if not isinstance(groups_raw, dict) or not groups_raw:
        raise ValueError('config/groups.yaml: nonempty whitelist required; collection refused')
    if any(not re.fullmatch(r'[1-9][0-9]{4,11}', str(k)) or not isinstance(v, (dict, type(None))) for k,v in groups_raw.items()):
        raise ValueError('config/groups.yaml: invalid group ID or group configuration')
    result = Settings(
        base_dir=BASE_DIR,
        self_qq=_s("SELF_QQ"),
        main_qq=_list("MAIN_QQ"),
        tg_token=_s("TG_TOKEN"),
        tg_chat_id=_i("TG_CHAT_ID", 0),
        tg_proxy=_s("TG_PROXY") or None,
        threads=threads,
        deepseek_api_key=_s("DEEPSEEK_API_KEY"),
        deepseek_base_url=_s("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=_s("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        block_window_seconds=_i("BLOCK_WINDOW_SECONDS", 120),
        block_merge_window_seconds=_i("BLOCK_MERGE_WINDOW", 300),
        block_max_messages=_i("BLOCK_MAX_MESSAGES", 25),
        pipeline_interval_seconds=_i("PIPELINE_INTERVAL_SECONDS", 20),
        summary_hours=[int(x) for x in _list("SUMMARY_HOURS", ("8", "12", "18"))],
        fewshot_limit=_i("FEWSHOT_LIMIT", 12),
        dry_run=_b("DRY_RUN", True),
        asr_enabled=_b("ASR_ENABLED", True),
        whisper_model=_s("WHISPER_MODEL", "small"),
        whisper_device=_s("WHISPER_DEVICE", "auto"),
        mail_enabled=_b("MAIL_ENABLED", False),
        mail_address=_s("MAIL_ADDRESS"),
        mail_auth_code=_s("MAIL_AUTH_CODE"),
        mail_smtp_host=_s("MAIL_SMTP_HOST", "smtp.qq.com"),
        mail_smtp_port=_i("MAIL_SMTP_PORT", 465),
        mail_imap_host=_s("MAIL_IMAP_HOST", "imap.qq.com"),
        mail_imap_port=_i("MAIL_IMAP_PORT", 993),
        mail_from_name=_s("MAIL_FROM_NAME", "info-hub"),
        mail_send_mode=_s("MAIL_SEND_MODE", "append"),
        mail_poll_seconds=_i("MAIL_POLL_SECONDS", 300),
        mail_weekly_dow=_s("MAIL_WEEKLY_DOW", "sun"),
        mail_weekly_hour=_i("MAIL_WEEKLY_HOUR", 20),
        mail_daily_limit=_i("MAIL_DAILY_LIMIT", 0),
        groups={str(k): (v or {}) for k, v in groups_raw.items()},
        group_names=_load_names(),
        targets=_load_targets(),
        rules=rules,
        tg_enabled=_b('TG_ENABLED', False),
        tg_image_enabled=_b('TG_IMAGE_ENABLED', False),
        glm_api_key=_s('GLM_API_KEY'),
        glm_base_url=_s('GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4'),
        live_delivery_enabled=_b('LIVE_DELIVERY_ENABLED', False),
        llm_enabled=_b('LLM_ENABLED', False),
        timezone=_s('TIMEZONE', 'Asia/Shanghai'),
        uninterested_folder=_s('MAIL_UNINTERESTED_FOLDER', '不感兴趣'),
        mail_source_groups=tuple(_list('MAIL_SOURCE_GROUPS')),
        mail_topics=tuple(_list('MAIL_TOPICS')),
        mail_weekly_enabled=_b('MAIL_WEEKLY_ENABLED', True),
        newspaper_enabled=_b('NEWSPAPER_ENABLED', False),
        newspaper_official_send=_b('NEWSPAPER_OFFICIAL_SEND', False),
        newspaper_legacy_events=_b('NEWSPAPER_LEGACY_EVENTS', True),
        newspaper=newspaper,
    )
    validate_settings(result)
    return result


def validate_settings(s: Settings) -> None:
    ZoneInfo(s.timezone)
    if s.timezone != 'Asia/Shanghai':
        raise ValueError('TIMEZONE must be Asia/Shanghai for this schedule')
    if not s.self_qq.isdigit() or not s.main_qq or any(not q.isdigit() for q in s.main_qq):
        raise ValueError('SELF_QQ and MAIN_QQ must be valid account IDs')
    for name in ('block_window_seconds','block_max_messages','pipeline_interval_seconds','mail_poll_seconds'):
        if getattr(s, name) <= 0:
            raise ValueError(f'{name} must be positive')
    if s.block_merge_window_seconds < 0 or s.fewshot_limit < 0 or s.mail_daily_limit < 0:
        raise ValueError('Counts and durations cannot be negative')
    if not s.summary_hours or any(h not in range(24) for h in s.summary_hours):
        raise ValueError('SUMMARY_HOURS must contain hours 0..23')
    if s.mail_weekly_hour not in range(24) or s.mail_weekly_dow not in ('mon','tue','wed','thu','fri','sat','sun'):
        raise ValueError('Invalid weekly schedule')
    if s.mail_send_mode not in ('append','smtp'):
        raise ValueError('MAIL_SEND_MODE must be append or smtp')
    if any(not 1 <= p <= 65535 for p in (s.mail_imap_port,s.mail_smtp_port)):
        raise ValueError('Invalid mail port')
    if s.mail_enabled and (not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', s.mail_address) or not s.mail_auth_code):
        raise ValueError('MAIL_ENABLED requires valid MAIL_ADDRESS and MAIL_AUTH_CODE')
    if s.live_delivery_enabled and (s.dry_run or not s.mail_enabled):
        raise ValueError('Live delivery requires MAIL_ENABLED=true and DRY_RUN=false')
    if s.llm_enabled and not s.deepseek_api_key:
        raise ValueError('LLM_ENABLED requires DEEPSEEK_API_KEY')
    if not s.uninterested_folder or any(c in s.uninterested_folder for c in '\r\n'):
        raise ValueError('MAIL_UNINTERESTED_FOLDER must be a nonempty single-line folder name')
    if any(not re.fullmatch(r'[1-9][0-9]{4,11}', g) for g in s.mail_source_groups):
        raise ValueError('MAIL_SOURCE_GROUPS must contain valid group IDs')
    if any(t not in set(TOPIC_KEYS) | {'needs_review'} for t in s.mail_topics):
        raise ValueError('MAIL_TOPICS must contain known topic keys')
    _validate_newspaper(s)


def _validate_newspaper(s: Settings) -> None:
    n = s.newspaper or {}
    if not isinstance(n, dict):
        raise ValueError('newspaper config must be a mapping')
    meaningful = n.get('start_at') or n.get('notice') or n.get('tech')
    if not meaningful:
        if s.newspaper_enabled:
            raise ValueError('NEWSPAPER_ENABLED requires config/newspaper.yaml')
        return
    raw = n.get('start_at')
    if not isinstance(raw, str):
        raise ValueError('newspaper.start_at must be an ISO timestamp')
    try:
        from datetime import datetime
        start = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('newspaper.start_at must be an ISO timestamp') from None
    if start.tzinfo is None:
        raise ValueError('newspaper.start_at must include a timezone')
    notice = n.get('notice') or {}
    tech = n.get('tech') or {}
    opencode = n.get('opencode') or {}
    if not isinstance(notice, dict) or not isinstance(tech, dict) or not isinstance(opencode, dict):
        raise ValueError('newspaper notice, tech and opencode must be mappings')
    hours = notice.get('hours')
    if not isinstance(hours, list) or not hours or any(type(h) is not int or h not in range(24) for h in hours):
        raise ValueError('newspaper.notice.hours must contain hours 0..23')
    if type(tech.get('hour', 18)) is not int or tech.get('hour', 18) not in range(24):
        raise ValueError('newspaper.tech.hour must be 0..23')
    limits = (
        ('notice.max_items', notice.get('max_items', 80), 1, 1000),
        ('notice.classify_batch_size', notice.get('classify_batch_size', 12), 1, 100),
        ('tech.max_wait_hours', tech.get('max_wait_hours', 24), 1, 720),
        ('tech.max_messages', tech.get('max_messages', 400), 1, 5000),
        ('opencode.timeout_seconds', opencode.get('timeout_seconds', 600), 30, 3600),
    )
    for name, value, low, high in limits:
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'newspaper.{name} must be an integer in {low}..{high}')
    for section in ('course_groups', 'course_hint_groups', 'mandatory_groups',
                    'role_filter_groups', 'image_forward_groups', 'groups', 'special_groups'):
        values = notice.get(section) or tech.get(section) or {}
        if not isinstance(values, dict):
            raise ValueError(f'newspaper.{section} must be a mapping')
        for gid in values:
            if not re.fullmatch(r'[1-9][0-9]{4,11}', str(gid)):
                raise ValueError(f'newspaper.{section}: invalid group ID')
    for gid, roles in (notice.get('role_filter_groups') or {}).items():
        if (not isinstance(roles, list) or not roles
                or any(role not in ('owner', 'admin', 'member') for role in roles)):
            raise ValueError(f'newspaper.role_filter_groups.{gid} must be a nonempty role list')
    for gid, cfg in (notice.get('image_forward_groups') or {}).items():
        if not isinstance(cfg, dict):
            raise ValueError(f'newspaper.image_forward_groups.{gid} must be a mapping')
        for key, default, low, high in (
                ('max_images', 9, 1, 50), ('max_bytes', 10 * 1024 * 1024, 1024, 20 * 1024 * 1024)):
            value = cfg.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'newspaper.image_forward_groups.{gid}.{key} out of range')
        if 'name' in cfg and (not isinstance(cfg['name'], str) or not cfg['name'].strip()):
            raise ValueError(f'newspaper.image_forward_groups.{gid}.name must be a string')
    for gid, cfg in (tech.get('special_groups') or {}).items():
        if not isinstance(cfg, dict):
            raise ValueError(f'newspaper.special_groups.{gid} must be a mapping')
        user_id = cfg.get('user_id')
        if not isinstance(user_id, str) or not re.fullmatch(r'[1-9][0-9]{4,11}', user_id):
            raise ValueError(f'newspaper.special_groups.{gid}.user_id must be an account ID')
        for key in ('name', 'column_title'):
            if key in cfg and (not isinstance(cfg[key], str) or not cfg[key].strip()
                               or '\r' in cfg[key] or '\n' in cfg[key]):
                raise ValueError(f'newspaper.special_groups.{gid}.{key} must be a single-line string')
    keywords = tech.get('keywords') or []
    if not isinstance(keywords, list) or any(not isinstance(x, str) or not x.strip() for x in keywords):
        raise ValueError('newspaper.tech.keywords must be a string list')
    for key, default in (('agent', 'info-hub-research'), ('model', 'dpsk/deepseek-flash')):
        value = opencode.get(key, default)
        pattern = (r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}' if key == 'agent'
                   else r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}')
        if (not isinstance(value, str) or not re.fullmatch(pattern, value)
                or '..' in value or '//' in value):
            raise ValueError(f'newspaper.opencode.{key} is invalid')


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
