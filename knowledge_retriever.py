#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识库检索模块

根据对话历史，检索与当前对话相关的知识库条目。
支持基于关键词匹配和语义相似度的检索方式。

使用示例：
    from knowledge_retriever import KnowledgeRetriever

    retriever = KnowledgeRetriever(base_url="http://localhost:8900")

    # 预加载知识库
    retriever.preload(department="hair", platform="xhs")

    # 根据对话历史检索
    history = [
        {"role": "user", "content": "最近掉头发很严重怎么办"},
        {"role": "assistant", "content": "请问您掉发持续多长时间了？"}
    ]
    results = retriever.retrieve(history, department="hair", platform="xhs", top_k=5)
    # results -> ["雄激素性脱发表现为...", "米诺地尔是常用药物...", ...]

    # 也可获取拼接好的文本直接插入 prompt
    knowledge_text = retriever.retrieve_as_text(
        history, department="hair", platform="xhs",
        top_k=5, separator="\\n"
    )
"""

import json
import math
import re
import logging
from typing import Optional, Dict, List, Tuple
from collections import Counter
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logger = logging.getLogger("knowledge_retriever")


class KnowledgeRetriever:
    """
    知识库检索器

    根据对话历史从知识库中检索相关条目，支持：
    1. 关键词匹配（TF-IDF 加权）
    2. 可选的语义相似度检索（需安装 sentence-transformers）
    3. 本地缓存，零延迟读取
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8900",
        use_semantic: bool = False,
        semantic_model: str = "shibing624/text2vec-base-chinese",
    ):
        """
        Args:
            base_url: 提示词管理系统服务地址
            use_semantic: 是否启用语义检索（需要安装 sentence-transformers）
            semantic_model: 语义编码模型名称
        """
        self.base_url = base_url.rstrip("/")
        self._kb_cache: Dict[str, List[str]] = {}  # key: "dept/plat" -> 条目列表
        self._tfidf_cache: Dict[str, Dict[str, Dict[str, float]]] = {}  # TF-IDF 索引
        self._embedding_cache: Dict[str, object] = {}  # 语义向量缓存
        self._encoder = None
        self._use_semantic = use_semantic
        self._semantic_model = semantic_model

        if use_semantic:
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
        预加载知识库到本地缓存

        Args:
            department: 科室代码，为空则加载全部
            platform: 平台代码，为空则加载对应科室的全部
        """
        params = []
        if department:
            params.append(f"department={department}")
        if platform:
            params.append(f"platform={platform}")
        query = "&".join(params)
        path = f"/api/knowledge?{query}" if query else "/api/knowledge"

        result = self._http_get(path)
        if not result:
            return

        for item in result.get("items", []):
            dept = item.get("department", "")
            plat = item.get("platform", "")
            if not dept or not plat:
                continue
            key = f"{dept}/{plat}"
            try:
                raw = json.loads(item.get("content", "[]"))
                if not isinstance(raw, list):
                    raw = []
                # 兼容旧格式（字符串数组） -> 转为对象数组
                items_list = []
                for entry in raw:
                    if isinstance(entry, str):
                        items_list.append({"text": entry, "type": "答疑", "bot_id": "9378"})
                    elif isinstance(entry, dict):
                        items_list.append(entry)
            except (json.JSONDecodeError, TypeError):
                items_list = []

            self._kb_cache[key] = items_list
            self._build_tfidf_index(key, items_list)
            if self._use_semantic and items_list:
                self._build_embedding_index(key, items_list)

            logger.info(f"知识库 [{key}] 已加载，共 {len(items_list)} 条")

    def get_available_keys(self) -> List[str]:
        """获取当前缓存中所有知识库的 key（科室/平台）"""
        return list(self._kb_cache.keys())

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
            method: 检索方法 "keyword"(关键词), "semantic"(语义), "auto"(自动选择)
            knowledge_type: 知识类型筛选，如 "答疑"、"问诊"等，为空不筛选
            bot_id: 机器人ID筛选，如 "9378"，为空不筛选

        Returns:
            相关知识条目列表，按相关度降序排列
        """
        key = f"{department}/{platform}"

        # 确保缓存中有数据
        if key not in self._kb_cache:
            self.preload(department=department, platform=platform)

        if key not in self._kb_cache or not self._kb_cache[key]:
            logger.warning(f"知识库 [{key}] 为空或不存在")
            return []

        # 按知识类型和机器人ID筛选
        search_items = self._kb_cache[key]
        if knowledge_type:
            search_items = [item for item in search_items if item.get("type", "答疑") == knowledge_type]
        if bot_id:
            target_bot_id = str(bot_id).strip()
            search_items = [
                item for item in search_items
                if not isinstance(item, dict)
                or str(item.get("bot_id") or "").strip() in ("", target_bot_id)
            ]
        if not search_items:
            return []

        # 从对话历史中提取查询文本
        query_text = self._extract_query(conversation_history)
        if not query_text.strip():
            # 无有效查询文本，返回前 top_k 条
            return [item.get("text", item) if isinstance(item, dict) else item for item in search_items[:top_k]]

        # 选择检索方法
        actual_method = method
        if method == "auto":
            actual_method = "semantic" if self._use_semantic else "keyword"

        if actual_method == "semantic" and self._use_semantic:
            results = self._semantic_search(key, query_text, top_k, min_score, search_items=search_items)
        else:
            results = self._keyword_search(key, query_text, top_k, min_score, search_items=search_items)

        return results

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
        """
        检索知识库并合并为单个文本字符串，便于直接插入 prompt

        Args:
            conversation_history: 对话历史
            department: 科室代码
            platform: 平台代码
            top_k: 返回条数
            min_score: 最低相关度
            separator: 条目间分隔符
            method: 检索方法
            knowledge_type: 知识类型筛选
            bot_id: 机器人ID筛选

        Returns:
            合并后的知识库文本
        """
        items = self.retrieve(
            conversation_history, department, platform,
            top_k=top_k, min_score=min_score, method=method,
            knowledge_type=knowledge_type, bot_id=bot_id
        )
        return separator.join(items) if items else ""

    # ==================== 检索实现 ====================

    def _keyword_search(
        self, key: str, query: str, top_k: int, min_score: float, search_items: list = None
    ) -> List[str]:
        """基于 TF-IDF 的关键词检索"""
        if key not in self._tfidf_cache:
            items = search_items or self._kb_cache[key]
            return [item.get("text", item) if isinstance(item, dict) else item for item in items[:top_k]]

        index = self._tfidf_cache[key]
        query_terms = self._tokenize(query)
        if not query_terms:
            items = search_items or self._kb_cache[key]
            return [item.get("text", item) if isinstance(item, dict) else item for item in items[:top_k]]

        # 计算查询向量
        query_tf = Counter(query_terms)
        query_vec = {}
        for term, count in query_tf.items():
            if term in index.get("__idf__", {}):
                query_vec[term] = (count / len(query_terms)) * index["__idf__"].get(term, 0)

        # 计算与每个条目的余弦相似度
        scores = []
        all_items = self._kb_cache[key]
        for i, item in enumerate(all_items):
            if search_items and item not in search_items:
                continue
            item_vec = index.get(str(i), {})
            score = self._cosine_similarity(query_vec, item_vec)
            if score >= min_score:
                scores.append((i, score))

        # 按分数降序排列
        scores.sort(key=lambda x: x[1], reverse=True)
        return [all_items[i].get("text", all_items[i]) if isinstance(all_items[i], dict) else all_items[i] for i, _ in scores[:top_k]]

    def _semantic_search(
        self, key: str, query: str, top_k: int, min_score: float, search_items: list = None
    ) -> List[str]:
        """基于语义向量的检索"""
        if self._encoder is None or key not in self._embedding_cache:
            return self._keyword_search(key, query, top_k, min_score, search_items=search_items)

        import numpy as np

        query_embedding = self._encoder.encode([query])[0]
        stored = self._embedding_cache[key]
        embeddings = stored["embeddings"]
        all_items = self._kb_cache[key]
        target_items = search_items or all_items

        # 构建 target_items 的索引映射
        if search_items:
            idx_map = {id(item): i for i, item in enumerate(all_items)}
            target_indices = [idx_map[id(item)] for item in target_items if id(item) in idx_map]
            target_embeddings = embeddings[target_indices]
            target_list = target_items
        else:
            target_embeddings = embeddings
            target_list = all_items

        # 余弦相似度
        similarities = np.dot(target_embeddings, query_embedding) / (
            np.linalg.norm(target_embeddings, axis=1) * np.linalg.norm(query_embedding) + 1e-8
        )

        # 排序
        top_indices = similarities.argsort()[::-1][:top_k]
        results = []
        for idx in top_indices:
            if similarities[idx] >= min_score:
                item = target_list[idx]
                results.append(item.get("text", item) if isinstance(item, dict) else item)
        return results

    # ==================== 索引构建 ====================

    def _build_tfidf_index(self, key: str, items: List):
        """构建 TF-IDF 索引"""
        # 分词（从对象中提取 text 字段）
        def _get_text(item):
            return item.get("text", "") if isinstance(item, dict) else str(item)

        tokenized_items = [self._tokenize(_get_text(item)) for item in items]
        n_docs = len(tokenized_items)

        # 计算 IDF
        doc_freq = Counter()
        for tokens in tokenized_items:
            for term in set(tokens):
                doc_freq[term] += 1

        idf = {term: math.log(n_docs / (df + 1)) + 1 for term, df in doc_freq.items()}

        # 计算 TF-IDF 向量
        index = {"__idf__": idf}
        for i, tokens in enumerate(tokenized_items):
            if not tokens:
                index[str(i)] = {}
                continue
            tf = Counter(tokens)
            total = len(tokens)
            tfidf_vec = {term: (count / total) * idf.get(term, 0) for term, count in tf.items()}
            index[str(i)] = tfidf_vec

        self._tfidf_cache[key] = index

    def _build_embedding_index(self, key: str, items: List):
        """构建语义向量索引"""
        if self._encoder is None:
            return

        import numpy as np
        # 从对象中提取 text 字段用于编码
        texts = [item.get("text", "") if isinstance(item, dict) else str(item) for item in items]
        embeddings = self._encoder.encode(texts)
        self._embedding_cache[key] = {
            "embeddings": np.array(embeddings),
        }

    # ==================== 工具方法 ====================

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """
        中文文本分词：按字符 bigram + 单字 + 英文单词
        简单实现，无需 jieba 等分词库
        """
        # 提取中文字符
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        # 提取英文单词
        english_words = re.findall(r'[a-zA-Z]+', text)
        # 提取数字
        numbers = re.findall(r'\d+', text)

        tokens = list(chinese_chars) + english_words + numbers

        # 添加 bigram（相邻两个字组合）
        bigrams = [chinese_chars[i] + chinese_chars[i + 1]
                   for i in range(len(chinese_chars) - 1)]
        tokens.extend(bigrams)

        # 添加 trigram
        trigrams = [chinese_chars[i] + chinese_chars[i + 1] + chinese_chars[i + 2]
                    for i in range(len(chinese_chars) - 2)]
        tokens.extend(trigrams)

        return [t.lower() for t in tokens if len(t) > 0]

    @staticmethod
    def _extract_query(history: List[Dict[str, str]]) -> str:
        """从对话历史中提取查询文本（优先使用最近的用户消息）"""
        # 取最近的几轮对话
        recent = history[-6:] if len(history) > 6 else history
        # 用户消息权重更高
        user_parts = []
        all_parts = []
        for msg in recent:
            content = msg.get("content", "").strip()
            if not content:
                continue
            all_parts.append(content)
            if msg.get("role") == "user":
                user_parts.append(content)

        # 优先用用户消息，否则用全部
        return " ".join(user_parts) if user_parts else " ".join(all_parts)

    @staticmethod
    def _cosine_similarity(vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        """计算两个稀疏向量的余弦相似度"""
        common_keys = set(vec_a.keys()) & set(vec_b.keys())
        if not common_keys:
            return 0.0

        dot = sum(vec_a[k] * vec_b[k] for k in common_keys)
        norm_a = math.sqrt(sum(v ** 2 for v in vec_a.values()))
        norm_b = math.sqrt(sum(v ** 2 for v in vec_b.values()))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def _http_get(self, path: str) -> Optional[dict]:
        try:
            req = Request(f"{self.base_url}{path}")
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except (URLError, HTTPError, TimeoutError) as e:
            logger.warning(f"请求失败 {path}: {e}")
            return None

    def __repr__(self):
        return (f"KnowledgeRetriever(cached_kb={len(self._kb_cache)}, "
                f"semantic={'on' if self._use_semantic else 'off'})")
