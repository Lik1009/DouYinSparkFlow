import re
import unicodedata


def norm(s: str) -> str:
    """规范化文本：NFKC 归一化、清除零宽字符、合并空白"""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s
