"""站内消息 DTO（独立最小子集）。

只保留基金私聊命令真正用到的那部分：作者、图片、博客、拍一拍与引用块，以及顶层
:class:`ChatMessage`。大区/SSE 事件模型、云剪贴板、投票与用户搜索模型都属于原
机器人工程，**不带进本包**。

解析一律容错：字段缺失取默认值、多余字段忽略、可选块（图片/博客/拍一拍/引用）
降级为 None。唯一的例外是顶层 `id`：解析失败必须抛 `ValueError`，因为无法定位
的消息不能静默放过。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _as_str(value: object, default: str = "") -> str:
    """只接受字符串，其余类型（含 None）取默认值。"""
    return value if isinstance(value, str) else default


def _as_optional_str(value: object) -> str | None:
    """只接受字符串，其余类型（含 None）为 None。"""
    return value if isinstance(value, str) else None


def _as_bool(value: object, default: bool = False) -> bool:
    """只接受真正的布尔值，其余类型取默认值。"""
    return value if isinstance(value, bool) else default


def _coerce_int(value: object) -> int:
    """把 JSON 里的整数或数字字符串转成 int；其余一律抛 ValueError。"""
    if isinstance(value, bool) or value is None:
        raise ValueError("不是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("不是整数")
        return int(value)
    if isinstance(value, str):
        # 非数字字符串由 int() 抛出 ValueError。
        return int(value.strip())
    raise ValueError("不是整数")


def _parse_author(value: object) -> "Author":
    """解析作者；非映射或字段缺失都退化为空作者。"""
    if not isinstance(value, Mapping):
        return Author(id="", username="")
    return Author(
        id=_as_str(value.get("id")),
        username=_as_str(value.get("username")),
        avatar_url=_as_str(value.get("avatar_url")),
        is_admin=_as_bool(value.get("is_admin")),
    )


def _parse_image(value: object) -> "ImageRef | None":
    """解析图片引用；非映射（含 null）为 None。"""
    if not isinstance(value, Mapping):
        return None
    return ImageRef(
        id=_as_str(value.get("id")),
        url=_as_str(value.get("url")),
        mime_type=_as_str(value.get("mime_type")),
    )


def _parse_blog(value: object) -> "BlogRef | None":
    """解析博客引用；非映射（含 null）为 None。"""
    if not isinstance(value, Mapping):
        return None
    return BlogRef(
        id=_as_str(value.get("id")),
        title=_as_str(value.get("title")),
        description=_as_str(value.get("description")),
        author=_as_optional_str(value.get("author")),
        updated_at=_as_str(value.get("updated_at")),
    )


def _parse_pat(value: object) -> "PatRef | None":
    """解析拍一拍引用；非映射（含 null）为 None。"""
    if not isinstance(value, Mapping):
        return None
    return PatRef(
        target_id=_as_str(value.get("target_id")),
        target_name=_as_str(value.get("target_name")),
    )


def _parse_reply(value: object) -> "ReplyRef | None":
    """解析引用块；id 解析失败时整块降级为 None（比丢掉整条消息好）。"""
    if not isinstance(value, Mapping):
        return None
    try:
        reply_id = _coerce_int(value.get("id"))
    except ValueError:
        return None
    return ReplyRef(
        id=reply_id,
        content=_as_str(value.get("content")),
        author_name=_as_optional_str(value.get("author_name")),
        is_deleted=_as_bool(value.get("is_deleted")),
        image_url=_as_optional_str(value.get("image_url")),
    )


@dataclass(frozen=True)
class Author:
    """消息作者。"""

    id: str
    username: str
    avatar_url: str = ""
    is_admin: bool = False


@dataclass(frozen=True)
class ImageRef:
    """消息引用的图床图片。"""

    id: str
    url: str
    mime_type: str


@dataclass(frozen=True)
class BlogRef:
    """消息引用的博客。"""

    id: str
    title: str
    description: str
    author: str | None
    updated_at: str


@dataclass(frozen=True)
class PatRef:
    """拍一拍消息的目标。"""

    target_id: str
    target_name: str


@dataclass(frozen=True)
class ReplyRef:
    """被引用的消息摘要。"""

    id: int
    content: str
    author_name: str | None
    is_deleted: bool
    image_url: str | None


@dataclass(frozen=True)
class ChatMessage:
    """一条站内消息。"""

    id: int
    channel_id: str
    author: Author
    content: str
    image: ImageRef | None
    image_missing: bool
    blog: BlogRef | None
    blog_missing: bool
    pat: PatRef | None
    reply: ReplyRef | None
    is_deleted: bool
    created_at: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage":
        """从 DTO 解析；顶层 id 解析失败抛 ValueError，其余字段容错。"""
        if not isinstance(data, Mapping):
            raise ValueError("消息体不是映射")
        try:
            message_id = _coerce_int(data.get("id"))
        except ValueError as exc:
            raise ValueError("消息 id 缺失或非数字") from exc
        return cls(
            id=message_id,
            channel_id=_as_str(data.get("channel_id")),
            author=_parse_author(data.get("author")),
            content=_as_str(data.get("content")),
            image=_parse_image(data.get("image")),
            image_missing=_as_bool(data.get("image_missing")),
            blog=_parse_blog(data.get("blog")),
            blog_missing=_as_bool(data.get("blog_missing")),
            pat=_parse_pat(data.get("pat")),
            reply=_parse_reply(data.get("reply")),
            is_deleted=_as_bool(data.get("is_deleted")),
            created_at=_as_str(data.get("created_at")),
        )


__all__ = ["Author", "BlogRef", "ChatMessage", "ImageRef", "PatRef", "ReplyRef"]
