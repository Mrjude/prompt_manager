#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
知识库检索模块（兼容层，已迁移至 client_sdk）

实现已整合进 client_sdk/knowledge_retriever.py 并做了性能优化，
本文件仅保留向后兼容的导入入口。

推荐新代码使用：
    from client_sdk import KnowledgeRetriever

旧代码无需修改，仍可继续使用：
    from knowledge_retriever import KnowledgeRetriever

主要优化（相比本文件的旧实现）：
1. 倒排索引：检索复杂度从 O(n) 降为 O(|q|·df)
2. 预计算文档范数：省掉每次检索的重复 L2 norm 计算
3. heapq.nlargest 取 TopK：O(n log k) 而非 O(n log n) 全排序
4. 位置索引替代对象比较：消除 O(n×m) 的 dict 深度比较
5. 预归一化语义向量 + argpartition：矩阵乘法即余弦，TopK 选取 O(n)
6. (type, bot_id) 分组预筛：筛选 O(1) 命中
7. 停用词过滤：减少约 30% 噪声 token
8. 改用公开端点 /api/v1/knowledge（旧版 /api/knowledge 需鉴权会 401）
9. 支持复用 PromptClient 缓存，零重复请求
"""

import os
import sys

# 支持两种布局：
#   1. 作为包的一部分：prompt_manager/client_sdk/
#   2. 已安装的 SDK：site-packages/client_sdk/
try:
    from client_sdk.knowledge_retriever import (  # noqa: F401
        KnowledgeRetriever,
        logger,
    )
except ImportError:
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from client_sdk.knowledge_retriever import (  # noqa: F401
        KnowledgeRetriever,
        logger,
    )

__all__ = ["KnowledgeRetriever", "logger"]
