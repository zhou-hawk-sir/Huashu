"""轻量 TF-IDF 索引器

无需额外依赖（不引入 sklearn/numpy），手写 inverted index + TF-IDF 评分。
适合 200~10000 条命令规模。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from .store import CmdEntry


# 中英文统一分词：英文按词，中文按字符 bigram
_TOKEN_PATTERN = re.compile(r"[A-Za-z_]+|[一-龥]")


def _tokenize(text: str) -> List[str]:
    """简单分词：
    - 英文按字母/下划线
    - 中文按单字（实际可用 bigram，本系统规模小，单字足够）
    - 数字忽略
    """
    tokens: List[str] = []
    for m in _TOKEN_PATTERN.finditer(text.lower()):
        tok = m.group(0)
        # 中文单字
        if "一" <= tok[0] <= "龥":
            tokens.append(tok)
        else:
            tokens.append(tok)
    return tokens


def _bigram_tokenize_chinese(text: str) -> List[str]:
    """对中文片段生成 bigram。"""
    chinese = "".join(re.findall(r"[一-龥]", text.lower()))
    return [chinese[i:i + 2] for i in range(len(chinese) - 1)] if len(chinese) >= 2 else list(chinese)


class TfidfIndexer:
    """TF-IDF 倒排索引。"""

    def __init__(self) -> None:
        # term -> {doc_id: tf}
        self._postings: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._doc_freq: Dict[str, int] = defaultdict(int)
        self._doc_lens: Dict[str, int] = {}
        self._docs: Dict[str, str] = {}   # doc_id -> text
        self._n_docs = 0
        self._avg_dl = 0.0
        self._dirty = True

    def add_document(self, doc_id: str, text: str) -> None:
        self._dirty = True
        tokens = _tokenize(text) + _bigram_tokenize_chinese(text)
        self._docs[doc_id] = text
        self._doc_lens[doc_id] = len(tokens)
        counter = Counter(tokens)
        seen_terms = set()
        for tok, cnt in counter.items():
            self._postings[tok][doc_id] = cnt
            seen_terms.add(tok)
        for t in seen_terms:
            self._doc_freq[t] += 1
        self._n_docs += 1

    def build(self) -> None:
        self._avg_dl = (
            sum(self._doc_lens.values()) / self._n_docs if self._n_docs else 0.0
        )
        self._dirty = False

    def _tfidf(self, term: str, doc_id: str) -> float:
        tf = self._postings.get(term, {}).get(doc_id, 0)
        if tf == 0:
            return 0.0
        df = self._doc_freq.get(term, 0)
        if df == 0:
            return 0.0
        # TF * IDF（log 平滑）
        idf = math.log((1 + self._n_docs) / (1 + df)) + 1
        return tf * idf

    def score(self, query: str, doc_ids: List[str]) -> List[Tuple[str, float]]:
        """对查询打分，返回 [(doc_id, score), ...] 按分数降序。"""
        if self._dirty:
            self.build()

        q_tokens = _tokenize(query) + _bigram_tokenize_chinese(query)
        if not q_tokens:
            return []

        q_counter = Counter(q_tokens)
        scores: Dict[str, float] = {}
        doc_ids_set = set(doc_ids)
        for term, qtf in q_counter.items():
            postings = self._postings.get(term, {})
            idf = math.log((1 + self._n_docs) / (1 + self._doc_freq.get(term, 0))) + 1
            for doc_id, tf in postings.items():
                if doc_id not in doc_ids_set:
                    continue
                scores[doc_id] = scores.get(doc_id, 0.0) + qtf * tf * idf

        # 长度归一化（除以 sqrt(doc_len)，模拟 cosine）
        normalized = []
        for doc_id, score in scores.items():
            dl = max(1, self._doc_lens.get(doc_id, 1))
            normalized.append((doc_id, score / math.sqrt(dl)))

        normalized.sort(key=lambda x: x[1], reverse=True)
        return normalized

    def doc_count(self) -> int:
        return self._n_docs