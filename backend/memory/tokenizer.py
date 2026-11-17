"""分词与症状归一

为什么自己写而不引分词库：
1. 运行环境要跨架构可移植，jieba 虽为纯 Python，但会带来额外词典体积与启动开销；
2. 运维文本的高区分度 token 是命令名、服务名、端口号、错误码，这些用正则就能切得很准；
3. 中文用字符 bigram，配合同义词表做症状归一，对「起不来 / 启动失败」这类表述差异足够。

同义词表是数据而非代码，后续可扩充到 200 条以上而不改逻辑。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List

# 英文标识符、命令名、版本号
_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.\-]*|\d+(?:\.\d+)*")
# 连续中文
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
# 常见无意义字符
_NOISE_RE = re.compile(r"[\s\u3000]+")

# ── 症状同义词表 ──────────────────────────────────────────────────────
# key 为规范码，value 为该症状的常见表述。匹配时按短语长度降序，避免短词抢先。
SYMPTOM_SYNONYMS: Dict[str, List[str]] = {
    "start_failure": ["起不来", "启动失败", "无法启动", "拉不起来", "拉起失败", "服务挂了", "启动不了"],
    "port_conflict": ["端口冲突", "端口占用", "端口被占", "地址已被使用", "address already in use"],
    "disk_full": ["磁盘满", "磁盘空间不足", "空间不够", "no space left", "磁盘写满"],
    "high_cpu": ["占用高", "cpu 高", "cpu占用过高", "负载高", "跑满"],
    "high_mem": ["内存涨", "内存持续增长", "内存泄漏", "内存占用高", "oom"],
    "perm_denied": ["权限不足", "没有权限", "permission denied", "拒绝访问"],
    "dep_missing": ["依赖缺失", "缺少依赖", "找不到模块", "no module named", "找不到包"],
    "net_unreachable": ["连不上", "网络不通", "无法连接", "connection refused", "超时"],
    "log_error": ["报错", "日志异常", "错误日志", "异常堆栈"],
    "service_timeout": ["响应慢", "超时", "卡住", "无响应"],
}

# 命令别名归一：让同一动作的不同写法落到同一 token
COMMAND_ALIASES: Dict[str, str] = {
    "systemctl": "systemctl",
    "service": "service",
    "ps": "ps",
    "pgrep": "pgrep",
    "pidof": "pidof",
    "ss": "ss",
    "netstat": "netstat",
    "lsof": "lsof",
    "df": "df",
    "du": "du",
    "journalctl": "journalctl",
    "kill": "kill",
    "pkill": "pkill",
    "rm": "rm",
}


def _build_phrase_index() -> List[tuple[str, str]]:
    """短语按长度降序排列，保证「端口被占用」优先于「端口」。"""
    pairs: List[tuple[str, str]] = []
    for code, phrases in SYMPTOM_SYNONYMS.items():
        for p in phrases:
            pairs.append((p.lower(), code))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


_PHRASES = _build_phrase_index()


def normalize_text(text: str) -> str:
    """全角转半角、统一小写、压缩空白。标点保留，因为命令里可能有。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = _NOISE_RE.sub(" ", text)
    return text.strip()


def symptom_codes(text: str) -> List[str]:
    """返回文本命中的症状规范码。

    匹配前压掉空白，这样「磁盘 空间不足」与「磁盘空间不足」等价。
    只返回码，不改动原文——归一化必须保留字面 token，
    否则「磁盘空间不足」被替换成 disk_full 后，按字面查询反而召回不到。
    """
    compact = normalize_text(text).replace(" ", "")
    codes: List[str] = []
    for phrase, code in _PHRASES:
        if phrase.replace(" ", "") in compact and code not in codes:
            codes.append(code)
    return codes


def normalize_symptom(text: str) -> str:
    """在原文后追加规范码，供分词使用。

    例：「ollama 起不来了」→「ollama 起不来了 start_failure」
    原文与规范码都保留：前者保证字面匹配，后者保证同义召回。
    """
    base = normalize_text(text)
    codes = symptom_codes(text)
    return f"{base} {' '.join(codes)}".strip() if codes else base


def tokens(text: str, normalize: bool = True) -> List[str]:
    """切词。英文/数字成词，中文按 bigram。

    normalize=True 时追加症状规范码，让同义表述产生相同的 token。
    """
    src = normalize_symptom(text) if normalize else normalize_text(text)
    out: List[str] = []

    for m in _WORD_RE.finditer(src):
        w = m.group(0).lower().strip(".-")
        if not w:
            continue
        out.append(COMMAND_ALIASES.get(w, w))

    for m in _CJK_RE.finditer(src):
        run = m.group(0)
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))

    return out


def term_freq(text: str, normalize: bool = True) -> Dict[str, int]:
    """词频统计，供 BM25 使用。"""
    tf: Dict[str, int] = {}
    for t in tokens(text, normalize=normalize):
        tf[t] = tf.get(t, 0) + 1
    return tf


def unique_terms(text: str, normalize: bool = True) -> List[str]:
    """去重后的词项，保持首次出现顺序。"""
    seen = set()
    out = []
    for t in tokens(text, normalize=normalize):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def explained_hits(query: str, doc: str, top: int = 5) -> List[str]:
    """返回命中词项，用于解释「为什么召回这条」。

    答辩与追责都需要这个：能指出是「端口 + 占用 + 11434」而非「相似度 0.87」。
    """
    q = set(tokens(query))
    d = set(tokens(doc))
    hits = [t for t in q & d if len(t) > 1]
    hits.sort(key=lambda t: (-len(t), t))
    return hits[:top]


def synonym_codes() -> Iterable[str]:
    return SYMPTOM_SYNONYMS.keys()
