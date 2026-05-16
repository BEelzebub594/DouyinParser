from __future__ import annotations

import base64
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
    DEFAULT_THUMB_URL = (
        "https://is1-ssl.mzstatic.com/image/thumb/Purple221/v4/7c/49/e1/"
        "7c49e1af-ce92-d1c4-9a93-0a316e47ba94/"
        "AppIcon_TikTok-0-0-1x_U007epad-0-1-0-0-85-220.png/512x512bb.jpg"
    )
    CARD_THUMB_SIZE = (440, 330)

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
        image_url_groups = cls._pick_image_url_groups(item)
        if not image_url_groups:
            return None

        return {
            "kind": "note",
            "title": cls._clean_text(item.get("desc")),
            "author": cls._clean_text((item.get("author") or {}).get("nickname")),
            "image_urls": [group[0] for group in image_url_groups],
            "image_url_groups": image_url_groups,
        }

    @classmethod
    def _pick_image_url_groups(cls, item: dict[str, Any]) -> list[list[str]]:
        image_url_groups: list[list[str]] = []
        seen_groups: set[tuple[str, ...]] = set()
        for image_info in item.get("images") or item.get("image_infos") or []:
            if not isinstance(image_info, dict):
                continue
            candidates: list[str] = []
            seen_urls: set[str] = set()
            for image_url in image_info.get("url_list") or []:
                if not isinstance(image_url, str) or not image_url.startswith("http"):
                    continue
                decoded_url = html.unescape(image_url)
                if decoded_url in seen_urls:
                    continue
                candidates.append(decoded_url)
                seen_urls.add(decoded_url)

            group_key = tuple(candidates)
            if candidates and group_key not in seen_groups:
                image_url_groups.append(candidates)
                seen_groups.add(group_key)
        return image_url_groups

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

        cover_url = cls._pick_video_cover_url(video)

        return {
            "kind": "video",
            "url": video_url,
            "title": cls._clean_text(item.get("desc")),
            "author": cls._clean_text((item.get("author") or {}).get("nickname")),
            "cover": html.unescape(cover_url),
        }

    @classmethod
    def _pick_video_cover_url(cls, video: dict[str, Any]) -> str:
        fallback_url = ""
        for key in ("cover", "origin_cover", "dynamic_cover", "animated_cover", "ai_dynamic_cover"):
            cover = video.get(key)
            if not isinstance(cover, dict):
                continue
            cover_url, first_url = cls._pick_preferred_cover_url(cover.get("url_list") or [])
            if cover_url:
                return cover_url
            fallback_url = fallback_url or first_url
            cover_url, first_url = cls._pick_preferred_cover_url([cover.get("uri"), cover.get("url")])
            if cover_url:
                return cover_url
            fallback_url = fallback_url or first_url
        return fallback_url

    @classmethod
    def _pick_first_http_url(cls, urls: list[Any]) -> str:
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            decoded_url = cls._decode_url(url)
            if decoded_url.startswith("http"):
                return decoded_url
        return ""

    @classmethod
    def _pick_preferred_cover_url(cls, urls: list[Any]) -> tuple[str, str]:
        first_url = cls._pick_first_http_url(urls)
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            decoded_url = cls._decode_url(url)
            if decoded_url.startswith("http") and cls._is_plain_image_url(decoded_url):
                return decoded_url, first_url
        return "", first_url

    @staticmethod
    def _is_plain_image_url(url: str) -> bool:
        clean_url = str(url or "").split("?", 1)[0].lower()
        return clean_url.endswith((".jpg", ".jpeg", ".png")) or "format=jpeg" in url.lower()

    @classmethod
    def _normalize_card_thumb_url(cls, thumb_url: str) -> str:
        thumb_url = str(thumb_url or "").strip()
        if not thumb_url or not cls._is_plain_image_url(thumb_url):
            return cls.DEFAULT_THUMB_URL
        return thumb_url

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
        image_url_groups = note_info.get("image_url_groups") or [
            [image_url] for image_url in note_info.get("image_urls") or []
        ]
        if not image_url_groups:
            raise VideoParserError("图文作品未找到图片地址")

        async with self._create_session() as session:
            image_bytes = await self._download_long_image(session, image_url_groups)

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

    async def _download_long_image(
        self,
        session: aiohttp.ClientSession,
        image_url_groups: list[list[str]],
    ) -> bytes:
        images: list[Image.Image] = []
        for index, image_url_group in enumerate(image_url_groups, 1):
            image_bytes = await self._download_image_from_candidates(session, image_url_group, index)
            images.append(self._open_rgb_image(image_bytes))

        if not images:
            raise VideoParserError("图文作品未下载到可用图片")

        return self._stitch_images_vertically(images)

    async def _download_image_from_candidates(
        self,
        session: aiohttp.ClientSession,
        image_urls: list[str],
        index: int,
    ) -> bytes:
        last_error: Exception | None = None
        for image_url in image_urls:
            try:
                return await self._download_image(session, image_url)
            except VideoParserError as e:
                last_error = e
                logger.warning(f"第 {index} 张图下载失败，尝试备用地址: {e}")

        raise VideoParserError(f"第 {index} 张图所有备用地址均下载失败: {last_error}")

    @staticmethod
    def _open_rgb_image(image_bytes: bytes) -> Image.Image:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        if image.mode != "RGB":
            image = image.convert("RGB")
        return image

    @staticmethod
    def _stitch_images_vertically(images: list[Image.Image]) -> bytes:
        if not images:
            raise VideoParserError("没有可拼接的图片")

        target_width = max(image.width for image in images)
        resized_images: list[Image.Image] = []
        for image in images:
            if image.width != target_width:
                height = max(1, round(image.height * target_width / image.width))
                image = image.resize((target_width, height), Image.Resampling.LANCZOS)
            resized_images.append(image)

        total_height = sum(image.height for image in resized_images)
        long_image = Image.new("RGB", (target_width, total_height), "white")

        offset_y = 0
        for image in resized_images:
            long_image.paste(image, (0, offset_y))
            offset_y += image.height

        output = io.BytesIO()
        long_image.save(output, format="JPEG", quality=92)
        return output.getvalue()

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

    @classmethod
    def _normalize_card_thumb_bytes(cls, image_bytes: bytes) -> tuple[bytes, int, int]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            if image.mode != "RGB":
                image = image.convert("RGB")

            resampling = getattr(Image, "Resampling", Image)
            method = getattr(resampling, "LANCZOS", getattr(Image, "LANCZOS", 1))
            image.thumbnail(cls.CARD_THUMB_SIZE, method)

            output = io.BytesIO()
            image.save(output, format="JPEG", quality=86, optimize=True)
            return output.getvalue(), image.width, image.height

    @staticmethod
    def _build_note_caption(note_info: dict[str, Any]) -> str:
        lines = []
        author = note_info.get("author", "")
        title = note_info.get("title", "")

        if author:
            lines.append(f"作者：{author}")
        if title:
            lines.append(f"文案：{title}")
        return "\n".join(lines)

    async def _send_video_card(self, bot: WechatAPIClient, group_id: str, video_info: dict):
        try:
            title = video_info.get("title", "")
            author = video_info.get("author", "")
            display_title = f"{title[:30]} - {author[:10]}" if author else title[:40]
            if not display_title:
                display_title = "抖音视频"

            video_url = video_info.get("url", "")
            raw_thumb_url = str(video_info.get("cover") or "").strip()
            thumb_url = self._normalize_card_thumb_url(raw_thumb_url)
            cdn_thumb = await self._upload_card_thumb(bot, raw_thumb_url)
            description = "点击观看无水印视频"

            logger.info(
                "准备发送抖音视频卡片: to={}, title={}, url={}, raw_thumb={}, card_thumb={}, cdn_thumb={}",
                group_id,
                display_title,
                video_url,
                raw_thumb_url,
                thumb_url,
                bool(cdn_thumb),
            )
            if hasattr(bot, "send_app_message"):
                xml = self._build_video_card_xml(
                    title=display_title,
                    description=description,
                    url=video_url,
                    thumb_url=thumb_url,
                    cdn_thumb=cdn_thumb,
                )
                await bot.send_app_message(group_id, xml, 49)
            else:
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

    async def _upload_card_thumb(self, bot: WechatAPIClient, thumb_url: str) -> dict[str, Any]:
        if not thumb_url or not hasattr(bot, "call_path"):
            return {}

        try:
            async with self._create_session() as session:
                image_bytes = await self._download_image(session, thumb_url)
            thumb_bytes, width, height = self._normalize_card_thumb_bytes(image_bytes)
            payload = {"imageContent": base64.b64encode(thumb_bytes).decode("ascii")}
            response = await bot.call_path("/message/UploadImageToCDN", body=payload)
            cdn_info = self._extract_cdn_thumb_info(response)
            if not cdn_info:
                logger.warning("抖音封面上传 CDN 未返回可用字段: {}", response)
                return {}

            cdn_info.setdefault("cdnthumblength", len(thumb_bytes))
            cdn_info.setdefault("cdnthumbwidth", width)
            cdn_info.setdefault("cdnthumbheight", height)
            logger.info(
                "抖音封面已上传 CDN: url={}, width={}, height={}, length={}",
                cdn_info.get("cdnthumburl", ""),
                cdn_info.get("cdnthumbwidth", 0),
                cdn_info.get("cdnthumbheight", 0),
                cdn_info.get("cdnthumblength", 0),
            )
            return cdn_info
        except Exception as exc:
            logger.warning("上传抖音卡片封面 CDN 失败，将使用远程 thumburl: {}", exc)
            return {}

    @classmethod
    def _extract_cdn_thumb_info(cls, response: Any) -> dict[str, Any]:
        candidates = cls._collect_dicts(response)

        info: dict[str, Any] = {}
        field_map = {
            "cdnthumburl": (
                "cdnthumburl",
                "cdnThumbUrl",
                "CdnThumbUrl",
                "cdnThumbURL",
                "CdnThumbURL",
                "cdnThumbImgUrl",
                "CdnThumbImgUrl",
                "cdnThumbImgURL",
                "CdnThumbImgURL",
                "cdnMidImgUrl",
                "CdnMidImgUrl",
                "cdnBigImgUrl",
                "CdnBigImgUrl",
                "fileId",
                "FileId",
                "fileID",
                "FileID",
            ),
            "cdnthumbaeskey": ("cdnthumbaeskey", "cdnThumbAesKey", "CdnThumbAesKey", "aeskey", "aesKey", "AesKey"),
            "cdnthumbmd5": ("cdnthumbmd5", "cdnThumbMd5", "CdnThumbMd5", "imageMD5", "imageMd5", "md5", "Md5", "MD5"),
            "cdnthumblength": ("cdnthumblength", "cdnThumbLength", "CdnThumbLength", "recvLen", "length", "Length", "size", "Size"),
            "cdnthumbwidth": ("cdnthumbwidth", "cdnThumbWidth", "CdnThumbWidth", "width", "Width"),
            "cdnthumbheight": ("cdnthumbheight", "cdnThumbHeight", "CdnThumbHeight", "height", "Height"),
        }
        for normalized_key, aliases in field_map.items():
            for candidate in candidates:
                for alias in aliases:
                    value = candidate.get(alias)
                    if value not in (None, ""):
                        info[normalized_key] = value
                        break
                if normalized_key in info:
                    break

        return info if info.get("cdnthumburl") and info.get("cdnthumbaeskey") else {}

    @classmethod
    def _collect_dicts(cls, value: Any) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        if isinstance(value, dict):
            collected.append(value)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    collected.extend(cls._collect_dicts(nested))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    collected.extend(cls._collect_dicts(item))
        return collected

    @classmethod
    def _build_video_card_xml(
        cls,
        title: str,
        description: str,
        url: str,
        thumb_url: str,
        cdn_thumb: dict[str, Any] | None = None,
    ) -> str:
        safe_title = cls._xml_escape(title or "抖音视频")
        safe_desc = cls._xml_escape(description or "点击观看无水印视频")
        safe_url = cls._xml_escape(url or "")
        safe_thumb = cls._xml_escape(thumb_url or cls.DEFAULT_THUMB_URL)
        cdn_thumb = cdn_thumb or {}
        safe_cdn_thumb_url = cls._xml_escape(cdn_thumb.get("cdnthumburl", ""))
        safe_cdn_thumb_aeskey = cls._xml_escape(cdn_thumb.get("cdnthumbaeskey", ""))
        safe_cdn_thumb_md5 = cls._xml_escape(cdn_thumb.get("cdnthumbmd5", ""))
        safe_cdn_thumb_length = cls._xml_escape(cdn_thumb.get("cdnthumblength", 0))
        safe_cdn_thumb_width = cls._xml_escape(cdn_thumb.get("cdnthumbwidth", 0))
        safe_cdn_thumb_height = cls._xml_escape(cdn_thumb.get("cdnthumbheight", 0))
        return (
            '<appmsg appid="" sdkver="0">'
            f"<title>{safe_title}</title>"
            f"<des>{safe_desc}</des>"
            "<action>view</action>"
            "<type>5</type>"
            "<showtype>0</showtype>"
            "<content></content>"
            f"<url>{safe_url}</url>"
            f"<lowurl>{safe_url}</lowurl>"
            "<forwardflag>0</forwardflag>"
            "<dataurl></dataurl>"
            "<lowdataurl></lowdataurl>"
            "<contentattr>0</contentattr>"
            "<streamvideo>"
            "<streamvideourl></streamvideourl>"
            "<streamvideototaltime>0</streamvideototaltime>"
            "<streamvideotitle></streamvideotitle>"
            "<streamvideowording></streamvideowording>"
            "<streamvideoweburl></streamvideoweburl>"
            f"<streamvideothumburl>{safe_thumb}</streamvideothumburl>"
            "<streamvideoaduxinfo></streamvideoaduxinfo>"
            "<streamvideopublishid></streamvideopublishid>"
            "</streamvideo>"
            "<canvasPageItem><canvasPageXml><![CDATA[]]></canvasPageXml></canvasPageItem>"
            "<appattach>"
            "<totallen>0</totallen>"
            "<attachid></attachid>"
            "<cdnattachurl></cdnattachurl>"
            "<emoticonmd5></emoticonmd5>"
            f"<aeskey>{safe_cdn_thumb_aeskey}</aeskey>"
            "<fileext>jpg</fileext>"
            f"<cdnthumburl>{safe_cdn_thumb_url}</cdnthumburl>"
            f"<cdnthumbmd5>{safe_cdn_thumb_md5}</cdnthumbmd5>"
            f"<cdnthumbaeskey>{safe_cdn_thumb_aeskey}</cdnthumbaeskey>"
            "<encryver>0</encryver>"
            f"<cdnthumblength>{safe_cdn_thumb_length}</cdnthumblength>"
            f"<cdnthumbwidth>{safe_cdn_thumb_width}</cdnthumbwidth>"
            f"<cdnthumbheight>{safe_cdn_thumb_height}</cdnthumbheight>"
            "<islargefilemsg>0</islargefilemsg>"
            "</appattach>"
            "<extinfo></extinfo>"
            "<androidsource>2</androidsource>"
            f"<thumburl>{safe_thumb}</thumburl>"
            "<mediatagname></mediatagname>"
            "<messageaction><![CDATA[]]></messageaction>"
            "<messageext><![CDATA[]]></messageext>"
            "<emoticongift><packageflag>0</packageflag><packageid></packageid></emoticongift>"
            "<emoticonshared><packageflag>0</packageflag><packageid></packageid></emoticonshared>"
            "<webviewshared>"
            f"<shareUrlOriginal>{safe_url}</shareUrlOriginal>"
            f"<shareUrlOpen>{safe_url}</shareUrlOpen>"
            "<jsAppId></jsAppId><publisherId></publisherId><publisherReqId></publisherReqId>"
            "</webviewshared>"
            "<finderLiveProductShare><isPriceBeginShow>false</isPriceBeginShow></finderLiveProductShare>"
            "<gameshare><appbrandext><priority>-1</priority></appbrandext><duration>-1</duration></gameshare>"
            "<directshare>0</directshare>"
            "</appmsg>"
        )

    @staticmethod
    def _xml_escape(value: Any) -> str:
        return html.escape(str(value or ""), quote=False)
