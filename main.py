from __future__ import annotations

import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, quote
from uuid import uuid4

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


DANBOORU_BASE_URL = "https://danbooru.donmai.us"
DANBOORU_POSTS_API = f"{DANBOORU_BASE_URL}/posts.json"
DANBOORU_TAGS_API = f"{DANBOORU_BASE_URL}/tags.json"
DANBOORU_TAG_ALIASES_API = f"{DANBOORU_BASE_URL}/tag_aliases.json"

# 不伪装成浏览器。httpx 的 TLS 指纹不是真浏览器，泛化 Mozilla UA 反而可能触发 CDN/WAF。
DANBOORU_DEFAULT_USER_AGENT = "AstrBot-Danbooru-TagAPI/1.0"

# Danbooru tag 中会出现的安全字符。普通用户不能借 tag 参数注入 API 元标签。
DANBOORU_TAG_RE = re.compile(r"^[a-z0-9_()'!+\-.]+$")
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
SCORE_SUFFIX_RE = re.compile(r"^(?P<tags>.*?)(?::(?P<score>\d+))\s*$")

# 只下载常见静态图片，避免视频、Flash、奇怪格式让 QQ/OneBot 传图失败。
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# 用户输入禁止直接控制这些 API 元标签；末尾 :数字仅作本地 score 过滤。
BLOCKED_META_PREFIXES = (
    "rating:",
    "score:",
    "order:",
    "status:",
    "filetype:",
    "limit:",
    "page:",
    "id:",
    "date:",
)

TAG_CATEGORY_NAMES = {
    0: "general",
    1: "artist",
    3: "copyright",
    4: "character",
    5: "meta",
}


DEFAULT_CHINESE_LOOKUP_URL = "https://tagsuggest.zeabur.app/api/tags/suggest"

# 本地可维护数据
DATA_DIR = Path("data/danbooru")
LOCAL_ALIASES_FILE = DATA_DIR / "manual_aliases.json"
SUGGEST_LOG_FILE = DATA_DIR / "alias_suggestions.jsonl"


@dataclass(slots=True)
class TagLookupResult:
    """单个输入项的实时 tag API / 中文对照解析结果。"""

    input_tag: str
    official_tag: str | None = None
    suggestions: list[dict[str, Any]] | None = None
    api_failed: bool = False
    # 中文词命中多个高相关官方 tag 时为 True，触发交互引导而非静默搜图。
    ambiguous: bool = False
    source: str = "english"  # "english" | "manual" | "chinese"


@dataclass(slots=True)
class TagResolution:
    """一次用户查询中所有 tag 的解析结果。"""

    resolved_tags: list[str]
    unknown_tags: list[str]
    ignored_tags: list[str]
    suggestions: dict[str, list[dict[str, Any]]]
    api_failed: bool = False
    # 存在需要用户确认的歧义项时填充。key = 用户输入词。
    ambiguous_terms: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class DanbooruPlugin(Star):
    """使用 Danbooru tags API 进行实时标签校验和自动补全的图片插件。

    设计原则：
    1. 不依赖 LLM 猜 tag，也不把整张对照表塞进上下文。
    2. 中文/别名查找在插件代码侧完成（手册词典 + 本地 JSON + 可选对照 API）。
    3. 只自动接受“精确命中”或“仅下划线/连字符差异”的真实 tag。
    4. 歧义（同一中文词对应多个热门官方 tag）时列出候选并引导用户，不静默选择。
    5. 为兼容受限账号，posts.json 默认只发送最多两个普通 tag。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.image_dir = Path("data/temp/danbooru")
        self.image_dir.mkdir(parents=True, exist_ok=True)

        # {sender_id: 上次成功请求的 monotonic 时间}
        self.user_last_request: dict[str, float] = {}

        # {标准化输入: (过期 monotonic 时间, 查询结果)}
        self.tag_lookup_cache: dict[str, tuple[float, TagLookupResult]] = {}

        # 中文对照结果缓存，与 tag 校验缓存分开，避免互相污染。
        self.chinese_lookup_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

        # 同一查询保留最近发送过的 post ID，避免连续搜同一 tag 时重复。
        self.recent_post_ids: dict[str, deque[int]] = {}

        # tag 额度已用满、不能追加 random:N 时，先记住该查询最新结果的 ID 上界，
        # 后续用 page=b<ID> 在历史范围内随机切片。无需额外 count API。
        self.query_id_ceilings: dict[str, int] = {}

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._local_aliases: dict[str, str] = {}
        self._load_local_aliases()

    # ---------------------------------------------------------------------
    # 配置与通用请求
    # ---------------------------------------------------------------------

    def _config_str(self, key: str, default: str = "") -> str:
        return str(self.config.get(key, default) or "").strip()

    def _config_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off", ""}:
                return False
        return bool(value)

    def _config_mapping(self, key: str) -> dict[str, str]:
        """读取 WebUI dict 配置，或兼容手写 JSON 字符串。"""
        raw = self.config.get(key, {})

        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[Danbooru] 配置 %s 不是合法 JSON，已忽略", key)
                return {}

        if not isinstance(raw, dict):
            return {}

        mapping: dict[str, str] = {}
        for source, target in raw.items():
            source_text = str(source or "").strip().casefold()
            target_text = str(target or "").strip().lower()
            if source_text and target_text:
                mapping[source_text] = target_text
        return mapping


    # ------------------------------------------------------------------
    # 本地别名文件 + 建议日志
    # ------------------------------------------------------------------

    def _load_local_aliases(self) -> None:
        self._local_aliases = {}
        if not LOCAL_ALIASES_FILE.exists():
            return
        try:
            raw = json.loads(LOCAL_ALIASES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[Danbooru] 读取本地别名失败: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        for source, target in raw.items():
            sk = str(source or "").strip().casefold()
            tv = str(target or "").strip().lower()
            if sk and tv:
                self._local_aliases[sk] = tv
        logger.info("[Danbooru] 已加载本地别名 %s 条", len(self._local_aliases))

    def _save_local_aliases(self) -> bool:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = dict(sorted(self._local_aliases.items(), key=lambda x: x[0]))
            LOCAL_ALIASES_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            logger.warning("[Danbooru] 保存本地别名失败: %s", exc)
            return False

    def _merged_aliases(self) -> dict[str, str]:
        """WebUI 配置 + 本地文件；本地优先。"""
        merged = self._config_mapping("manual_tag_aliases")
        merged.update(self._local_aliases)
        return merged

    def _append_suggestion_log(
        self,
        *,
        action: str,
        input_term: str,
        candidates: list[dict[str, Any]] | None = None,
        used: str | None = None,
        sender_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self._config_bool("enable_alias_suggestion_log", True):
            return
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            record: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "action": action,
                "input": input_term,
                "used": used,
                "user": sender_id or "",
                "candidates": [
                    {
                        "name": str(c.get("name") or ""),
                        "cn_name": str(c.get("cn_name") or ""),
                        "post_count": int(c.get("post_count") or c.get("count") or 0),
                        "category": c.get("category"),
                    }
                    for c in (candidates or [])[:12]
                ],
            }
            if extra:
                record["extra"] = extra
            with SUGGEST_LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("[Danbooru] 写建议日志失败: %s", exc)

    def _is_alias_admin(self, event: AstrMessageEvent) -> bool:
        sender = self._get_sender_id(event)
        raw = self._config_str("alias_admin_ids", "")
        if raw:
            ids = {x.strip() for x in re.split(r"[\s,;，；]+", raw) if x.strip()}
            if sender in ids:
                return True
        for attr in ("is_admin", "is_super_admin"):
            try:
                method = getattr(event, attr, None)
                if callable(method) and method():
                    return True
            except Exception:
                pass
        try:
            if getattr(event, "role", None) in {"admin", "owner"}:
                return True
        except Exception:
            pass
        return False

    def _get_api_auth(self) -> httpx.BasicAuth | None:
        username = self._config_str("danbooru_username")
        api_key = self._config_str("danbooru_api_key")
        if username and api_key:
            return httpx.BasicAuth(username, api_key)
        return None

    def _get_api_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._config_str(
                "danbooru_user_agent",
                DANBOORU_DEFAULT_USER_AGENT,
            ),
            "Accept": "application/json",
        }

    def _api_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=12.0, read=25.0, write=12.0, pool=12.0)

    # ---------------------------------------------------------------------
    # 用户输入 -> 可验证的 Danbooru tag
    # ---------------------------------------------------------------------

    def _extract_keyword(self, message: str, command: str) -> str:
        """从 /danbooru hatsune_miku 中取出 hatsune_miku。"""
        message = (message or "").strip()
        for prefix in (f"/{command}", command):
            if message.startswith(prefix):
                return message[len(prefix):].strip()
        return ""

    def _normalize_user_input(self, value: str) -> str:
        """统一常见分隔符；不会把自然语言硬转为 tag。"""
        value = (value or "").strip()
        return re.sub(r"[，,、；;\n\t]+", " ", value)

    def _parse_score_suffix(self, value: str) -> tuple[str, int]:
        """解析末尾的 :数字；score 只在本地过滤，不进入 API 查询。"""
        value = (value or "").strip()
        match = SCORE_SUFFIX_RE.fullmatch(value)
        if match is None:
            return value, 0
        score = min(1_000_000, max(0, int(match.group("score"))))
        return match.group("tags").strip(), score

    def _safe_tag_token(self, token: str) -> str | None:
        """提取一个允许拿去访问 tags API 的普通 tag token。"""
        token = (token or "").strip().lower()
        if not token:
            return None

        normalized = token.lstrip("-")
        if normalized.startswith(BLOCKED_META_PREFIXES):
            logger.info("[Danbooru] 拦截用户元标签: %r", token)
            return None

        # 本插件不让用户直接加负标签，避免绕开安全策略或额外消耗查询额度。
        if token.startswith("-") or ":" in token:
            return None

        if not DANBOORU_TAG_RE.fullmatch(token):
            return None

        return token

    def _manual_alias_terms(self, user_input: str) -> list[str]:
        """应用可配置的人工词典，优先于自动中文对照。

        配置示例：
        "manual_tag_aliases": {
          "初音未来": "hatsune_miku",
          "白丝": "white_thighhighs",
          "初音未来白丝": "hatsune_miku white_thighhighs"
        }
        """
        normalized = self._normalize_user_input(user_input)
        aliases = self._merged_aliases()
        if not normalized:
            return []

        whole = normalized.casefold()
        if whole in aliases:
            return aliases[whole].split()

        terms: list[str] = []
        for raw_term in normalized.split():
            replacement = aliases.get(raw_term.casefold(), raw_term)
            terms.extend(replacement.split())
        return terms

    def _spelling_variants(self, tag: str) -> list[str]:
        """只尝试可解释的下划线合并修正，不做角色名模糊猜测。"""
        variants = [tag]
        parts = tag.replace("-", "_").split("_")
        if 2 <= len(parts) <= 6:
            for index in range(len(parts) - 1):
                merged = parts[:index] + [parts[index] + parts[index + 1]] + parts[index + 2 :]
                variants.append("_".join(merged))

        deduped: list[str] = []
        seen: set[str] = set()
        for value in variants:
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        return deduped

    @staticmethod
    def _compact_tag(tag: str) -> str:
        return re.sub(r"[_-]", "", tag.lower())

    def _cache_ttl_seconds(self) -> int:
        return min(86_400, max(0, self._config_int("tag_cache_ttl_seconds", 900)))

    def _cache_get(self, key: str) -> TagLookupResult | None:
        cached = self.tag_lookup_cache.get(key)
        if cached is None:
            return None
        expires_at, result = cached
        if time.monotonic() >= expires_at:
            self.tag_lookup_cache.pop(key, None)
            return None
        return result

    def _cache_set(self, key: str, result: TagLookupResult) -> None:
        ttl = self._cache_ttl_seconds()
        if ttl > 0:
            self.tag_lookup_cache[key] = (time.monotonic() + ttl, result)

    def _chinese_cache_get(self, key: str) -> list[dict[str, Any]] | None:
        cached = self.chinese_lookup_cache.get(key)
        if cached is None:
            return None
        expires_at, result = cached
        if time.monotonic() >= expires_at:
            self.chinese_lookup_cache.pop(key, None)
            return None
        return result

    def _chinese_cache_set(self, key: str, result: list[dict[str, Any]]) -> None:
        ttl = self._cache_ttl_seconds()
        if ttl > 0:
            self.chinese_lookup_cache[key] = (time.monotonic() + ttl, result)

    # ---------------------------------------------------------------------
    # 中文对照查找（插件侧，不经过 LLM）
    # ---------------------------------------------------------------------

    async def _fetch_chinese_candidates(self, term: str) -> list[dict[str, Any]]:
        """调用对照 suggest API，把中文词映射到官方 name + cn_name + count。

        结果只作候选；最终是否采用仍由后续逻辑决定（唯一高置信 / 歧义引导）。
        """
        if not self._config_bool("enable_chinese_lookup", True):
            return []

        term = (term or "").strip()
        if not term or not CJK_RE.search(term):
            return []

        cache_key = term.casefold()
        cached = self._chinese_cache_get(cache_key)
        if cached is not None:
            return cached

        base_url = self._config_str("chinese_lookup_url", DEFAULT_CHINESE_LOOKUP_URL)
        if not base_url:
            return []

        limit = min(30, max(5, self._config_int("chinese_lookup_limit", 12)))

        try:
            async with httpx.AsyncClient(
                timeout=self._api_timeout(),
                follow_redirects=True,
                headers=self._get_api_headers(),
            ) as client:
                response = await client.get(
                    base_url,
                    params={"q": term},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("[Danbooru] 中文对照查询失败: term=%r, error=%s", term, exc)
            self._chinese_cache_set(cache_key, [])
            return []

        raw_list: list[Any]
        if isinstance(payload, dict):
            raw_list = payload.get("results") or payload.get("data") or []
        elif isinstance(payload, list):
            raw_list = payload
        else:
            raw_list = []

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in raw_list:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip().lower()
            if not name or name in seen or not DANBOORU_TAG_RE.fullmatch(name):
                continue
            seen.add(name)
            count = int(row.get("count") or row.get("post_count") or 0)
            category = row.get("category")
            try:
                category = int(category) if category is not None else None
            except (TypeError, ValueError):
                category = None
            candidates.append(
                {
                    "name": name,
                    "cn_name": str(row.get("cn_name") or "").strip(),
                    "post_count": count,
                    "category": category,
                }
            )

        candidates.sort(key=lambda r: int(r.get("post_count") or 0), reverse=True)
        candidates = candidates[:limit]
        self._chinese_cache_set(cache_key, candidates)
        logger.info(
            "[Danbooru] 中文对照命中: term=%r, candidates=%s",
            term,
            [c["name"] for c in candidates[:5]],
        )
        return candidates

    def _pick_chinese_official(
        self,
        term: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[str | None, bool, list[dict[str, Any]]]:
        """从中文候选里决定：唯一官方 tag / 歧义 / 无可用。

        返回 (official_tag | None, is_ambiguous, suggestions)
        """
        if not candidates:
            return None, False, []

        threshold = max(2, self._config_int("chinese_ambiguity_threshold", 3))
        original = term.strip()

        # 1) cn_name 精确匹配优先
        exact = [
            c
            for c in candidates
            if str(c.get("cn_name") or "").strip() == original
            or str(c.get("cn_name") or "").strip().casefold() == original.casefold()
        ]
        if len(exact) == 1:
            return exact[0]["name"], False, candidates[:8]
        if len(exact) > 1:
            # 多个精确中文名（少见），按热度取前 threshold 视为歧义
            if len(exact) >= threshold:
                return None, True, exact[:12]
            return exact[0]["name"], False, exact[:8]

        # 2) 无精确时：若头部明显领先且总数不多，可自动采用第一名
        top = candidates[0]
        top_count = int(top.get("post_count") or 0)
        if len(candidates) == 1 and top_count > 0:
            return top["name"], False, candidates[:8]

        # 头部显著领先（例如第一名比第二名高一个数量级且很高）时放宽
        if len(candidates) >= 2:
            second_count = int(candidates[1].get("post_count") or 0)
            if top_count >= 5000 and (second_count == 0 or top_count >= second_count * 5):
                return top["name"], False, candidates[:8]

        # 3) 否则视为歧义，交给用户选择
        if len(candidates) >= threshold:
            return None, True, candidates[:12]

        # 候选很少但仍不唯一：也引导，避免静默错选
        if len(candidates) >= 2:
            return None, True, candidates[:12]

        return top["name"], False, candidates[:8]

    async def _fetch_tag_candidates(
        self,
        client: httpx.AsyncClient,
        pattern: str,
    ) -> list[dict[str, Any]]:
        """调用 tags.json，拿到按帖子数排序的真实候选。"""
        response = await client.get(
            DANBOORU_TAGS_API,
            params={
                "search[name_matches]": pattern,
                "search[hide_empty]": "true",
                "limit": min(50, max(5, self._config_int("tag_suggestion_limit", 15))),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"tags.json 返回 {type(payload).__name__}，不是列表")

        candidates = [row for row in payload if isinstance(row, dict)]
        candidates.sort(key=lambda row: int(row.get("post_count") or 0), reverse=True)
        return candidates

    async def _lookup_tag_alias(
        self,
        client: httpx.AsyncClient,
        tag: str,
    ) -> str | None:
        """查已批准的旧 tag alias；失败时静默回退到正常候选逻辑。"""
        try:
            response = await client.get(
                DANBOORU_TAG_ALIASES_API,
                params={
                    "search[antecedent_name]": tag,
                    "search[status]": "active",
                    "limit": 5,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("[Danbooru] tag alias 查询失败 %r: %s", tag, exc)
            return None

        if not isinstance(payload, list):
            return None

        for row in payload:
            if not isinstance(row, dict):
                continue
            antecedent = str(row.get("antecedent_name") or "").lower()
            consequent = str(row.get("consequent_name") or "").lower()
            if antecedent == tag and consequent and DANBOORU_TAG_RE.fullmatch(consequent):
                return consequent
        return None

    async def _resolve_one_tag(
        self,
        client: httpx.AsyncClient,
        raw_tag: str,
        sender_id: str = "",
    ) -> TagLookupResult:
        """将一个输入项解析为官方 tag；只接受可证明的匹配。"""
        raw_tag = raw_tag.strip()
        cache_key = raw_tag.casefold()
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        # ---------- 中文 / 日文路径 ----------
        if CJK_RE.search(raw_tag):
            if not self._config_bool("enable_chinese_lookup", True):
                result = TagLookupResult(input_tag=raw_tag, source="chinese")
                self._cache_set(cache_key, result)
                return result

            candidates = await self._fetch_chinese_candidates(raw_tag)
            official, ambiguous, suggestions = self._pick_chinese_official(raw_tag, candidates)

            if official:
                # 可选：再过一遍英文 normalize / alias（提高一致性）
                safe = self._safe_tag_token(official)
                if safe:
                    result = TagLookupResult(
                        input_tag=raw_tag,
                        official_tag=safe,
                        suggestions=suggestions,
                        ambiguous=False,
                        source="chinese",
                    )
                    self._cache_set(cache_key, result)
                    self._append_suggestion_log(
                        action="auto_accept",
                        input_term=raw_tag,
                        candidates=suggestions,
                        used=safe,
                        sender_id=sender_id,
                    )
                    return result

            result = TagLookupResult(
                input_tag=raw_tag,
                official_tag=None,
                suggestions=suggestions or candidates[:10],
                ambiguous=ambiguous,
                source="chinese",
            )
            self._cache_set(cache_key, result)
            if ambiguous:
                self._append_suggestion_log(
                    action="ambiguity",
                    input_term=raw_tag,
                    candidates=result.suggestions,
                    sender_id=sender_id,
                )
            elif result.suggestions:
                self._append_suggestion_log(
                    action="no_match_with_candidates",
                    input_term=raw_tag,
                    candidates=result.suggestions,
                    sender_id=sender_id,
                )
            return result

        # ---------- 英文路径（原有逻辑） ----------
        safe_tag = self._safe_tag_token(raw_tag.lower())
        if safe_tag is None:
            return TagLookupResult(input_tag=raw_tag)

        all_candidates: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        try:
            for variant in self._spelling_variants(safe_tag):
                # 先精确查；找不到时再查前缀，用于给用户显示 autocomplete 候选。
                candidates = await self._fetch_tag_candidates(client, variant)
                if not candidates:
                    candidates = await self._fetch_tag_candidates(client, f"{variant}*")

                for item in candidates:
                    name = str(item.get("name") or "").lower()
                    if name and name not in seen_names:
                        seen_names.add(name)
                        all_candidates.append(item)

            all_candidates.sort(key=lambda row: int(row.get("post_count") or 0), reverse=True)

        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[Danbooru] tags API 校验失败: tag=%r, error=%s", safe_tag, exc)
            result = TagLookupResult(input_tag=raw_tag, api_failed=True)
            self._cache_set(cache_key, result)
            return result

        exact = next(
            (
                str(item.get("name"))
                for item in all_candidates
                if str(item.get("name") or "").lower() == safe_tag
            ),
            None,
        )
        if exact:
            result = TagLookupResult(
                input_tag=raw_tag,
                official_tag=exact,
                suggestions=all_candidates[:10],
                source="english",
            )
            self._cache_set(cache_key, result)
            return result

        # 仅接受下划线或连字符造成的差异，例如 white_thigh_highs -> white_thighhighs。
        compact = self._compact_tag(safe_tag)
        normalized = next(
            (
                str(item.get("name"))
                for item in all_candidates
                if self._compact_tag(str(item.get("name") or "")) == compact
            ),
            None,
        )
        if normalized:
            logger.info("[Danbooru] tag 拼写规范化: %r -> %r", safe_tag, normalized)
            result = TagLookupResult(
                input_tag=raw_tag,
                official_tag=normalized,
                suggestions=all_candidates[:10],
                source="english",
            )
            self._cache_set(cache_key, result)
            return result

        alias = await self._lookup_tag_alias(client, safe_tag)
        if alias:
            logger.info("[Danbooru] tag alias 规范化: %r -> %r", safe_tag, alias)
            result = TagLookupResult(
                input_tag=raw_tag,
                official_tag=alias,
                suggestions=all_candidates[:10],
                source="english",
            )
            self._cache_set(cache_key, result)
            return result

        result = TagLookupResult(
            input_tag=raw_tag,
            suggestions=all_candidates[:10],
            source="english",
        )
        self._cache_set(cache_key, result)
        return result

    async def _resolve_user_tags(
        self,
        user_input: str,
        sender_id: str = "",
        max_terms_override: int | None = None,
    ) -> TagResolution:
        """处理用户的全部普通 tag，并确保最终不超过 API 查询项限制。"""
        input_terms = self._manual_alias_terms(user_input)
        configured_max = min(10, max(0, self._config_int("max_api_query_terms", 2)))
        max_terms = (
            configured_max
            if max_terms_override is None
            else min(configured_max, max(0, max_terms_override))
        )

        ignored_terms = input_terms[max_terms:] if max_terms >= 0 else []
        input_terms = input_terms[:max_terms]

        if ignored_terms:
            logger.warning(
                "[Danbooru] 标签过多：仅解析前 %s 个，忽略 %r",
                max_terms,
                ignored_terms,
            )

        if not input_terms:
            return TagResolution([], [], ignored_terms, {})

        resolved: list[str] = []
        unknown: list[str] = []
        suggestions: dict[str, list[dict[str, Any]]] = {}
        ambiguous_terms: dict[str, list[dict[str, Any]]] = {}
        api_failed = False

        async with httpx.AsyncClient(
            timeout=self._api_timeout(),
            follow_redirects=True,
            auth=self._get_api_auth(),
            headers=self._get_api_headers(),
        ) as client:
            for raw_term in input_terms:
                lookup = await self._resolve_one_tag(client, raw_term, sender_id=sender_id)
                api_failed = api_failed or lookup.api_failed

                if lookup.ambiguous and lookup.suggestions:
                    ambiguous_terms[raw_term] = lookup.suggestions
                    suggestions[raw_term] = lookup.suggestions
                    continue

                if lookup.official_tag:
                    if lookup.official_tag not in resolved:
                        resolved.append(lookup.official_tag)
                    continue

                if lookup.api_failed and self._config_bool("allow_unverified_tags_on_api_error", False):
                    safe = self._safe_tag_token(raw_term)
                    if safe and safe not in resolved:
                        logger.warning("[Danbooru] tags API 故障，按配置放行未验证 tag: %r", safe)
                        resolved.append(safe)
                    continue

                unknown.append(raw_term)
                if lookup.suggestions:
                    suggestions[raw_term] = lookup.suggestions

        return TagResolution(
            resolved_tags=resolved,
            unknown_tags=unknown,
            ignored_tags=ignored_terms,
            suggestions=suggestions,
            api_failed=api_failed,
            ambiguous_terms=ambiguous_terms,
        )

    def _format_candidate_line(self, item: dict[str, Any]) -> str:
        name = str(item.get("name") or "")
        count = int(item.get("post_count") or item.get("count") or 0)
        category = TAG_CATEGORY_NAMES.get(item.get("category"), "other")
        cn = str(item.get("cn_name") or "").strip()
        if cn and cn != name:
            return f"{name}  [{cn}]  ({category}, {count})"
        return f"{name}  ({category}, {count})"

    def _format_suggestions(self, resolution: TagResolution, limit: int = 6) -> str:
        """把候选以可复制的形式输出给用户。"""
        lines: list[str] = []
        for source, candidates in resolution.suggestions.items():
            formatted: list[str] = []
            for item in candidates[:limit]:
                line = self._format_candidate_line(item)
                if line:
                    formatted.append(line)
            if formatted:
                lines.append(f"「{source}」候选：")
                lines.extend(f"  · {x}" for x in formatted)
        return "\n".join(lines)

    def _format_ambiguity_guidance(self, resolution: TagResolution) -> str:
        """多个热门官方 tag 对应同一中文词时，引导用户精确选择。"""
        lines = [
            "找到多个相关的官方 tag，请直接复制其中一个再发 /danbooru，或补充更具体的名称：",
        ]
        for source, candidates in resolution.ambiguous_terms.items():
            lines.append(f"\n「{source}」可能对应：")
            for item in candidates[:8]:
                lines.append(f"  · {self._format_candidate_line(item)}")
        lines.append("\n也可用 /danbooru_tags 关键词 继续查看更多候选。")
        lines.append("管理员可用 /danbooru_alias 中文 英文tag 写入本地词典。")
        return "\n".join(lines)

    def _format_resolution_error(self, resolution: TagResolution) -> str:
        if resolution.api_failed:
            return "Danbooru 标签校验接口暂时不可用，本次没有发送未验证标签。稍后再试。"

        lines = ["没有识别到可验证的 Danbooru tag。"]
        if resolution.unknown_tags:
            lines.append("未识别：" + "、".join(resolution.unknown_tags))

        suggestions = self._format_suggestions(resolution)
        if suggestions:
            lines.append("候选（请复制规范 tag 重试）：\n" + suggestions)
        elif any(CJK_RE.search(item) for item in resolution.unknown_tags):
            lines.append(
                "中文对照未命中可用结果。可尝试：\n"
                "1) 使用更常见的角色/作品译名\n"
                "2) 在 manual_tag_aliases 里添加映射\n"
                "3) 用 /danbooru_tags 关键词 查看候选"
            )
        else:
            lines.append("可先用 /danbooru_tags 关键词 查看 Danbooru 的真实候选标签。")

        return "\n".join(lines)

    # ---------------------------------------------------------------------
    # Danbooru posts 搜索与本地过滤
    # ---------------------------------------------------------------------

    def _get_local_tag_filters(self, search_mode: str = "safe") -> tuple[set[str], set[str]]:
        """普通 default tag 在本地过滤，避免占用 posts API 的 tag 上限。"""
        positive_raw = self._normalize_user_input(
            self._config_str("default_positive_tags", "")
        )
        negative_raw = self._normalize_user_input(
            self._config_str(
                "default_negative_tags",
                "-ai_generated -comic -4koma -multiple_views -text -speech_bubble -translated",
            )
        )

        required: set[str] = set()
        excluded: set[str] = set()

        for token in positive_raw.split():
            safe = self._safe_tag_token(token)
            if safe:
                required.add(safe)

        for token in negative_raw.split():
            token = token.strip().lower()
            if token.startswith("-"):
                raw = token[1:]
                if DANBOORU_TAG_RE.fullmatch(raw):
                    excluded.add(raw)

        if search_mode == "comic":
            # 漫画本来就常带这些 tag；沿用普通搜图排除项会把结果全部筛掉。
            excluded.difference_update(
                {"comic", "4koma", "multiple_views", "text", "speech_bubble", "translated"}
            )

        return required, excluded

    def _build_query_tags(
        self,
        verified_tags: list[str],
        *,
        search_mode: str,
        random_count: int = 0,
    ) -> str:
        """构造 posts 查询；score 永远不发送给 Danbooru。"""
        rating = "rating:q,e" if search_mode == "r18" else "rating:g,s"
        fixed_metatags = [rating, "-status:deleted"]
        if random_count > 0:
            fixed_metatags.append(f"random:{random_count}")
        return " ".join([*verified_tags, *fixed_metatags]).strip()

    def _normalize_image_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"{DANBOORU_BASE_URL}{url}"
        return url

    def _get_image_url(self, post: dict[str, Any]) -> str:
        for key in ("large_file_url", "file_url", "preview_file_url"):
            image_url = self._normalize_image_url(str(post.get(key) or ""))
            if image_url:
                return image_url
        return ""

    def _is_valid_post(
        self,
        post: Any,
        *,
        min_score: int,
        search_mode: str,
    ) -> bool:
        if not isinstance(post, dict):
            return False
        if post.get("is_deleted") or post.get("is_banned"):
            return False
        if self._config_bool("exclude_animated", True) and post.get("is_animated"):
            return False

        file_ext = str(post.get("file_ext") or "").lower()
        if file_ext not in ALLOWED_EXTENSIONS:
            return False
        if not self._get_image_url(post):
            return False

        allowed_ratings = {"q", "e"} if search_mode == "r18" else {"g", "s"}
        if str(post.get("rating") or "").lower() not in allowed_ratings:
            return False
        if self._post_score(post) < min_score:
            return False

        post_tags = set(str(post.get("tag_string") or "").split())
        required, excluded = self._get_local_tag_filters(search_mode)
        if required and not required.issubset(post_tags):
            return False
        if excluded.intersection(post_tags):
            return False

        max_file_size_mb = max(1, self._config_int("max_file_size_mb", 20))
        file_size = post.get("file_size")
        if isinstance(file_size, int) and file_size > max_file_size_mb * 1024 * 1024:
            return False

        return True

    @staticmethod
    def _post_score(post: dict[str, Any]) -> int:
        try:
            return int(post.get("score") or 0)
        except (TypeError, ValueError):
            return 0

    def _remember_post(self, query_key: str, post_id: Any) -> None:
        try:
            normalized_id = int(post_id)
        except (TypeError, ValueError):
            return
        history_size = min(1000, max(0, self._config_int("recent_history_size", 200)))
        if history_size == 0:
            return
        history = self.recent_post_ids.get(query_key)
        if history is None or history.maxlen != history_size:
            history = deque(history or (), maxlen=history_size)
            self.recent_post_ids[query_key] = history
        if normalized_id in history:
            history.remove(normalized_id)
        history.append(normalized_id)

    async def _fetch_post(
        self,
        verified_tags: list[str],
        *,
        min_score: int,
        search_mode: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """用单次 posts API 请求尽量扩大随机范围，再在本地过滤与去重。

        尚有搜索项额度时使用 Danbooru 的 random:N。普通 tag 已占满额度时，
        使用 page=b<ID> 从该查询的历史 ID 范围随机切片；首次请求先建立上界。
        """
        result_pool_size = min(200, max(1, self._config_int("result_pool_size", 100)))
        max_terms = min(10, max(0, self._config_int("max_api_query_terms", 2)))
        use_server_random = len(verified_tags) < max_terms
        random_count = result_pool_size if use_server_random else 0
        query_tags = self._build_query_tags(
            verified_tags,
            search_mode=search_mode,
            random_count=random_count,
        )
        base_query = self._build_query_tags(verified_tags, search_mode=search_mode)
        query_key = f"{base_query}|score>={min_score}|mode={search_mode}"

        page: str | None = None
        ceiling = self.query_id_ceilings.get(base_query)
        if not use_server_random and ceiling and ceiling > 1:
            # 避开极早期 5% 的 ID 区间，降低冷门/新 tag 随机到空页的概率。
            page = f"b{random.randint(max(1, ceiling // 20), ceiling)}"

        logger.info(
            "[Danbooru] 请求 posts API: verified_tags=%r, query=%r, limit=%s, page=%r, auth=%s",
            verified_tags,
            query_tags,
            result_pool_size,
            page,
            "yes" if self._get_api_auth() else "no",
        )

        try:
            async with httpx.AsyncClient(
                timeout=self._api_timeout(),
                follow_redirects=True,
                auth=self._get_api_auth(),
                headers=self._get_api_headers(),
            ) as client:
                params: dict[str, Any] = {"tags": query_tags, "limit": result_pool_size}
                if page:
                    params["page"] = page
                response = await client.get(DANBOORU_POSTS_API, params=params)
                response.raise_for_status()
                posts = response.json()
                if page and isinstance(posts, list) and not posts:
                    # 随机边界早于冷门/新 tag 的首个帖子时，仅回退一次最新页。
                    logger.info("[Danbooru] 随机 ID 页为空，回退到该查询最新页")
                    page = None
                    response = await client.get(
                        DANBOORU_POSTS_API,
                        params={"tags": query_tags, "limit": result_pool_size},
                    )
                    response.raise_for_status()
                    posts = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "[Danbooru] posts API HTTP 错误: status=%s, body=%r",
                exc.response.status_code,
                exc.response.text[:300],
            )
            return None, query_tags
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[Danbooru] posts API 请求/解析失败: %s", exc)
            return None, query_tags
        except Exception:
            logger.exception("[Danbooru] 请求 posts API 时发生未知异常")
            return None, query_tags

        if not isinstance(posts, list):
            logger.warning("[Danbooru] posts API 返回格式异常: %s", type(posts).__name__)
            return None, query_tags

        if not use_server_random and page is None:
            ids = [
                int(post["id"])
                for post in posts
                if isinstance(post, dict) and str(post.get("id", "")).isdigit()
            ]
            if ids:
                self.query_id_ceilings[base_query] = max(ids)

        valid_posts = [
            post
            for post in posts
            if self._is_valid_post(post, min_score=min_score, search_mode=search_mode)
        ]
        if not valid_posts:
            logger.info("[Danbooru] API 返回 %s 条，本地过滤后为 0 条", len(posts))
            return None, query_tags

        recent_ids = set(self.recent_post_ids.get(query_key, ()))
        fresh_posts = []
        for post in valid_posts:
            try:
                post_id = int(post.get("id"))
            except (TypeError, ValueError):
                post_id = None
            if post_id is None or post_id not in recent_ids:
                fresh_posts.append(post)
        selected = random.choice(fresh_posts or valid_posts)
        self._remember_post(query_key, selected.get("id"))

        logger.info(
            "[Danbooru] API 返回 %s 条，本地有效 %s 条，新结果 %s 条；随机选择 post_id=%r",
            len(posts),
            len(valid_posts),
            len(fresh_posts),
            selected.get("id"),
        )
        return selected, query_tags

    # ---------------------------------------------------------------------
    # 图片下载与消息发送
    # ---------------------------------------------------------------------

    async def _download_image(
        self,
        image_url: str,
        expected_ext: str = "jpg",
        post_id: int | str | None = None,
    ) -> str | None:
        """下载 CDN 图片。不要向 cdn.donmai.us 发送 Danbooru API Key。"""
        expected_ext = expected_ext.lower().lstrip(".")
        if expected_ext not in ALLOWED_EXTENSIONS:
            expected_ext = "jpg"

        file_path = self.image_dir / f"{uuid4().hex}.{expected_ext}"
        image_host = urlparse(image_url).netloc
        post_referrer = (
            f"{DANBOORU_BASE_URL}/posts/{post_id}"
            if post_id is not None
            else f"{DANBOORU_BASE_URL}/"
        )

        try:
            timeout = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0)
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": self._config_str(
                        "danbooru_user_agent",
                        DANBOORU_DEFAULT_USER_AGENT,
                    ),
                    "Referer": post_referrer,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            ) as client:
                response = await client.get(image_url)
                response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            content = response.content
            if not content_type.startswith("image/"):
                logger.warning("[Danbooru] 下载结果不是图片: type=%r, url=%s", content_type, image_url)
                return None
            if len(content) < 1024:
                logger.warning("[Danbooru] 图片过小，疑似错误页: size=%s, url=%s", len(content), image_url)
                return None

            max_file_size_mb = max(1, self._config_int("max_file_size_mb", 20))
            if len(content) > max_file_size_mb * 1024 * 1024:
                logger.warning("[Danbooru] 图片过大: size=%s, limit=%sMB", len(content), max_file_size_mb)
                return None

            file_path.write_bytes(content)
            absolute_path = str(file_path.resolve())
            logger.info("[Danbooru] 图片下载完成: path=%s, size=%s, type=%s", absolute_path, len(content), content_type)
            return absolute_path

        except httpx.HTTPStatusError as exc:
            response = exc.response
            logger.warning(
                "[Danbooru] 图片 HTTP 错误: status=%s, host=%r, cf_mitigated=%r, server=%r, body=%r, url=%s",
                response.status_code,
                image_host,
                response.headers.get("cf-mitigated"),
                response.headers.get("server"),
                response.text[:180],
                image_url,
            )
        except httpx.HTTPError as exc:
            logger.warning("[Danbooru] 图片下载失败: %s; url=%s", exc, image_url)
        except Exception:
            logger.exception("[Danbooru] 图片下载发生未知异常: %s", image_url)
        finally:
            if file_path.exists() and file_path.stat().st_size == 0:
                try:
                    file_path.unlink()
                except OSError:
                    pass

        return None

    def _cleanup_old_cache(self) -> None:
        try:
            cache_hours = max(1, self._config_int("cache_hours", 24))
            deadline = time.time() - cache_hours * 3600
            for file_path in self.image_dir.iterdir():
                if not file_path.is_file():
                    continue
                try:
                    if file_path.stat().st_mtime < deadline:
                        file_path.unlink()
                except OSError:
                    pass
        except Exception:
            logger.exception("[Danbooru] 清理图片缓存失败")

    @staticmethod
    def _format_artist(post: dict[str, Any]) -> str:
        artist_tags = str(post.get("tag_string_artist") or "").strip()
        return artist_tags.replace("_", " ") if artist_tags else "未知"

    def _format_post_text(self, post: dict[str, Any], query_tags: str, resolved_tags: list[str]) -> str:
        post_id = post.get("id", "未知")
        lines = [
            f"Danbooru #{post_id}",
            f"作者：{self._format_artist(post)}",
            f"评分：{post.get('score', '未知')}",
            f"页面：{DANBOORU_BASE_URL}/posts/{post_id}",
        ]
        if self._config_bool("show_query_tags", False):
            lines.append("验证标签：" + (" ".join(resolved_tags) or "（无）"))
            lines.append("实际查询：" + query_tags)
        return "\n".join(lines)

    async def _send_post(
        self,
        event: AstrMessageEvent,
        post: dict[str, Any],
        query_tags: str,
        resolved_tags: list[str],
    ) -> bool:
        image_url = self._get_image_url(post)
        if not image_url:
            await event.send(event.plain_result("这条记录没有可用图片地址。"))
            return False

        image_path = await self._download_image(
            image_url=image_url,
            expected_ext=str(post.get("file_ext") or "jpg"),
            post_id=post.get("id"),
        )
        if image_path is None:
            await event.send(event.plain_result("图片下载失败了。这张图大概在网络里选择了远方。"))
            return False

        try:
            result = event.chain_result([
                Comp.Plain(self._format_post_text(post, query_tags, resolved_tags)),
                Comp.Image.fromFileSystem(image_path),
            ])
            await event.send(result)
            logger.info("[Danbooru] 已请求 OneBot 上传图片: post_id=%r", post.get("id"))
            self._cleanup_old_cache()
            return True
        except Exception:
            logger.exception("[Danbooru] 发送图片失败: post_id=%r", post.get("id"))
            try:
                await event.send(event.plain_result("图片已经下载好了，但发送到 QQ 时出了问题。"))
            except Exception:
                logger.exception("[Danbooru] 连错误提示也未能发送")
            return False

    # ---------------------------------------------------------------------
    # 命令和 LLM 工具入口
    # ---------------------------------------------------------------------

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return "unknown"

    def _check_cooldown(self, event: AstrMessageEvent) -> int:
        cooldown = max(0, self._config_int("user_cooldown_seconds", 20))
        if cooldown == 0:
            return 0
        last_time = self.user_last_request.get(self._get_sender_id(event), 0.0)
        remaining = cooldown - (time.monotonic() - last_time)
        return max(1, int(remaining)) if remaining > 0 else 0

    def _mark_request(self, event: AstrMessageEvent) -> None:
        self.user_last_request[self._get_sender_id(event)] = time.monotonic()

    async def _handle_request(
        self,
        event: AstrMessageEvent,
        user_input: str,
        *,
        search_mode: str = "safe",
    ) -> None:
        remaining = self._check_cooldown(event)
        if remaining > 0:
            await event.send(event.plain_result(f"请稍等 {remaining} 秒再请求。图片不是井水，不能一直打。"))
            return

        tag_input, min_score = self._parse_score_suffix(user_input)
        if ":" in tag_input:
            await event.send(
                event.plain_result(
                    "只支持在整条命令末尾使用英文冒号加最低分，例如："
                    "/danbooru hatsune_miku solo:150"
                )
            )
            return

        sender_id = self._get_sender_id(event)
        max_terms = min(10, max(0, self._config_int("max_api_query_terms", 2)))
        reserved_terms = 1 if search_mode == "comic" else 0
        if reserved_terms > max_terms:
            await event.send(
                event.plain_result("漫画搜索需要至少 1 个普通 tag 额度，请把 max_api_query_terms 调到 1 或更高。")
            )
            return
        resolution = await self._resolve_user_tags(
            tag_input,
            sender_id=sender_id,
            max_terms_override=max_terms - reserved_terms,
        )

        # 歧义优先：不静默搜图，先引导用户选精确 tag
        if resolution.ambiguous_terms:
            await event.send(event.plain_result(self._format_ambiguity_guidance(resolution)))
            return

        if tag_input and not resolution.resolved_tags:
            await event.send(event.plain_result(self._format_resolution_error(resolution)))
            return

        api_tags = list(resolution.resolved_tags)
        if search_mode == "comic" and "comic" not in api_tags:
            api_tags.append("comic")

        post, query_tags = await self._fetch_post(
            api_tags,
            min_score=min_score,
            search_mode=search_mode,
        )
        if post is None:
            message = "没有找到符合条件的图片。可能是标签较冷门，或本地过滤条件筛得太干净。"
            if min_score > 0:
                message += f"\n本次最低评分为 {min_score}；分数仅在本地筛选，可降低或去掉末尾分数。"
            if resolution.ignored_tags:
                message += "\n本次因查询上限未使用：" + "、".join(resolution.ignored_tags)
            await event.send(event.plain_result(message))
            return

        self._mark_request(event)
        await self._send_post(event, post, query_tags, api_tags)


    def _help_text(self, for_admin: bool = False) -> str:
        lines = [
            "【Danbooru 搜图插件】命令说明",
            "",
            "普通用户：",
            "  /danbooru [tag…][:分数]  普通搜图（General + Sensitive）",
            "                       例：/danbooru hatsune_miku",
            "                       例：/danbooru hatsune_miku solo:150",
            "                       例：/danbooru 初音未来:100",
            "  /danbooru_r18 [tag…][:分数]  R18 搜图（需管理员在配置中启用）",
            "                       例：/danbooru_r18 hatsune_miku:100",
            "  /danbooru_comic [tag…][:分数]  单独搜索漫画",
            "                       例：/danbooru_comic touhou:50",
            "  /danbooru_tags 关键词  查看候选 tag（英文前缀或中文）",
            "                       例：/danbooru_tags hatsune_mi",
            "                       例：/danbooru_tags 芙莉莲",
            "  /danbooru_help       显示本帮助",
            "",
            "说明：",
            "  · 不写分数时最低分为 0；末尾 :数字只在本地筛选，不发送给 API",
            "  · 多个 tag 用空格分隔，含义是同时满足（AND），不要用逗号",
            "  · 未登录/Member 最多 2 个普通 tag，Gold 最多 6 个，Platinum+ 的 API 不限；插件自身最多放行 10 个",
            "  · 不填用户名/API Key 时按未登录权限请求；使用账号权限时两项都要配置，并让 max_api_query_terms 匹配等级",
            "  · rating/status/score 等免费 metatag 不占上述额度；负 tag 会占额度，但本插件不接受用户自定义 metatag/负 tag",
            "  · posts API 单页最多 200 条；插件默认取 100 条，并保留用户冷却以避免过密请求",
            "  · 漫画命令固定占用 comic 这 1 个 tag；默认额度为 2 时还能再写 1 个 tag",
            "  · R18 命令仅查询 Questionable + Explicit；普通命令不会返回这两类",
            "  · 搜图通常只调用一次 posts API；随机池和近期 ID 去重会减少连续重复",
            "  · 中文通过对照服务 + 本地/配置词典映射，不靠 LLM 猜 tag",
            "  · 同一中文对应多个热门角色时，会列出候选让你复制精确 tag",
            "  · 英文拼写仅自动修正下划线/连字符差异与官方 alias",
        ]
        if for_admin:
            lines.extend(
                [
                    "",
                    "管理员（别名维护）：",
                    "  /danbooru_alias 中文 英文tag [英文tag…]",
                    "                       写入本地词典 data/danbooru/manual_aliases.json",
                    "                       例：/danbooru_alias 爱丽丝 alice_margatroid",
                    "  /danbooru_alias_del 中文",
                    "                       删除本地词典中的键",
                    "  /danbooru_alias_list [关键词]",
                    "                       列出本地别名（可过滤）",
                    "  /danbooru_suggest_log [条数]",
                    "                       查看最近中文对照/歧义建议日志（默认 10）",
                    "",
                    "权限：配置 alias_admin_ids，或平台管理员身份。",
                    f"本地词典路径：{LOCAL_ALIASES_FILE}",
                    f"建议日志路径：{SUGGEST_LOG_FILE}",
                ]
            )
        else:
            lines.append("")
            lines.append("管理员可用 /danbooru_help admin 查看维护命令。")
        return "\n".join(lines)

    @filter.command("danbooru_help")
    async def danbooru_help_command(self, event: AstrMessageEvent):
        """显示命令帮助。加参数 admin 可看维护命令。"""
        arg = self._extract_keyword(event.message_str, "danbooru_help").strip().lower()
        want_admin = arg in {"admin", "管理员", "op"}
        is_admin = self._is_alias_admin(event)
        if want_admin and not is_admin:
            await event.send(
                event.plain_result("无管理员权限，仅显示普通命令。\n\n" + self._help_text(False))
            )
            return
        await event.send(event.plain_result(self._help_text(for_admin=is_admin or want_admin)))

    @filter.command("danbooru")
    async def danbooru_command(self, event: AstrMessageEvent):
        """按已验证的 Danbooru tag 搜图。

        用法：
        /danbooru hatsune_miku
        /danbooru hatsune_miku white_thighhighs
        /danbooru white_thigh_highs   # 自动纠正为 white_thighhighs（可证明时）
        /danbooru 初音未来            # 中文对照（启用时）
        /danbooru 爱丽丝              # 多个候选时会列出引导，不静默选择
        /danbooru                     # 随机普通评级图片（最低分默认 0）

        中文通过对照服务 + 手工词典映射；不会让 LLM 直接发明 tag。
        """
        await self._handle_request(
            event,
            self._extract_keyword(event.message_str, "danbooru"),
        )

    @filter.command("danbooru_r18")
    async def danbooru_r18_command(self, event: AstrMessageEvent):
        """按已验证 tag 搜索 Questionable + Explicit 图片。"""
        if not self._config_bool("enable_r18_search", False):
            await event.send(
                event.plain_result("R18 搜寻未启用。管理员可在插件配置中开启 enable_r18_search。")
            )
            return
        await self._handle_request(
            event,
            self._extract_keyword(event.message_str, "danbooru_r18"),
            search_mode="r18",
        )

    @filter.command("danbooru_comic")
    async def danbooru_comic_command(self, event: AstrMessageEvent):
        """单独搜索带 comic tag 的普通评级漫画。"""
        await self._handle_request(
            event,
            self._extract_keyword(event.message_str, "danbooru_comic"),
            search_mode="comic",
        )

    @filter.command("danbooru_tags")
    async def danbooru_tags_command(self, event: AstrMessageEvent):
        """显示 tag 候选（支持英文前缀或中文关键词）。

        用法：
        /danbooru_tags hatsune_mi
        /danbooru_tags white_thigh
        /danbooru_tags 初音未来
        /danbooru_tags 爱丽丝
        """
        keyword = self._extract_keyword(event.message_str, "danbooru_tags").strip()
        if not keyword:
            await event.send(
                event.plain_result(
                    "请输入关键词，例如：\n"
                    "/danbooru_tags hatsune_mi\n"
                    "/danbooru_tags 初音未来\n"
                    "/danbooru_tags 爱丽丝"
                )
            )
            return

        # 中文关键词 → 对照服务
        if CJK_RE.search(keyword) and self._config_bool("enable_chinese_lookup", True):
            candidates = await self._fetch_chinese_candidates(keyword)
            self._append_suggestion_log(
                action="tags_query",
                input_term=keyword,
                candidates=candidates,
                sender_id=self._get_sender_id(event),
            )
            if not candidates:
                await event.send(
                    event.plain_result(
                        f"中文对照未找到与「{keyword}」相关的官方 tag。\n"
                        "可尝试更常见的译名，或改用英文前缀查询。"
                    )
                )
                return

            lines = [f"中文对照候选：「{keyword}」"]
            for item in candidates[:12]:
                lines.append(f"- {self._format_candidate_line(item)}")
            lines.append("\n复制规范英文 tag 后使用 /danbooru 即可搜图。")
            if self._is_alias_admin(event):
                lines.append("管理员：/danbooru_alias 中文 英文tag 可写入本地词典。")
            await event.send(event.plain_result("\n".join(lines)))
            return

        # 英文前缀 → 原有 tags API
        safe_keyword = self._safe_tag_token(keyword.replace(" ", "_"))
        if not safe_keyword:
            await event.send(
                event.plain_result(
                    "请输入英文 tag 前缀或中文关键词，例如：\n"
                    "/danbooru_tags hatsune_mi\n"
                    "/danbooru_tags 芙莉莲"
                )
            )
            return

        try:
            async with httpx.AsyncClient(
                timeout=self._api_timeout(),
                follow_redirects=True,
                auth=self._get_api_auth(),
                headers=self._get_api_headers(),
            ) as client:
                candidates = await self._fetch_tag_candidates(client, f"{safe_keyword}*")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[Danbooru] 候选 tag 查询失败: %s", exc)
            await event.send(event.plain_result("Danbooru 标签候选接口暂时不可用。"))
            return

        if not candidates:
            await event.send(event.plain_result(f"没有找到以 {safe_keyword} 开头的非空 tag。"))
            return

        lines = [f"Danbooru tag 候选：{safe_keyword}"]
        for item in candidates[:10]:
            name = str(item.get("name") or "")
            count = int(item.get("post_count") or 0)
            category = TAG_CATEGORY_NAMES.get(item.get("category"), "other")
            lines.append(f"- {name}  [{category}, {count} posts]")
        await event.send(event.plain_result("\n".join(lines)))

    
    @filter.command("danbooru_alias")
    async def danbooru_alias_command(self, event: AstrMessageEvent):
        """管理员：添加本地中文→官方 tag 映射。"""
        if not self._is_alias_admin(event):
            await event.send(event.plain_result("无权限。需要管理员或配置中的 alias_admin_ids。"))
            return
        args = self._extract_keyword(event.message_str, "danbooru_alias").strip()
        parts = args.split()
        if len(parts) < 2:
            await event.send(
                event.plain_result(
                    "用法：/danbooru_alias 中文或别名 英文tag [英文tag…]\n"
                    "例：/danbooru_alias 爱丽丝 alice_margatroid\n"
                    "例：/danbooru_alias 白丝 white_thighhighs"
                )
            )
            return
        key = parts[0].strip()
        tags: list[str] = []
        for p in parts[1:]:
            safe = self._safe_tag_token(p)
            if not safe:
                await event.send(event.plain_result(f"非法 tag，已拒绝：{p}"))
                return
            tags.append(safe)
        value = " ".join(tags)
        self._local_aliases[key.casefold()] = value
        if not self._save_local_aliases():
            await event.send(event.plain_result("写入本地文件失败，请检查 data/danbooru 目录权限。"))
            return
        self.tag_lookup_cache.pop(key.casefold(), None)
        self.chinese_lookup_cache.pop(key.casefold(), None)
        await event.send(
            event.plain_result(
                f"已写入本地别名：{key} → {value}\n文件：{LOCAL_ALIASES_FILE}"
            )
        )

    @filter.command("danbooru_alias_del")
    async def danbooru_alias_del_command(self, event: AstrMessageEvent):
        """管理员：删除本地别名。"""
        if not self._is_alias_admin(event):
            await event.send(event.plain_result("无权限。需要管理员或配置中的 alias_admin_ids。"))
            return
        key = self._extract_keyword(event.message_str, "danbooru_alias_del").strip()
        if not key:
            await event.send(event.plain_result("用法：/danbooru_alias_del 中文或别名"))
            return
        ck = key.casefold()
        if ck not in self._local_aliases:
            await event.send(event.plain_result(f"本地词典中没有「{key}」。"))
            return
        removed = self._local_aliases.pop(ck)
        self._save_local_aliases()
        self.tag_lookup_cache.pop(ck, None)
        await event.send(event.plain_result(f"已删除本地别名：{key}（原为 {removed}）"))

    @filter.command("danbooru_alias_list")
    async def danbooru_alias_list_command(self, event: AstrMessageEvent):
        """管理员：列出本地别名。"""
        if not self._is_alias_admin(event):
            await event.send(event.plain_result("无权限。需要管理员或配置中的 alias_admin_ids。"))
            return
        filt = self._extract_keyword(event.message_str, "danbooru_alias_list").strip().casefold()
        items = sorted(self._local_aliases.items(), key=lambda x: x[0])
        if filt:
            items = [(k, v) for k, v in items if filt in k or filt in v]
        if not items:
            await event.send(event.plain_result("本地别名为空，或没有匹配项。"))
            return
        lines = [f"本地别名（共 {len(items)} 条，显示前 40）："]
        for k, v in items[:40]:
            lines.append(f"  {k} → {v}")
        if len(items) > 40:
            lines.append(f"  … 其余 {len(items) - 40} 条省略")
        lines.append(f"文件：{LOCAL_ALIASES_FILE}")
        await event.send(event.plain_result("\n".join(lines)))

    @filter.command("danbooru_suggest_log")
    async def danbooru_suggest_log_command(self, event: AstrMessageEvent):
        """管理员：查看最近的对照/歧义建议日志。"""
        if not self._is_alias_admin(event):
            await event.send(event.plain_result("无权限。需要管理员或配置中的 alias_admin_ids。"))
            return
        arg = self._extract_keyword(event.message_str, "danbooru_suggest_log").strip()
        try:
            n = max(1, min(50, int(arg))) if arg else 10
        except ValueError:
            n = 10
        if not SUGGEST_LOG_FILE.exists():
            await event.send(event.plain_result(f"暂无建议日志。路径：{SUGGEST_LOG_FILE}"))
            return
        try:
            lines_file = SUGGEST_LOG_FILE.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            await event.send(event.plain_result(f"读取日志失败：{exc}"))
            return
        recent = lines_file[-n:]
        if not recent:
            await event.send(event.plain_result("日志为空。"))
            return
        out: list[str] = [f"最近 {len(recent)} 条建议日志："]
        for line in recent:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out.append(line[:120])
                continue
            action = obj.get("action", "?")
            inp = obj.get("input", "")
            used = obj.get("used")
            cands = obj.get("candidates") or []
            top = ", ".join(
                f"{c.get('name')}({c.get('post_count', 0)})" for c in cands[:3]
            )
            ts = obj.get("ts", "")
            piece = f"[{ts}] {action} 「{inp}」"
            if used:
                piece += f" → {used}"
            if top:
                piece += f" | 候选: {top}"
            out.append(piece)
        out.append(f"\n完整文件：{SUGGEST_LOG_FILE}")
        out.append("确认后可用 /danbooru_alias 中文 英文tag 写入词典。")
        await event.send(event.plain_result("\n".join(out)))


    @filter.llm_tool(name="search_danbooru")
    async def search_danbooru(self, event: AstrMessageEvent, tags: str = ""):
        """按真实 Danbooru 标签检索一张普通评级的二次元图片。

        仅在用户明确要求使用 Danbooru 搜图时调用。
        可传入 1～2 个英文官方 tag，或中文角色/作品名（插件会做对照查找）。
        插件会验证 tag、处理下划线拼写与官方别名；中文歧义时会列出候选让用户确认。
        可在整个字符串末尾追加 :数字作为本地最低评分，例如 hatsune_miku:150。
        不要传 rating、score、order、status、负面标签或解释性长句。

        Args:
            tags(string): 1～2 个 tag（英文官方名或中文常用名），空格分隔；
                可在末尾加 :最低分。例："hatsune_miku white_thighhighs:100"。
        """
        try:
            await self._handle_request(event, (tags or "").strip())
        except Exception:
            logger.exception("[search_danbooru] 工具执行异常: tags=%r", tags)
            try:
                await event.send(
                    event.plain_result(
                        "Danbooru 查询时出了点问题。标签们大概正在进行一场不必要的文学讨论。"
                    )
                )
            except Exception:
                logger.exception("[search_danbooru] 异常提示发送失败")
