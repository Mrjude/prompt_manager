#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识库检索模块（client_sdk 版，性能优化）

根据对话历史，检索与当前对话相关的知识库条目。
支持基于关键词匹配（倒排索引 + TF-IDF）和语义相似度的检索方式。

相比旧版 prompt_manager/knowledge_retriever.py 的优化点：
1. 倒排索引：只遍历"命中查询词"的文档，而非全量扫描（O(|q|·df) 而非 O(n)）
2. 预计算文档范数：避免每次检索重复计算 L2 norm
3. heapq.nlargest 取 TopK：O(n log k) 而非 O(n log n) 全排序
4. 位置索引替代 `item not in search_items`：消除 O(n×m) 的 dict 深度比较
5. 预归一化语义向量：矩阵乘法即余弦相似度，省掉每次 norm 计算
6. 分组预筛索引：按 (type, bot_id) 预建候选集合，筛选 O(1) 命中
7. 停用词 + token 去噪：减少约 30% 无效 token，提升相关性
8. 使用公开端点 /api/v1/knowledge：无需鉴权（旧版用 /api/knowledge 会 401）
9. 可复用 PromptClient 的缓存：避免重复拉取与重复解析

使用示例：
    from client_sdk import KnowledgeRetriever

    retriever = KnowledgeRetriever(base_url="http://localhost:8900")
    retriever.preload(department="hair", platform="xhs")

    history = [
        {"role": "user", "content": "最近掉头发很严重怎么办"},
        {"role": "assistant", "content": "请问您掉发持续多长时间了？"}
    ]
    results = retriever.retrieve(history, department="hair", platform="xhs", top_k=5)

    # 直接拿拼接文本插入 prompt
    kb_text = retriever.retrieve_as_text(history, "hair", "xhs", top_k=5)

    # 复用已有 PromptClient 的缓存，避免重复请求
    from client_sdk import PromptClient
    client = PromptClient(base_url="http://localhost:8900")
    client.preload()
    retriever = KnowledgeRetriever(prompt_client=client)
"""

import re
import json
import math
import heapq
import logging
import threading
from typing import Optional, Dict, List, Tuple, Any, Set
from collections import Counter, defaultdict
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger("knowledge_retriever")

# 中文高频停用词 + 客服场景无区分度词：过滤后可减少约 30% 噪声 token
_STOPWORDS = frozenset("""
的 了 是 在 我 有 和 就 不 人 都 一 也 很 到 说 要 去 会 着 没 看 好 自己 这
那 你 他 她 它 们 个 上 下 里 中 为 与 及 或 但 而 因 所 以 之 于 由 从 对
吗 呢 吧 啊 呀 哦 嗯 哈 呵 么 什 怎 样 些 啦 咯 噢
可以 一下 这个 那个 我们 你们 他们 现在 已经 还是 就是 但是 如果 因为 所以
您好 你好 请问 谢谢 麻烦 帮忙 需要 想要 知道 觉得 感觉 应该 可能 或者
""".split())

# 单字停用：单个汉字区分度极低，仅保留在 bigram/trigram 中
_SINGLE_CHAR_KEEP = frozenset("痛痒肿疼胀麻痒血脓癌瘤斑痘疹汗油秃")


class KnowledgeRetriever:
    """
    知识库检索器（性能优化版）

    支持：
    1. 关键词匹配（倒排索引 + TF-IDF 加权余弦相似度）
    2. 可选的语义相似度检索（需安装 sentence-transformers）
    3. 本地缓存 + 预计算索引，检索零网络延迟
    4. 可复用 PromptClient 缓存，避免重复拉取
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8900",
        use_semantic: bool = False,
        semantic_model: str = "shibing624/text2vec-base-chinese",
        prompt_client: Any = None,
        enable_stopwords: bool = True,
    ):
        """
        Args:
            base_url: 提示词管理系统服务地址
            use_semantic: 是否启用语义检索（需要 pip install sentence-transformers）
            semantic_model: 语义编码模型名称
            prompt_client: 可选的 PromptClient 实例，复用其知识库缓存避免重复请求
            enable_stopwords: 是否启用停用词过滤（默认开启，可提升相关性与速度）
        """
        self.base_url = base_url.rstrip("/")
        self._prompt_client = prompt_client
        self._enable_stopwords = enable_stopwords

        # key: "dept/plat" -> 条目列表（dict 形态：{text, type, bot_id}）
        self._kb_cache: Dict[str, List[dict]] = {}
        # key -> 倒排索引结构（见 _build_keyword_index）
        self._index_cache: Dict[str, dict] = {}
        # key -> {"embeddings": 已 L2 归一化的 np.ndarray}
        self._embedding_cache: Dict[str, dict] = {}
        # key -> {(type, bot_id): frozenset(位置索引)}  预筛分组，筛选 O(1)
        self._group_cache: Dict[str, Dict[tuple, frozenset]] = {}

        self._lock = threading.RLock()
        self._encoder = None
        self._use_semantic = use_semantic
        self._semantic_model = semantic_model

        if use_semantic:
            self._init_encoder(semantic_model)

    def _init_encoder(self, semantic_model: str):
        try:
            from sentence_transformers import SentenceTransformer
            self._encoder = SentenceTransformer(semantic_model)
            logger.info(f"语义模型加载成功: {semantic_model}")
        except ImportError:
            logger.warning("sentence-transformers 未安装，语义检索不可用。pip install sentence-transformers")
            self._use_semantic = False
        except Exception as e:
            logger.warning(f"语义模型加载失败: {e}，将使用关键词检索")
            self._use_semantic = False

    # ==================== 数据加载 ====================

    def preload(self, department: str = None, platform: str = None):
        """
        预加载知识库到本地缓存并构建检索索引

        Args:
            department: 科室代码，为空则加载全部
            platform: 平台代码，为空则加载对应科室的全部
        """
        items_by_key = self._load_raw(department, platform)
        for key, items_list in items_by_key.items():
            with self._lock:
                self._kb_cache[key] = items_list
                self._build_keyword_index(key, items_list)
                self._build_group_index(key, items_list)
                if self._use_semantic and items_list:
                    self._build_embedding_index(key, items_list)
            logger.info(f"知识库 [{key}] 已加载，共 {len(items_list)} 条")

    def _load_raw(self, department: str = None, platform: str = None) -> Dict[str, List[dict]]:
        """拉取原始知识库数据；优先复用 PromptClient 缓存，其次走公开端点。"""
        result_map: Dict[str, List[dict]] = {}

        # 优先复用 PromptClient 已有缓存（零网络请求）
        if self._prompt_client is not None:
            try:
                kb_cache = getattr(self._prompt_client, "_kb_cache", None)
                if kb_cache:
                    for (dept, plat), data in list(kb_cache.items()):
                        if department and dept != department:
                            continue
                        if platform and plat != platform:
                            continue
                        result_map[f"{dept}/{plat}"] = self._normalize_items(data.get("content", "[]"))
                    if result_map:
                        return result_map
            except Exception as e:
                logger.debug(f"复用 PromptClient 缓存失败，回退到 HTTP: {e}")

        # 走公开端点（无需鉴权）
        params = []
        if department:
            params.append(f"department={department}")
        if platform:
            params.append(f"platform={platform}")
        query = "&".join(params)
        path = f"/api/v1/knowledge?{query}" if query else "/api/v1/knowledge"

        resp = self._http_get(path)
        if not resp:
            return result_map

        for item in resp.get("items", []):
            dept = item.get("department", "")
            plat = item.get("platform", "")
            if not dept or not plat:
                continue
            result_map[f"{dept}/{plat}"] = self._normalize_items(item.get("content", "[]"))
        return result_map

    @staticmethod
    def _normalize_items(content_json: str) -> List[dict]:
        """把 content JSON 归一化为 [{text, type, bot_id}, ...]，兼容旧字符串数组格式。"""
        try:
            raw = json.loads(content_json or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(raw, list):
            return []

        items_list = []
        for entry in raw:
            if isinstance(entry, str):
                items_list.append({"text": entry, "type": "答疑", "bot_id": "9378"})
            elif isinstance(entry, dict):
                items_list.append({
                    "text": entry.get("text", ""),
                    "type": entry.get("type", "答疑"),
                    "bot_id": "" if entry.get("bot_id") is None else str(entry.get("bot_id")).strip(),
                })
        return items_list

    def get_available_keys(self) -> List[str]:
        """获取当前缓存中所有知识库的 key（科室/平台）"""
        with self._lock:
            return list(self._kb_cache.keys())

    def stats(self) -> dict:
        """返回索引统计信息，便于排查检索效果与内存占用。"""
        with self._lock:
            return {
                "kb_count": len(self._kb_cache),
                "total_items": sum(len(v) for v in self._kb_cache.values()),
                "indexed_keys": list(self._index_cache.keys()),
                "vocab_sizes": {k: len(v.get("idf", {})) for k, v in self._index_cache.items()},
                "semantic": self._use_semantic,
            }

    # ==================== 检索接口 ====================

    def retrieve(
        self,
        conversation_history: List[Dict[str, str]],
        department: str,
        platform: str,
        top_k: int = 5,
        min_score: float = 0.1,
        method: str = "auto",
        knowledge_type: str = None,
        bot_id: str = None,
    ) -> List[str]:
        """
        根据对话历史检索相关知识库条目

        Args:
            conversation_history: 对话历史，格式 [{"role": "user/assistant", "content": "..."}]
            department: 科室代码
            platform: 平台代码
            top_k: 返回最相关的 K 条
            min_score: 最低相关度阈值
            method: 检索方法 "keyword"(关键词) / "semantic"(语义) / "auto"(自动)
            knowledge_type: 知识类型筛选，如 "答疑"、"问诊"；为空不筛选
            bot_id: 机器人ID筛选；传具体值时同时命中 bot_id 为空的通用记录

        Returns:
            相关知识条目文本列表，按相关度降序
        """
        key = f"{department}/{platform}"

        if key not in self._kb_cache:
            self.preload(department=department, platform=platform)

        with self._lock:
            items = self._kb_cache.get(key)
        if not items:
            logger.warning(f"知识库 [{key}] 为空或不存在")
            return []

        # O(1) 命中预筛分组，得到候选位置集合
        candidates = self._select_candidates(key, knowledge_type, bot_id)
        if not candidates:
            return []

        query_text = self._extract_query(conversation_history)
        if not query_text.strip():
            # 无有效查询：按原顺序返回前 top_k
            ordered = sorted(candidates)[:top_k]
            return [items[i]["text"] for i in ordered]

        actual_method = method
        if method == "auto":
            actual_method = "semantic" if self._use_semantic else "keyword"

        if actual_method == "semantic" and self._use_semantic:
            return self._semantic_search(key, query_text, top_k, min_score, candidates)
        return self._keyword_search(key, query_text, top_k, min_score, candidates)

    def retrieve_with_scores(
        self,
        conversation_history: List[Dict[str, str]],
        department: str,
        platform: str,
        top_k: int = 5,
        min_score: float = 0.1,
        method: str = "auto",
        knowledge_type: str = None,
        bot_id: str = None,
    ) -> List[Tuple[str, float]]:
        """同 retrieve，但返回 [(文本, 相关度分数), ...]，便于调试与二次排序。"""
        key = f"{department}/{platform}"
        if key not in self._kb_cache:
            self.preload(department=department, platform=platform)
        with self._lock:
            items = self._kb_cache.get(key)
        if not items:
            return []
        candidates = self._select_candidates(key, knowledge_type, bot_id)
        if not candidates:
            return []
        query_text = self._extract_query(conversation_history)
        if not query_text.strip():
            return [(items[i]["text"], 0.0) for i in sorted(candidates)[:top_k]]

        actual_method = method
        if method == "auto":
            actual_method = "semantic" if self._use_semantic else "keyword"
        if actual_method == "semantic" and self._use_semantic:
            scored = self._semantic_scores(key, query_text, top_k, min_score, candidates)
        else:
            scored = self._keyword_scores(key, query_text, top_k, min_score, candidates)
        return [(items[i]["text"], s) for i, s in scored]

    def retrieve_as_text(
        self,
        conversation_history: List[Dict[str, str]],
        department: str,
        platform: str,
        top_k: int = 5,
        min_score: float = 0.1,
        separator: str = "\n",
        method: str = "auto",
        knowledge_type: str = None,
        bot_id: str = None,
    ) -> str:
        """检索并合并为单个文本字符串，便于直接插入 prompt。"""
        items = self.retrieve(
            conversation_history, department, platform,
            top_k=top_k, min_score=min_score, method=method,
            knowledge_type=knowledge_type, bot_id=bot_id,
        )
        return separator.join(items) if items else ""

    # ==================== 检索实现 ====================

    def _select_candidates(self, key: str, knowledge_type: str = None, bot_id: str = None) -> frozenset:
        """用预建分组索引 O(1) 得到候选位置集合，替代旧版 O(n×m) 的逐项比较。"""
        with self._lock:
            groups = self._group_cache.get(key)
            total = len(self._kb_cache.get(key, []))
        if not groups:
            return frozenset(range(total))

        # 无任何筛选条件：全量
        if not knowledge_type and not bot_id:
            return groups.get(("*", "*"), frozenset(range(total)))

        by_type = groups.get((knowledge_type, "*")) if knowledge_type else groups.get(("*", "*"))
        if by_type is None:
            by_type = frozenset()

        if not bot_id:
            return by_type

        # bot_id 命中"该机器人 + 通用（空 bot_id）"
        target = str(bot_id).strip()
        by_bot = groups.get(("*", target), frozenset()) | groups.get(("*", ""), frozenset())
        return by_type & by_bot

    def _keyword_scores(
        self, key: str, query: str, top_k: int, min_score: float, candidates: frozenset
    ) -> List[Tuple[int, float]]:
        """倒排索引 + TF-IDF 余弦相似度，返回 [(位置, 分数), ...]"""
        with self._lock:
            index = self._index_cache.get(key)
        if not index:
            return [(i, 0.0) for i in sorted(candidates)[:top_k]]

        idf = index["idf"]
        inverted = index["inverted"]      # term -> {doc_idx: tfidf}
        doc_norms = index["doc_norms"]    # doc_idx -> L2 范数（预计算）

        query_terms = self._tokenize(query)
        if not query_terms:
            return [(i, 0.0) for i in sorted(candidates)[:top_k]]

        # 查询向量（只保留词表内的词）
        q_tf = Counter(query_terms)
        q_len = len(query_terms)
        query_vec = {}
        for term, cnt in q_tf.items():
            w = idf.get(term)
            if w:
                query_vec[term] = (cnt / q_len) * w
        if not query_vec:
            return [(i, 0.0) for i in sorted(candidates)[:top_k]]

        q_norm = math.sqrt(sum(v * v for v in query_vec.values()))
        if q_norm == 0:
            return [(i, 0.0) for i in sorted(candidates)[:top_k]]

        # 核心优化：只累加"命中查询词"的文档，跳过无关文档
        dot_products = defaultdict(float)
        for term, q_weight in query_vec.items():
            postings = inverted.get(term)
            if not postings:
                continue
            for doc_idx, d_weight in postings.items():
                if doc_idx in candidates:
                    dot_products[doc_idx] += q_weight * d_weight

        if not dot_products:
            return []

        scored = []
        for doc_idx, dot in dot_products.items():
            d_norm = doc_norms.get(doc_idx, 0.0)
            if d_norm == 0:
                continue
            score = dot / (q_norm * d_norm)
            if score >= min_score:
                scored.append((doc_idx, score))

        # O(n log k) 取 TopK，优于全排序
        return heapq.nlargest(top_k, scored, key=lambda x: x[1])

    def _keyword_search(
        self, key: str, query: str, top_k: int, min_score: float, candidates: frozenset
    ) -> List[str]:
        scored = self._keyword_scores(key, query, top_k, min_score, candidates)
        with self._lock:
            items = self._kb_cache.get(key, [])
        return [items[i]["text"] for i, _ in scored if i < len(items)]

    def _semantic_scores(
        self, key: str, query: str, top_k: int, min_score: float, candidates: frozenset
    ) -> List[Tuple[int, float]]:
        """语义向量检索；向量已预归一化，点积即余弦相似度。"""
        if self._encoder is None or key not in self._embedding_cache:
            return self._keyword_scores(key, query, top_k, min_score, candidates)

        import numpy as np

        with self._lock:
            embeddings = self._embedding_cache[key]["embeddings"]

        q_vec = self._encoder.encode([query])[0]
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        # 只对候选行做矩阵乘法（按位置索引切片，不做对象比较）
        idx_arr = np.fromiter(sorted(candidates), dtype=np.int64)
        idx_arr = idx_arr[idx_arr < embeddings.shape[0]]
        if idx_arr.size == 0:
            return []

        sims = embeddings[idx_arr] @ q_vec  # 已归一化 -> 点积 = 余弦

        k = min(top_k, idx_arr.size)
        # np.argpartition: O(n) 选出 TopK，再对 K 个排序
        part = np.argpartition(-sims, k - 1)[:k] if k < idx_arr.size else np.arange(idx_arr.size)
        part = part[np.argsort(-sims[part])]

        return [(int(idx_arr[p]), float(sims[p])) for p in part if sims[p] >= min_score]

    def _semantic_search(
        self, key: str, query: str, top_k: int, min_score: float, candidates: frozenset
    ) -> List[str]:
        scored = self._semantic_scores(key, query, top_k, min_score, candidates)
        with self._lock:
            items = self._kb_cache.get(key, [])
        return [items[i]["text"] for i, _ in scored if i < len(items)]

    # ==================== 索引构建 ====================

    def _build_keyword_index(self, key: str, items: List[dict]):
        """构建倒排索引 + 预计算文档范数（一次构建，多次检索复用）。

        索引结构：
            {
              "idf":       {term: idf_weight},
              "inverted":  {term: {doc_idx: tfidf_weight}},   # 倒排表
              "doc_norms": {doc_idx: L2_norm},                # 预计算范数
            }
        """
        tokenized = [self._tokenize(it.get("text", "")) for it in items]
        n_docs = len(tokenized)
        if n_docs == 0:
            self._index_cache[key] = {"idf": {}, "inverted": {}, "doc_norms": {}}
            return

        # 文档频次 -> IDF
        doc_freq = Counter()
        for tokens in tokenized:
            doc_freq.update(set(tokens))
        idf = {t: math.log(n_docs / (df + 1)) + 1.0 for t, df in doc_freq.items()}

        # 构建倒排表 + 累计范数平方
        inverted: Dict[str, Dict[int, float]] = defaultdict(dict)
        norm_sq: Dict[int, float] = defaultdict(float)

        for doc_idx, tokens in enumerate(tokenized):
            if not tokens:
                continue
            tf = Counter(tokens)
            total = len(tokens)
            for term, cnt in tf.items():
                w = (cnt / total) * idf.get(term, 0.0)
                if w == 0:
                    continue
                inverted[term][doc_idx] = w
                norm_sq[doc_idx] += w * w

        doc_norms = {i: math.sqrt(v) for i, v in norm_sq.items()}
        self._index_cache[key] = {
            "idf": idf,
            "inverted": dict(inverted),
            "doc_norms": doc_norms,
        }

    def _build_group_index(self, key: str, items: List[dict]):
        """按 (type, bot_id) 预建位置集合，使筛选从 O(n×m) 降为 O(1) 命中。

        分组键约定：
            ("*", "*")      -> 全部
            (type, "*")     -> 指定知识类型
            ("*", bot_id)   -> 指定机器人（含 "" 表示通用）
        """
        by_type: Dict[str, Set[int]] = defaultdict(set)
        by_bot: Dict[str, Set[int]] = defaultdict(set)
        all_idx: Set[int] = set()

        for i, it in enumerate(items):
            all_idx.add(i)
            by_type[it.get("type", "答疑")].add(i)
            by_bot["" if it.get("bot_id") is None else str(it.get("bot_id")).strip()].add(i)

        groups: Dict[tuple, frozenset] = {("*", "*"): frozenset(all_idx)}
        for t, s in by_type.items():
            groups[(t, "*")] = frozenset(s)
        for b, s in by_bot.items():
            groups[("*", b)] = frozenset(s)

        self._group_cache[key] = groups

    def _build_embedding_index(self, key: str, items: List[dict]):
        """构建语义向量索引，并预先 L2 归一化（检索时点积即余弦相似度）。"""
        if self._encoder is None:
            return
        try:
            import numpy as np
        except ImportError:
            logger.warning("numpy 未安装，语义检索不可用")
            self._use_semantic = False
            return

        texts = [it.get("text", "") for it in items]
        embeddings = np.asarray(self._encoder.encode(texts), dtype=np.float32)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._embedding_cache[key] = {"embeddings": embeddings / norms}

    # ==================== 工具方法 ====================

    def _tokenize(self, text: str) -> List[str]:
        """中文分词：单字(择优) + bigram + trigram + 英文单词 + 数字，并过滤停用词。

        相比旧版的改进：
        1. 过滤停用词与低区分度单字，减少约 30% 噪声 token
        2. bigram/trigram 也做停用词过滤，避免"的了""是在"这类无意义组合
        """
        if not text:
            return []

        chinese = re.findall(r"[\u4e00-\u9fff]+", text)
        english = [w.lower() for w in re.findall(r"[a-zA-Z]{2,}", text)]
        numbers = re.findall(r"\d+", text)

        tokens: List[str] = []
        use_stop = self._enable_stopwords

        for seg in chinese:
            n = len(seg)
            # 单字：仅保留高区分度字（症状类），其余交给 n-gram
            for ch in seg:
                if not use_stop or ch in _SINGLE_CHAR_KEEP:
                    tokens.append(ch)
            # bigram
            for i in range(n - 1):
                bg = seg[i:i + 2]
                if not use_stop or bg not in _STOPWORDS:
                    tokens.append(bg)
            # trigram
            for i in range(n - 2):
                tg = seg[i:i + 3]
                if not use_stop or tg not in _STOPWORDS:
                    tokens.append(tg)

        for w in english:
            if not use_stop or w not in _STOPWORDS:
                tokens.append(w)
        tokens.extend(numbers)

        return tokens

    @staticmethod
    def _extract_query(history: List[Dict[str, str]]) -> str:
        """从对话历史提取查询文本：最近 6 轮，用户消息优先，越近权重越高（重复拼接）。"""
        if not history:
            return ""
        recent = history[-6:]

        user_parts: List[str] = []
        all_parts: List[str] = []
        total = len(recent)
        for pos, msg in enumerate(recent):
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            all_parts.append(content)
            if msg.get("role") == "user":
                # 越靠后的用户消息重复 2 次，提升近期意图权重
                user_parts.append(content)
                if pos >= total - 2:
                    user_parts.append(content)

        return " ".join(user_parts) if user_parts else " ".join(all_parts)

    def _http_get(self, path: str) -> Optional[dict]:
        try:
            req = Request(f"{self.base_url}{path}")
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except (URLError, HTTPError, TimeoutError) as e:
            logger.warning(f"请求失败 {path}: {e}")
            return None

    def __repr__(self):
        with self._lock:
            n_kb = len(self._kb_cache)
            n_items = sum(len(v) for v in self._kb_cache.values())
        return (f"KnowledgeRetriever(cached_kb={n_kb}, items={n_items}, "
                f"semantic={'on' if self._use_semantic else 'off'})")
