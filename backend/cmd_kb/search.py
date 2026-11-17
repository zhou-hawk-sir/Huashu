"""命令检索器

提供：
- 精确匹配（按名字）
- 关键词搜索（按 category/desc）
- 语义搜索（TF-IDF）

混合打分：TF-IDF 0.7 + 关键词命中 0.3
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from utils.logger import get_logger
from .indexer import TfidfIndexer
from .store import CmdEntry, CmdStore, get_default_store

logger = get_logger("cmd_kb.search")


@dataclass
class SearchResult:
    """单条检索结果。"""
    entry: CmdEntry
    score: float
    matched_by: List[str] = field(default_factory=list)  # tfidf | keyword | exact

    def to_text(self) -> str:
        e = self.entry
        lines = [f"- **{e.name}** ({e.category}, 风险={e.risk})：{e.desc}"]
        if e.common_args:
            lines.append(f"  常用参数：{' '.join(e.common_args)}")
        if e.dangerous_args:
            lines.append(f"  危险参数：{' '.join(e.dangerous_args)}")
        if e.examples:
            lines.append(f"  示例：{'; '.join(e.examples)}")
        return "\n".join(lines)


class CmdSearcher:
    """命令检索器。"""

    def __init__(self, store: Optional[CmdStore] = None) -> None:
        self.store = store or get_default_store()
        self._indexer = TfidfIndexer()
        self._built = False
        self._build_index()

    def _build_index(self) -> None:
        for e in self.store.all():
            self._indexer.add_document(e.name, e.to_text())
        self._indexer.build()
        self._built = True

    def refresh(self) -> None:
        """store 更新后重建索引。"""
        self._indexer = TfidfIndexer()
        self._build_index()

    def search(
        self,
        query: str,
        top_k: int = 8,
        min_score: float = 0.05,
        category: Optional[str] = None,
        risk: Optional[str] = None,
    ) -> List[SearchResult]:
        """混合检索：TF-IDF + 关键词。"""
        if not query:
            return []

        all_entries = self.store.all()
        if category:
            all_entries = [e for e in all_entries if e.category == category]
        if risk:
            all_entries = [e for e in all_entries if e.risk == risk]

        # 精确匹配优先
        exact = self.store.get(query.strip())
        exact_results: List[SearchResult] = []
        if exact is not None:
            exact_results.append(SearchResult(
                entry=exact, score=10.0, matched_by=["exact"]
            ))

        # TF-IDF 检索
        doc_ids = [e.name for e in all_entries]
        tfidf_scores = dict(self._indexer.score(query, doc_ids))

        # 关键词命中（category/desc/common_args 子串匹配）
        keyword_scores: dict[str, float] = {}
        q_lower = query.lower()
        for e in all_entries:
            score = 0.0
            hits = []
            if q_lower in e.name.lower():
                score += 2.0
                hits.append("name")
            if q_lower in e.category.lower():
                score += 1.5
                hits.append("category")
            if q_lower in e.desc.lower():
                score += 1.0
                hits.append("desc")
            for arg in e.common_args:
                if q_lower in arg.lower():
                    score += 0.5
                    hits.append("arg")
                    break
            if score > 0:
                keyword_scores[e.name] = score
                # 记录匹配维度（每个 entry 单次即可）

        # 合并
        merged: dict[str, float] = {}
        match_kind: dict[str, list[str]] = {}
        for name, s in tfidf_scores.items():
            merged[name] = s * 0.7
            match_kind.setdefault(name, []).append("tfidf")
        for name, s in keyword_scores.items():
            merged[name] = merged.get(name, 0.0) + s * 0.3
            match_kind.setdefault(name, []).append("keyword")

        # 排序 + 阈值过滤
        sorted_names = sorted(merged.items(), key=lambda x: x[1], reverse=True)
        results: List[SearchResult] = list(exact_results)

        for name, score in sorted_names:
            if any(r.entry.name == name for r in exact_results):
                continue
            if score < min_score:
                continue
            entry = self.store.get(name)
            if entry is None:
                continue
            results.append(SearchResult(
                entry=entry, score=round(score, 4),
                matched_by=match_kind.get(name, []),
            ))
            if len(results) >= top_k:
                break

        return results

    def search_to_text(self, query: str, top_k: int = 8, **kwargs) -> str:
        """检索并格式化为可塞入 Prompt 的文本。"""
        results = self.search(query, top_k=top_k, **kwargs)
        if not results:
            return "（未在命令知识库中找到匹配项）"
        lines = [f"以下命令与「{query}」相关："]
        for r in results:
            lines.append(r.to_text())
        return "\n".join(lines)