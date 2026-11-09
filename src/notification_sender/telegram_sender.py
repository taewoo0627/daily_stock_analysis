# -*- coding: utf-8 -*-
"""
Telegram 发送提醒服务

职责：
1. 通过 Telegram Bot API 发送 文本消息
2. 通过 Telegram Bot API 发送 图片消息
"""
import logging
from typing import Optional
import requests
import time
import re

from src.config import Config
from src.formatters import _custom_unit_to_index, strip_hidden_markdown_metadata, utf16_len


logger = logging.getLogger(__name__)


class TelegramSender:

    def __init__(self, config: Config):
        """
        初始化 Telegram 配置

        Args:
            config: 配置对象
        """
        self._telegram_config = {
            'bot_token': getattr(config, 'telegram_bot_token', None),
            'chat_id': getattr(config, 'telegram_chat_id', None),
            'message_thread_id': getattr(config, 'telegram_message_thread_id', None),
        }

    def _is_telegram_configured(self) -> bool:
        """检查 Telegram 配置是否完整"""
        return bool(self._telegram_config['bot_token'] and self._telegram_config['chat_id'])

    def send_to_telegram(
        self,
        content: str,
        *,
        chat_id: Optional[str] = None,
        message_thread_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到 Telegram 机器人

        Telegram Bot API 格式：
        POST https://api.telegram.org/bot<token>/sendMessage
        {
            "chat_id": "xxx",
            "text": "消息内容",
            "parse_mode": "Markdown"
        }

        Args:
            content: 消息内容（Markdown 格式）

        Returns:
            是否发送成功
        """
        target_chat_id = chat_id if chat_id is not None else self._telegram_config.get("chat_id")
        target_message_thread_id = (
            message_thread_id
            if message_thread_id is not None
            else self._telegram_config.get("message_thread_id")
        )

        if not (self._telegram_config["bot_token"] and target_chat_id):
            logger.warning("Telegram 配置不完整，跳过推送")
            return False

        bot_token = self._telegram_config['bot_token']
        chat_id = target_chat_id
        message_thread_id = target_message_thread_id

        try:
            # Telegram API 端点
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            # Telegram 消息最大长度 4096（按 UTF-16 code unit 计）
            max_length = 4096

            sanitized_content = strip_hidden_markdown_metadata(content).strip()
            if not sanitized_content:
                logger.warning("Telegram 消息内容为空，跳过推送")
                return False

            telegram_content = self._convert_to_telegram_markdown(sanitized_content)

            if utf16_len(telegram_content) <= max_length:
                # 单条消息发送
                return self._send_telegram_message(
                    api_url,
                    chat_id,
                    sanitized_content,
                    message_thread_id,
                    timeout_seconds=timeout_seconds,
                )
            else:
                # 按 Markdown 转义后的最终 payload 分段，避免转义字符使请求超限
                return self._send_telegram_chunked(
                    api_url,
                    chat_id,
                    telegram_content,
                    max_length,
                    message_thread_id,
                    timeout_seconds=timeout_seconds,
                )

        except Exception as e:
            logger.error(f"发送 Telegram 消息失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False

    def _send_telegram_message(
        self,
        api_url: str,
        chat_id: str,
        text: str,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
        markdown_converted: bool = False,
    ) -> bool:
        """Send a single Telegram message with exponential backoff retry (Fixes #287)"""
        # Convert Markdown to Telegram-compatible format
        telegram_text = text if markdown_converted else self._convert_to_telegram_markdown(text)

        payload = {
            "chat_id": chat_id,
            "text": telegram_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        if message_thread_id:
            payload['message_thread_id'] = message_thread_id

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(api_url, json=payload, timeout=timeout_seconds or 10)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries:
                    delay = 2 ** attempt  # 2s, 4s
                    logger.warning(f"Telegram request failed (attempt {attempt}/{max_retries}): {e}, "
                                   f"retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"Telegram request failed after {max_retries} attempts: {e}")
                    return False

            if response.status_code == 200:
                result = response.json()
                if result.get('ok'):
                    logger.info("Telegram 消息发送成功")
                    return True
                else:
                    error_desc = result.get('description', '未知错误')
                    logger.error(f"Telegram 返回错误: {error_desc}")

                    # If Markdown parsing failed, fall back to plain text
                    if self._should_fallback_to_plain_text(error_desc=error_desc):
                        if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                            return True

                    return False
            elif response.status_code == 429:
                # Rate limited — respect Retry-After header
                retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                if attempt < max_retries:
                    logger.warning(f"Telegram rate limited, retrying in {retry_after}s "
                                   f"(attempt {attempt}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Telegram rate limited after {max_retries} attempts")
                    return False
            else:
                if attempt < max_retries and response.status_code >= 500:
                    delay = 2 ** attempt
                    logger.warning(f"Telegram server error HTTP {response.status_code} "
                                   f"(attempt {attempt}/{max_retries}), retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                if self._should_fallback_to_plain_text(response_text=response.text):
                    if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                        return True
                logger.error(f"Telegram 请求失败: HTTP {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return False

        return False

    @staticmethod
    def _should_fallback_to_plain_text(error_desc: str = "", response_text: str = "") -> bool:
        """Detect Telegram Markdown parsing failures that should retry as plain text."""
        haystack = f"{error_desc}\n{response_text}".lower()
        markers = (
            "can't parse entities",
            "can't parse entity",
            "can't find end of the entity",
            "parse entities",
            "parse_mode",
            "markdown",
        )
        return any(marker in haystack for marker in markers)

    def _send_plain_text_fallback(
        self,
        api_url: str,
        payload: dict,
        text: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Retry Telegram send without parse_mode when Markdown parsing fails."""
        logger.info("Telegram Markdown 解析失败，尝试使用纯文本格式重新发送...")
        plain_payload = dict(payload)
        plain_payload.pop('parse_mode', None)
        plain_text = re.sub(r"<[^>]+>", "", text)
        plain_payload['text'] = plain_text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

        try:
            response = requests.post(api_url, json=plain_payload, timeout=timeout_seconds or 10)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.error(f"Telegram plain-text fallback failed: {e}")
            return False

        if response.status_code == 200:
            try:
                result = response.json()
            except ValueError:
                logger.error("Telegram 纯文本回退失败: 响应不是有效 JSON")
                logger.error(f"响应内容: {response.text}")
                return False

            if result.get('ok'):
                logger.info("Telegram 消息发送成功（纯文本）")
                return True

            logger.error("Telegram 纯文本回退失败: Telegram API 返回 ok=false")
            logger.error(f"响应内容: {response.text}")
            return False

        logger.error(f"Telegram 纯文本回退失败: HTTP {response.status_code}")
        logger.error(f"响应内容: {response.text}")
        return False

    def _send_telegram_chunked(
        self,
        api_url: str,
        chat_id: str,
        content: str,
        max_length: int,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """按已转换的 Telegram Markdown payload 分段发送长消息。"""
        # 按段落分割
        delimiter = f"\n{self._TG_SEPARATOR}\n"
        sections = content.split(delimiter)
        delimiter_length = len(delimiter)

        current_chunk = []
        current_length = 0
        all_success = True
        chunk_index = 1

        def _flush_chunk() -> bool:
            nonlocal current_chunk, current_length, chunk_index, all_success
            if not current_chunk:
                return all_success

            chunk_content = delimiter.join(current_chunk)
            logger.info(f"发送 Telegram 消息块 {chunk_index}...")
            chunk_index += 1
            current_chunk = []
            current_length = 0
            if not self._send_telegram_message(
                api_url,
                chat_id,
                chunk_content,
                message_thread_id,
                timeout_seconds=timeout_seconds,
                markdown_converted=True,
            ):
                all_success = False
            return all_success

        def _split_long_section(section: str, limit: int) -> list[str]:
            chunks: list[str] = []
            while utf16_len(section) > limit:
                cut = max(1, _custom_unit_to_index(section, limit, utf16_len))
                newline_cut = section.rfind("\n", 0, cut)
                if newline_cut > cut // 2:
                    cut = newline_cut + 1
                chunks.append(section[:cut])
                section = section[cut:]
            chunks.append(section)
            return chunks

        for section in sections:
            if utf16_len(section) > max_length:
                # 单段超限时强制切片，避免依赖“\\n---\\n”边界导致的整段超长发送
                if not _flush_chunk():
                    return False
                for long_chunk in _split_long_section(section, max_length):
                    logger.info(f"发送 Telegram 消息块 {chunk_index}...")
                    chunk_index += 1
                    if not self._send_telegram_message(
                        api_url,
                        chat_id,
                        long_chunk,
                        message_thread_id,
                        timeout_seconds=timeout_seconds,
                        markdown_converted=True,
                    ):
                        all_success = False
                continue

            additional_length = utf16_len(section)
            if current_chunk:
                additional_length += delimiter_length

            if current_length + additional_length > max_length:
                _flush_chunk()
                current_chunk = [section]
                current_length = utf16_len(section)
                continue

            current_chunk.append(section)
            current_length += additional_length

        # 发送最后一块
        if not _flush_chunk():
            return False

        return all_success

    def _send_telegram_photo(self, image_bytes: bytes) -> bool:
        """Send image via Telegram sendPhoto API (Issue #289)."""
        if not self._is_telegram_configured():
            return False
        bot_token = self._telegram_config['bot_token']
        chat_id = self._telegram_config['chat_id']
        message_thread_id = self._telegram_config.get('message_thread_id')
        api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        try:
            data = {"chat_id": chat_id}
            if message_thread_id:
                data['message_thread_id'] = message_thread_id
            files = {"photo": ("report.png", image_bytes, "image/png")}
            response = requests.post(api_url, data=data, files=files, timeout=30)
            if response.status_code == 200 and response.json().get('ok'):
                logger.info("Telegram 图片发送成功")
                return True
            logger.error("Telegram 图片发送失败: %s", response.text[:200])
            return False
        except Exception as e:
            logger.error("Telegram 图片发送异常: %s", e)
            return False

    _TG_SEPARATOR = "━━━━━━━━━━━━"
    _TG_LABEL_MAP = {
        "大盘复盘": "시장 복기",
        "评分": "점수",
    }

    @staticmethod
    def _tg_escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _tg_pretty_number(cell: str) -> str:
        """Human-friendly numbers: 1136000.00 -> 1,136,000 ; 1985.59 万股 -> 1,986만 주."""
        cell = (cell or "").strip()

        def _volume(match):
            value = float(match.group(1))
            unit = "만 주" if match.group(2) == "万股" else "억 주"
            return f"{value:,.0f}{unit}" if abs(value) >= 100 else f"{value:,.1f}{unit}"

        cell = re.sub(r"(-?\d+(?:\.\d+)?)\s*(万股|亿股)", _volume, cell)
        match = re.fullmatch(r"(-?)(\d{4,})(?:\.(\d+))?", cell)
        if match:
            sign, whole, frac = match.groups()
            frac = (frac or "").rstrip("0")
            return f"{sign}{int(whole):,}" + (f".{frac}" if frac else "")
        return cell

    def _tg_inline(self, text: str) -> str:
        """Convert inline Markdown (bold/italic/code/links) to Telegram HTML."""
        links = []

        def _save_link(match):
            links.append((match.group(1), match.group(2)))
            return f"\x00{len(links) - 1}\x00"

        text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", _save_link, text)
        text = re.sub(r"\\([\\\[\]()_*\x60|#+\-.!>~=])", r"\1", text)
        text = self._tg_escape(text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])", r"<i>\1</i>", text)
        text = re.sub(r"\x60([^\x60]+)\x60", r"<code>\1</code>", text)

        def _restore_link(match):
            label, url = links[int(match.group(1))]
            return f'<a href="{self._tg_escape(url)}">{self._tg_escape(label)}</a>'

        return re.sub(r"\x00(\d+)\x00", _restore_link, text)

    def _tg_plain(self, text: str) -> str:
        """Inline text without any formatting tags (safe to wrap in <b>)."""
        return re.sub(r"<[^>]+>", "", self._tg_inline(text))

    def _tg_table(self, rows: list) -> list:
        """Render a Markdown table as readable bullet lines."""

        def _cells(row: str) -> list:
            return [c.strip() for c in row.strip().strip("|").split("|")]

        def _is_empty(value: str) -> bool:
            return not value or value.upper() in {"N/A", "NA", "-", "—", "NONE", "NAN"}

        parsed = [_cells(r) for r in rows if not re.fullmatch(r"\|?[\s:\-|]+\|?", r.strip())]
        if not parsed:
            return []
        header, body = parsed[0], parsed[1:]
        if not body:
            return ["  ".join(self._tg_inline(c) for c in header if c)]

        out = []
        if len(header) == 2:
            for row in body:
                key = row[0] if row else ""
                value = self._tg_pretty_number(row[1]) if len(row) > 1 else ""
                if _is_empty(value):
                    continue
                if len(value) <= 40:
                    out.append(f"• {self._tg_inline(key)}: <b>{self._tg_plain(value)}</b>")
                else:
                    out.append(f"• <b>{self._tg_plain(key)}</b>: {self._tg_inline(value)}")
            return out

        for row in body:
            pairs = [
                (h, self._tg_pretty_number(v))
                for h, v in zip(header, row)
                if not _is_empty(self._tg_pretty_number(v))
            ]
            if not pairs:
                continue
            if len(body) == 1:
                for h, v in pairs:
                    out.append(f"• {self._tg_inline(h)}: <b>{self._tg_plain(v)}</b>")
            else:
                title_h, title_v = pairs[0]
                out.append(f"▪️ <b>{self._tg_plain(title_v)}</b>")
                rest = [f"{self._tg_inline(h)} {self._tg_inline(v)}" for h, v in pairs[1:]]
                if rest:
                    out.append("    " + " · ".join(rest))
        return out

    def _convert_to_telegram_markdown(self, text: str) -> str:
        """
        Convert report Markdown into Telegram HTML for readability:
        headings -> bold, tables -> bullet lines, quotes -> blockquote,
        numbers with thousands separators, no stray escape characters.
        """
        source = strip_hidden_markdown_metadata(text)
        for zh, ko in self._TG_LABEL_MAP.items():
            source = source.replace(zh, ko)
        lines = source.split("\n")
        out = []
        i = 0
        while i < len(lines):
            raw = lines[i].rstrip()
            stripped = raw.strip()
            if stripped.startswith("|") and stripped.count("|") >= 2:
                block = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    block.append(lines[i].strip())
                    i += 1
                out.extend(self._tg_table(block))
                continue
            i += 1
            if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
                out.append(self._TG_SEPARATOR)
                continue
            heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
            if heading:
                out.append("")
                out.append(f"<b>{self._tg_plain(heading.group(1).strip())}</b>")
                continue
            if stripped.startswith(">"):
                content = stripped.lstrip(">").strip()
                if content:
                    out.append(f"<blockquote>{self._tg_inline(content)}</blockquote>")
                continue
            bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
            if bullet:
                indent = "    " if len(bullet.group(1)) >= 2 else ""
                out.append(f"{indent}• {self._tg_inline(bullet.group(2))}")
                continue
            out.append(self._tg_inline(raw))
        result = "\n".join(out)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()
