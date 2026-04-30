from __future__ import annotations

import html
import io
import json
import re
import ssl
import tomllib
from typing import Any

import aiohttp
from loguru import logger
from PIL import Image

from utils.decorators import on_text_message
from utils.plugin_base import PluginBase

try:
    from WechatAPI import WechatAPIClient
except ImportError:  # 兼容部分框架版本的导出路径
    from WechatAPI.Client import WechatAPIClient


class VideoParserError(Exception):
    pass


class DouyinParser(PluginBase):
    description = "抖音解析插件"
    author = "BEelzebub"
    version = "1.1.0"

    USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
    )
    DOUYIN_URL_RE = re.compile(r'https?://[^\s<>"]+?(?:douyin\.com|iesdouyin\.com)[^\s<>"]*')
    ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*({.*?})\s*</script>", re.S)

    def __init__(self):
        super().__init__()
        self.load_config()

    def load_config(self):
        with open("plugins/DouyinParser/config.toml", "rb") as f:
            config = tomllib.load(f)

        config = config["DouyinParser"]
        self.enable = config["enable"]
        self.allowed_groups = config["allowed_groups"]

    @on_text_message(priority=10)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        content = str(message.get("Content", "")).strip()
        group_id = str(message.get("FromWxid", "")).strip()
        if not content or not group_id:
            return

        if "*" not in self.allowed_groups and group_id not in self.allowed_groups:
            return

        douyin_url = self._extract_douyin_url(content)
        if not douyin_url:
            return

        try:
            result = await self.parse_video(douyin_url)
            logger.debug(f"抖音解析结果: {result}")
            if result.get("kind") == "note":
                await self._send_note(bot, group_id, result)
            else:
                await self._send_video_card(bot, group_id, result)
        except VideoParserError as e:
            logger.error(f"解析抖音失败: {e}")
            await bot.send_text_message(group_id, f"解析失败: {e}")
        except Exception as e:
            logger.exception(f"处理抖音链接时发生错误: {e}")
            await bot.send_text_message(group_id, "解析失败，请稍后重试")

    @classmethod
    def _extract_douyin_url(cls, content: str) -> str | None:
        match = cls.DOUYIN_URL_RE.search(content)
        if not match:
            return None
        return match.group(0).rstrip("，。,.!！?？)")

    async def parse_video(self, video_url: str) -> dict[str, Any]:
        """解析抖音分享页，兼容视频和图文作品。"""
        try:
            async with self._create_session() as session:
                resolved_url = await self._resolve_redirect(session, video_url)
                html_content = await self._fetch_text(session, resolved_url)
                result = self._parse_page_html(html_content)
                if "source_url" not in result:
                    result["source_url"] = resolved_url
                return result
        except aiohttp.ClientError as e:
            raise VideoParserError(f"网络请求失败: {e}") from e
        except VideoParserError:
            raise
        except Exception as e:
            raise VideoParserError(f"解析过程发生错误: {e}") from e

    def _create_session(self) -> aiohttp.ClientSession:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        headers = {"User-Agent": self.USER_AGENT}
        return aiohttp.ClientSession(connector=connector, headers=headers)

    async def _resolve_redirect(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308):
                return response.headers.get("Location") or url
            return str(response.url)

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url) as response:
            if response.status != 200:
                raise VideoParserError(f"获取页面失败，状态码: {response.status}")
            html_content = await response.text()
            if not html_content:
                raise VideoParserError("页面内容为空")
            return html_content

    @classmethod
    def _parse_page_html(cls, html_content: str) -> dict[str, Any]:
        item = cls._extract_aweme_item(html_content)
        if item:
            note = cls._parse_note_item(item)
            if note:
                return note

            video = cls._parse_video_item(item)
            if video:
                return video

        legacy_video = cls._parse_legacy_video(html_content)
        if legacy_video:
            return legacy_video

        raise VideoParserError("未找到可解析的抖音图文或视频内容")

    @classmethod
    def _extract_aweme_item(cls, html_content: str) -> dict[str, Any] | None:
        match = cls.ROUTER_DATA_RE.search(html_content)
        if not match:
            return None

        try:
            router_data = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            logger.warning(f"解析 _ROUTER_DATA 失败: {e}")
            return None

        loader_data = router_data.get("loaderData")
        if not isinstance(loader_data, dict):
            return None

        for page_data in loader_data.values():
            if not isinstance(page_data, dict):
                continue
            video_info = page_data.get("videoInfoRes")
            if not isinstance(video_info, dict):
                continue
            item_list = video_info.get("item_list")
            if isinstance(item_list, list) and item_list and isinstance(item_list[0], dict):
                return item_list[0]
        return None

    @classmethod
    def _parse_note_item(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        image_url = cls._pick_image_url(item)
        if not image_url:
            return None

        return {
            "kind": "note",
            "title": cls._clean_text(item.get("desc")),
            "author": cls._clean_text((item.get("author") or {}).get("nickname")),
            "image_url": image_url,
        }

    @classmethod
    def _pick_image_url(cls, item: dict[str, Any]) -> str:
        candidates: list[str] = []
        for image_info in item.get("images") or item.get("image_infos") or []:
            if not isinstance(image_info, dict):
                continue
            for image_url in image_info.get("url_list") or []:
                if isinstance(image_url, str) and image_url.startswith("http"):
                    candidates.append(html.unescape(image_url))

        for image_url in candidates:
            if re.search(r"\.(?:jpe?g|png)(?:\?|$)", image_url, re.I):
                return image_url
        return candidates[0] if candidates else ""

    @classmethod
    def _parse_video_item(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        video = item.get("video")
        if not isinstance(video, dict) or video.get("duration") == 0:
            return None

        play_addr = video.get("play_addr") or {}
        urls = play_addr.get("url_list") or []
        video_url = cls._pick_video_url(urls)
        if not video_url:
            return None

        cover = video.get("cover") or {}
        cover_urls = cover.get("url_list") or []
        cover_url = cover_urls[0] if cover_urls else ""

        return {
            "kind": "video",
            "url": video_url,
            "title": cls._clean_text(item.get("desc")),
            "author": cls._clean_text((item.get("author") or {}).get("nickname")),
            "cover": html.unescape(cover_url),
        }

    @classmethod
    def _parse_legacy_video(cls, html_content: str) -> dict[str, Any] | None:
        pattern = re.compile(r'"play_addr":\s*{\s*"uri":\s*"[^"]*",\s*"url_list":\s*\[([^\]]*)\]')
        match = pattern.search(html_content)
        if not match:
            return None

        urls = [url.strip().strip('"') for url in match.group(1).split(",")]
        video_url = cls._pick_video_url(urls)
        if not video_url:
            return None

        title = cls._match_json_string(html_content, "desc")
        author = cls._match_json_string(html_content, "nickname")
        cover_match = re.search(r'"cover":\s*{\s*"url_list":\s*\[\s*"([^"]+)"', html_content)

        return {
            "kind": "video",
            "url": video_url,
            "title": title,
            "author": author,
            "cover": cls._decode_url(cover_match.group(1)) if cover_match else "",
        }

    @classmethod
    def _pick_video_url(cls, urls: list[Any]) -> str:
        decoded_urls = [
            cls._decode_url(url).replace("playwm", "play")
            for url in urls
            if isinstance(url, str) and url
        ]
        snssdk_urls = [url for url in decoded_urls if "aweme.snssdk.com" in url]
        return snssdk_urls[0] if snssdk_urls else (decoded_urls[0] if decoded_urls else "")

    @staticmethod
    def _match_json_string(text: str, key: str) -> str:
        match = re.search(rf'"{re.escape(key)}":\s*"([^"]*)"', text)
        return DouyinParser._clean_text(DouyinParser._decode_url(match.group(1))) if match else ""

    @staticmethod
    def _decode_url(value: str) -> str:
        return html.unescape(value.encode().decode("unicode_escape"))

    @staticmethod
    def _clean_text(value: Any) -> str:
        return "" if value is None else html.unescape(str(value)).strip()

    async def _send_note(self, bot: WechatAPIClient, group_id: str, note_info: dict[str, Any]):
        image_url = note_info.get("image_url", "")
        if not image_url:
            raise VideoParserError("图文作品未找到图片地址")

        async with self._create_session() as session:
            image_bytes = await self._download_image(session, image_url)

        await bot.send_image_message(group_id, image=image_bytes)

        caption = self._build_note_caption(note_info)
        if caption:
            await bot.send_text_message(group_id, caption)

    async def _download_image(self, session: aiohttp.ClientSession, image_url: str) -> bytes:
        async with session.get(image_url) as response:
            if response.status != 200:
                raise VideoParserError(f"下载图文图片失败，状态码: {response.status}")
            image_bytes = await response.read()

        if not image_bytes:
            raise VideoParserError("图文图片内容为空")

        return self._normalize_image_bytes(image_bytes)

    @staticmethod
    def _normalize_image_bytes(image_bytes: bytes) -> bytes:
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.format and image.format.upper() in {"JPEG", "PNG"}:
                    return image_bytes

                output = io.BytesIO()
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(output, format="JPEG", quality=92)
                return output.getvalue()
        except Exception as e:
            logger.warning(f"图文图片格式检查失败，直接发送原始字节: {e}")
            return image_bytes

    @staticmethod
    def _build_note_caption(note_info: dict[str, Any]) -> str:
        lines = []
        author = note_info.get("author", "")
        title = note_info.get("title", "")
        source_url = note_info.get("source_url", "")

        if author:
            lines.append(f"作者：{author}")
        if title:
            lines.append(f"文案：{title}")
        if source_url:
            lines.append(f"链接：{source_url}")
        return "\n".join(lines)

    async def _send_video_card(self, bot: WechatAPIClient, group_id: str, video_info: dict):
        try:
            title = video_info.get("title", "")
            author = video_info.get("author", "")
            display_title = f"{title[:30]} - {author[:10]}" if author else title[:40]
            if not display_title:
                display_title = "抖音视频"

            video_url = video_info.get("url", "")
            thumb_url = video_info.get(
                "cover",
                "https://is1-ssl.mzstatic.com/image/thumb/Purple221/v4/7c/49/e1/"
                "7c49e1af-ce92-d1c4-9a93-0a316e47ba94/"
                "AppIcon_TikTok-0-0-1x_U007epad-0-1-0-0-85-220.png/512x512bb.jpg",
            )
            description = "点击观看无水印视频"

            logger.info(f"准备发送抖音视频卡片: to={group_id}, title={display_title}, url={video_url}")
            await bot.send_link_message(
                wxid=group_id,
                url=video_url,
                title=display_title,
                description=description,
                thumb_url=thumb_url,
            )
        except Exception as e:
            logger.exception(f"发送抖音视频卡片失败: {e}")
            message = f"视频标题：{video_info.get('title', '未知')}\n视频链接：{video_info.get('url', '')}\n"
            await bot.send_text_message(group_id, message)
