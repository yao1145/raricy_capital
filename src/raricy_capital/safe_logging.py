"""安全堆栈：只保留「模块.函数:行号」与稳定分类。

只带 :func:`safe_stack` 及其局部辅助函数 —— 运维层把异常写进事件与滚动日志时
只需要这一部分；原机器人日志体系里的字段白名单、凭据登记中心与第三方日志压制
不在本次拆分范围内。

堆栈只保留文件名主干、函数名与行号：不含源码行、局部变量、异常原文与绝对路径。
异常链（``__cause__`` / ``__context__``）按深度上限展开，取每条链**末尾**的若干帧
—— 越靠后越接近真正抛出的位置。
"""

from __future__ import annotations

import os
import re
from typing import Any

# 异常链与帧数的上限。堆栈是用来定位「哪一行」的，不是用来复现现场的。
_STACK_MAX_FRAMES: int = 6
_STACK_MAX_DEPTH: int = 3

_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _safe_frame(filename: str, function: str) -> str | None:
    """把一帧压成 `模块.函数:行号`；名字不合法就整帧丢弃。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    if not _NAME_RE.match(stem):
        return None
    function_name = "module" if function == "<module>" else function
    if not _NAME_RE.match(function_name):
        function_name = "function"
    return f"{stem}.{function_name}"


def safe_stack(exc: BaseException | None, *, line_of: Any | None = None) -> str | None:
    """把异常的调用链压成 `模块.函数:行号` 列表，供日志字段使用。

    ``line_of`` 只作签名兼容保留（原实现同样未使用它）。
    """
    if exc is None:
        return None
    frames: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    depth = 0
    while current is not None and depth < _STACK_MAX_DEPTH and id(current) not in seen:
        seen.add(id(current))
        extracted = _extract_frames(current)
        if len(frames) + len(extracted) > _STACK_MAX_FRAMES:
            frames.extend(extracted[len(extracted) - (_STACK_MAX_FRAMES - len(frames)):])
        else:
            frames.extend(extracted)
        current = current.__cause__ or current.__context__
        depth += 1
    return ",".join(frames) or None


def _extract_frames(exc: BaseException) -> list[str]:
    """取单条异常的末尾若干帧；取不到来源信息的合成异常返回空列表。"""
    traceback = exc.__traceback__
    if traceback is None:
        return []
    frames: list[str] = []
    while traceback is not None:
        code = traceback.tb_frame.f_code
        rendered = _safe_frame(code.co_filename, code.co_name)
        if rendered is not None:
            frames.append(f"{rendered}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    return frames[-_STACK_MAX_FRAMES:]


__all__ = ['safe_stack']
