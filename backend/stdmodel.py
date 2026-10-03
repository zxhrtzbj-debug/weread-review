"""pydantic.BaseModel 的最小替代：基于 dataclasses。

只提供本项目真正用到的两个方法：model_dump(exclude_none=...) 和 replace(...)。
具体模型写成 @dataclass 并继承 Model，外部构造方式（关键字参数、**dict）保持一致。
"""

from __future__ import annotations

from dataclasses import fields


class Model:
    """pydantic 风格的数据模型基类。子类用 @dataclass 装饰即可。"""

    def model_dump(
        self, *, exclude_none: bool = False, exclude: tuple[str, ...] = ()
    ) -> dict:
        out = {}
        for f in fields(self):
            if f.name in exclude:
                continue
            value = getattr(self, f.name)
            if exclude_none and value is None:
                continue
            out[f.name] = value
        return out

    def replace(self, **patch) -> "Model":
        """返回合并了 patch 的新实例（忽略 None）。"""
        merged = {**self.model_dump(), **{k: v for k, v in patch.items() if v is not None}}
        return type(self)(**merged)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        inner = ", ".join(f"{f.name}={getattr(self, f.name)!r}" for f in fields(self))
        return f"{type(self).__name__}({inner})"
