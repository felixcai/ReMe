"""
test_embedding.py - 向量索引与检索的端到端测试脚本

演示流程：
  Step 1：生成文件1（主题A），全量索引（FullFileWatcher），记录 time_A
  Step 2：等待 20 秒，生成文件2（主题A）和文件3（主题B），全量索引，记录 time_B
  Step 3：等待 20 秒，追加内容到文件1（主题A），增量索引（DeltaFileWatcher），记录 time_C
  Step 4：Hybrid 检索（向量 + 关键词加权融合），分三个场景：
    4a：检索所有主题A（无时间过滤）—— 预期：file1首次 + file2 + file1追加
    4b：检索所有主题B（无时间过滤）—— 预期：只有 file3
    4c：检索 time_B 之后写入的主题A（updated_at 过滤）—— 预期：file2 + file1追加部分

核心设计：
  - 所有 async 接口通过 S() 同步调用，方便断点调试
  - FullFileWatcher / DeltaFileWatcher 直接调 _on_changes()，不启动后台监听 Task
  - hybrid_search_with_filter() 在脚本内实现（不改框架代码），额外支持 metadata where 过滤
  - updated_at 由 ChromaFileStore.upsert_file()/upsert_chunks() 写入时自动生成（毫秒时间戳）
  - 文件路径统一使用绝对路径，保证 FullFileWatcher / DeltaFileWatcher 存取一致
"""

import asyncio
import time
from pathlib import Path

from watchfiles import Change

from reme.core.enumeration import MemorySource
from reme.core.file_store.chroma_file_store import ChromaFileStore
from reme.core.file_watcher.delta_file_watcher import DeltaFileWatcher
from reme.core.file_watcher.full_file_watcher import FullFileWatcher
from reme.core.schema.memory_search_result import MemorySearchResult
from reme.reme_light import ReMeLight

# =============================================================================
# 同步执行辅助函数
# 所有 async 接口均通过 S() 同步调用，方便在任意行设断点观察结果
# =============================================================================
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def S(coro):
    """把 coroutine 同步跑完并返回结果，相当于 await。"""
    return _loop.run_until_complete(coro)


def section(title: str):
    """输出分隔线，方便区分各测试阶段。"""
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def print_results(results: list[MemorySearchResult], title: str):
    """格式化打印检索结果列表。"""
    print(f"\n  [{title}] 命中 {len(results)} 条 chunk：")
    if not results:
        print("    （无结果）")
        return
    for i, r in enumerate(results, 1):
        path_name = Path(r.path).name
        updated_at = r.metadata.get("updated_at", "N/A")
        print(f"    {i}. [{path_name}] 行{r.start_line}-{r.end_line}  "
              f"score={r.score:.4f}  updated_at={updated_at}")
        preview = r.snippet[:80].replace("\n", " ")
        print(f"       {preview}")


# =============================================================================
# 测试用 markdown 文件内容
# 主题A：Python 机器学习 / 深度学习 / NLP
# 主题B：React 前端组件开发
# =============================================================================

CONTENT_FILE1_INITIAL = """\
# Python 机器学习入门

Python 是机器学习领域最流行的编程语言，拥有丰富的生态系统。

## 常用库

- **NumPy**：多维数组与矩阵运算，是机器学习计算的基础
- **Pandas**：表格数据处理，支持 CSV、Excel、SQL 等多种格式
- **Scikit-learn**：提供监督学习、无监督学习、模型评估等完整工具链

## 典型工作流

1. 数据加载与预处理（缺失值、归一化）
2. 特征工程与特征选择
3. 模型训练（线性回归、决策树、随机森林等）
4. 交叉验证与超参数调优
5. 模型评估（准确率、F1、AUC）
"""

# 追加到文件1末尾，关于主题A的 NLP 部分
CONTENT_FILE1_APPEND = """\

## 自然语言处理（NLP）

Python 同样是 NLP 领域的核心语言：

- **NLTK / spaCy**：分词、词性标注、命名实体识别
- **词向量**：Word2Vec、GloVe 将单词映射为稠密向量
- **BERT**：基于 Transformer 的预训练模型，在 NLP 各任务上取得突破性效果
- **Hugging Face Transformers**：提供海量预训练模型，一行代码完成文本分类、摘要等任务
"""

CONTENT_FILE2 = """\
# Python 深度学习实践

深度学习是机器学习的子领域，通过多层神经网络自动提取特征。

## 主流框架

- **TensorFlow / Keras**：Google 开发，生产部署成熟，TensorFlow Lite 支持移动端
- **PyTorch**：Facebook 开发，动态计算图，研究领域首选，调试直观

## 神经网络基础

卷积神经网络（CNN）擅长图像识别；循环神经网络（RNN）/ LSTM 处理序列数据；
Transformer 架构通过自注意力机制彻底改变了 NLP 和 CV 领域。

## 训练技巧

- 批归一化（Batch Norm）稳定训练过程
- Dropout 防止过拟合
- 学习率调度（Cosine Annealing、ReduceLROnPlateau）
- 混合精度训练（FP16）加速 GPU 训练，节省显存
"""

CONTENT_FILE3 = """\
# React 前端组件开发

React 是 Facebook 开源的 JavaScript 组件化 UI 框架，采用声明式编程风格。

## 核心概念

- **JSX**：在 JavaScript 中直接写 HTML 结构，经 Babel 编译为 React.createElement()
- **组件**：函数组件是主流写法，接收 props，返回 JSX
- **状态管理**：useState 管理本地状态，useReducer 处理复杂状态逻辑

## Hooks 常用 API

- `useState`：声明响应式状态变量
- `useEffect`：处理副作用（数据请求、订阅、DOM 操作）
- `useContext`：跨组件层级共享数据，避免 props drilling
- `useMemo` / `useCallback`：性能优化，避免不必要的重渲染

## 状态管理方案

中大型项目推荐 Redux Toolkit 或 Zustand；小型项目 Context API 已足够。
"""


# =============================================================================
# Hybrid Search with Metadata Filter（内联实现，不改框架代码）
#
# 逻辑与 ChromaFileStore.hybrid_search() 完全对齐：
#   向量检索（collection.query）+ 关键词检索（collection.get + where_document）
#   → 加权融合（vector_weight + text_weight = 1.0）
#
# 相比原版新增 filters 参数，支持 metadata where 过滤（如 updated_at 时间过滤）
# =============================================================================

def _build_where(
    sources: list[MemorySource] | None = None,
    filters: dict | None = None,
) -> dict | None:
    """
    把 sources 和自定义 filters 合并成 Chroma where 子句。

    如果两者都存在，用 $and 合并；只有一个时直接返回。

    示例：
      _build_where([MemorySource.MEMORY], {"updated_at": {"$gte": 1234}})
      → {"$and": [{"source": "memory"}, {"updated_at": {"$gte": 1234}}]}
    """
    clauses = []

    if sources:
        if len(sources) == 1:
            clauses.append({"source": sources[0].value})
        else:
            clauses.append({"source": {"$in": [s.value for s in sources]}})

    if filters:
        clauses.append(filters)

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _parse_get_results(raw: dict, query: str) -> list[MemorySearchResult]:
    """
    把 collection.get() 的原始结果解析为 MemorySearchResult 列表（含相关性评分）。
    评分逻辑与 ChromaFileStore.keyword_search() 完全一致：
      score = match_count/n_words + phrase_bonus（最高 1.0）
    """
    results = []
    ids = raw.get("ids") or []
    if not ids:
        return results

    documents = raw.get("documents") or []
    metadatas = raw.get("metadatas") or []

    words = query.split()
    words_lower = [w.lower() for w in words]
    n_words = max(1, len(words))
    query_lower = query.lower()

    for i, _ in enumerate(ids):
        meta = metadatas[i] if i < len(metadatas) else {}
        text = documents[i] if i < len(documents) else ""
        text_lower = text.lower()

        match_count = sum(1 for w in words_lower if w in text_lower)
        base_score = match_count / n_words
        phrase_bonus = 0.2 if n_words > 1 and query_lower in text_lower else 0.0
        score = min(1.0, base_score + phrase_bonus)

        results.append(MemorySearchResult(
            path=meta.get("path", ""),
            start_line=meta.get("start_line", 0),
            end_line=meta.get("end_line", 0),
            score=score,
            snippet=text,
            source=MemorySource(meta.get("source", MemorySource.MEMORY.value)),
            # 把 updated_at 存入 metadata 字段，方便打印时展示
            metadata={"updated_at": meta.get("updated_at")},
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _parse_query_results(raw: dict) -> list[MemorySearchResult]:
    """
    把 collection.query() 的原始结果解析为 MemorySearchResult 列表（含相似度评分）。
    评分逻辑与 ChromaFileStore.vector_search() 完全一致：
      cosine distance [0, 2] → score = max(0.0, 1.0 - distance / 2.0)
    """
    results = []
    ids = raw.get("ids") or []
    if not ids or not ids[0]:
        return results

    documents = raw.get("documents") or [[]]
    metadatas = raw.get("metadatas") or [[]]
    distances = raw.get("distances") or [[]]

    for i, _ in enumerate(ids[0]):
        meta = metadatas[0][i] if metadatas[0] else {}
        text = documents[0][i] if documents[0] else ""
        distance = distances[0][i] if distances[0] else 1.0

        # 余弦距离 [0, 2] 转为相似度分数 [1, 0]
        score = max(0.0, 1.0 - distance / 2.0)

        results.append(MemorySearchResult(
            path=meta.get("path", ""),
            start_line=meta.get("start_line", 0),
            end_line=meta.get("end_line", 0),
            score=score,
            snippet=text,
            source=MemorySource(meta.get("source", MemorySource.MEMORY.value)),
            raw_metric=distance,
            metadata={"updated_at": meta.get("updated_at")},
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _merge_results(
    vector_results: list[MemorySearchResult],
    keyword_results: list[MemorySearchResult],
    vector_weight: float,
    limit: int,
) -> list[MemorySearchResult]:
    """
    加权合并向量检索和关键词检索结果。
    逻辑与 ChromaFileStore._merge_hybrid_results() 完全一致：
      同一 chunk（相同 path:start_line:end_line）的得分相加。
    """
    text_weight = 1.0 - vector_weight
    merged: dict[str, MemorySearchResult] = {}

    for r in vector_results:
        r.score = r.score * vector_weight
        merged[r.merge_key] = r

    for r in keyword_results:
        key = r.merge_key
        if key in merged:
            # 同一 chunk 同时命中向量检索和关键词检索，得分叠加
            merged[key].score += r.score * text_weight
        else:
            r.score = r.score * text_weight
            merged[key] = r

    results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
    return results[:limit]


async def hybrid_search_with_filter(
    file_store: ChromaFileStore,
    query: str,
    limit: int,
    sources: list[MemorySource] | None = None,
    filters: dict | None = None,
    vector_weight: float = 0.7,
    candidate_multiplier: float = 3.0,
) -> list[MemorySearchResult]:
    """
    带 metadata 过滤的 Hybrid 检索（向量 + 关键词加权融合）。

    和 ChromaFileStore.hybrid_search() 参数完全对齐，仅增加 filters 参数。
    filters 会和 sources 合并为 Chroma where 子句，支持 updated_at 等字段过滤。

    Args:
        file_store:           ChromaFileStore 实例
        query:                检索查询文本
        limit:                返回结果数量上限
        sources:              来源过滤（同原版 hybrid_search）
        filters:              额外 metadata 过滤，如 {"updated_at": {"$gte": time_B}}
        vector_weight:        向量检索权重（0.0-1.0），关键词权重 = 1 - vector_weight
        candidate_multiplier: 候选池倍数，实际候选数 = limit * multiplier

    示例：
        # 无时间过滤
        results = await hybrid_search_with_filter(fs, "Python 机器学习", limit=10)

        # 只查 time_B 之后写入的 chunk
        results = await hybrid_search_with_filter(
            fs, "Python 机器学习", limit=10,
            filters={"updated_at": {"$gte": time_B}},
        )
    """
    assert 0.0 <= vector_weight <= 1.0, f"vector_weight 必须在 [0, 1]，当前值：{vector_weight}"

    collection = file_store.chunks_collection
    # 候选数量上限 200，防止 Chroma 对过大 n_results 报错
    candidates = min(200, max(1, int(limit * candidate_multiplier)))

    # 合并 sources 和 filters 为统一的 Chroma where 子句
    where = _build_where(sources=sources, filters=filters)

    # ------------------------------------------------------------------
    # 关键词检索（全文匹配）：collection.get + where_document
    # ------------------------------------------------------------------
    keyword_results: list[MemorySearchResult] = []

    if file_store.fts_enabled and query:
        words = query.split()
        if words:
            # 为每个词生成大小写变体，提升召回率（Chroma $contains 大小写敏感）
            word_variants: set[str] = set()
            for word in words:
                word_variants.update([word, word.lower(), word.capitalize(), word.upper()])
            variant_list = list(word_variants)

            if len(variant_list) == 1:
                where_document: dict = {"$contains": variant_list[0]}
            else:
                where_document = {"$or": [{"$contains": w} for w in variant_list]}

            try:
                kw_raw = collection.get(
                    where=where,
                    where_document=where_document,
                    include=["documents", "metadatas"],
                )
                keyword_results = _parse_get_results(kw_raw, query)[:candidates]
            except Exception as e:
                print(f"  [警告] keyword_search 失败：{e}")

    # ------------------------------------------------------------------
    # 向量检索（语义相似度）：get_embedding + collection.query
    # ------------------------------------------------------------------
    vector_results: list[MemorySearchResult] = []

    if file_store.vector_enabled and query:
        query_embedding = await file_store.get_embedding(query)
        if query_embedding:
            try:
                vec_raw = collection.query(
                    query_embeddings=[query_embedding],
                    n_results=candidates,
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
                vector_results = _parse_query_results(vec_raw)
            except Exception as e:
                # 常见于 n_results > 当前 collection 中满足 where 条件的文档数量
                print(f"  [警告] vector_search 失败（可能是结果数量不足 candidates={candidates}）：{e}")

    # ------------------------------------------------------------------
    # 合并两路结果
    # ------------------------------------------------------------------
    if not keyword_results and not vector_results:
        return []
    if not keyword_results:
        return vector_results[:limit]
    if not vector_results:
        return keyword_results[:limit]

    return _merge_results(vector_results, keyword_results, vector_weight, limit)


# =============================================================================
# 主测试流程
# =============================================================================

def main():
    # =========================================================================
    # 初始化 ReMeLight，获取共享的 file_store（ChromaFileStore 实例）
    # =========================================================================
    section("初始化 ReMeLight")

    reme = ReMeLight(
        working_dir="felix_test/.reme_embedding_test",
        llm_api_key="sk-3fe61157d23345779df499667e5263fa",
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="sk-29a937c25bff4752af88514525c329b3",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_as_llm_config={"model_name": "qwen3-235b-a22b"},
        default_embedding_model_config={"model_name": "text-embedding-v4"},
        default_file_store_config={
            "backend": "chroma",
            "store_name": "reme",
            "fts_enabled": True,
            "vector_enabled": True,
        },
        # rebuild_index_on_start=False：防止默认 watcher 的后台 Task
        # 在测试期间异步触发 clear_all()，干扰我们手动控制的索引流程
        default_file_watcher_config={"rebuild_index_on_start": False},
        enable_load_env=True,
    )
    S(reme.start())
    print("  ReMeLight 已启动")

    # 获取共享的 ChromaFileStore 实例
    file_store: ChromaFileStore = reme.default_file_store
    print(f"  file_store 类型：{type(file_store).__name__}")
    print(f"  collection 名称：{file_store.collection_name}")

    # 手动清空索引，保证每次测试从干净状态开始
    S(file_store.clear_all())
    print("  索引已清空（fresh start）")

    # =========================================================================
    # 创建测试文件目录，初始化两个 watcher 实例（注入同一 file_store）
    # =========================================================================
    test_dir = Path("felix_test/.test_embedding_data")
    test_dir.mkdir(parents=True, exist_ok=True)

    # FullFileWatcher：全量同步策略，文件有变化就整份重建索引
    # DeltaFileWatcher：增量同步策略，对 append-only 文件只索引新增尾部
    # rebuild_index_on_start=False：不启动后台监听 Task，手动控制索引触发时机
    full_watcher = FullFileWatcher(
        watch_paths=[str(test_dir)],
        file_store=file_store,
        rebuild_index_on_start=False,
    )
    delta_watcher = DeltaFileWatcher(
        watch_paths=[str(test_dir)],
        file_store=file_store,
        rebuild_index_on_start=False,
    )

    # =========================================================================
    # Step 1：生成文件1（主题A），全量索引，记录 time_A
    # =========================================================================
    section("Step 1: 文件1（主题A - Python机器学习入门）全量索引")

    file1 = test_dir / "topic_a_1.md"
    file1.write_text(CONTENT_FILE1_INITIAL, encoding="utf-8")
    print(f"  已写入：{file1.name}（{len(CONTENT_FILE1_INITIAL)} 字符）")

    # 注意：使用 resolve() 确保路径是绝对路径
    # FullFileWatcher._build_file_metadata() 内部也会转绝对路径，保持一致
    # DeltaFileWatcher 后续需要按同一路径在 file_store 中查找旧索引数据
    file1_abs = str(file1.resolve())

    # 手动触发全量索引，相当于 watcher 收到 Change.added 事件
    # 内部流程：读文件 → chunk_markdown → get_chunk_embeddings → delete_file → upsert_file
    S(full_watcher._on_changes({(Change.added, file1_abs)}))
    print(f"  全量索引完成：{file1.name}")

    # 记录 time_A（索引完成之后），file1 所有 chunks 的 updated_at 都 < time_A
    time_A = int(time.time() * 1000)
    print(f"  time_A = {time_A}（索引完成后记录）")

    # =========================================================================
    # Step 2：等待 20 秒，生成文件2（主题A）和文件3（主题B），全量索引，记录 time_B
    # =========================================================================
    section("Step 2: 等待 20 秒，文件2（主题A）+ 文件3（主题B）全量索引")

    print("  等待 20 秒（拉开时间戳差距，确保 time_B 过滤有效）...")
    time.sleep(20)

    file2 = test_dir / "topic_a_2.md"
    file3 = test_dir / "topic_b_1.md"
    file2.write_text(CONTENT_FILE2, encoding="utf-8")
    file3.write_text(CONTENT_FILE3, encoding="utf-8")
    print(f"  已写入：{file2.name}（{len(CONTENT_FILE2)} 字符）")
    print(f"  已写入：{file3.name}（{len(CONTENT_FILE3)} 字符）")

    file2_abs = str(file2.resolve())
    file3_abs = str(file3.resolve())

    # 在触发索引之前记录 time_B
    # 这样 file2 和 file3 的所有 chunks 的 updated_at >= time_B
    time_B = int(time.time() * 1000)
    print(f"  time_B = {time_B}（在触发索引之前记录，用于后续 updated_at 过滤）")

    S(full_watcher._on_changes({
        (Change.added, file2_abs),
        (Change.added, file3_abs),
    }))
    print(f"  全量索引完成：{file2.name}, {file3.name}")

    # =========================================================================
    # Step 3：等待 20 秒，追加内容到文件1（主题A NLP），增量索引，记录 time_C
    # =========================================================================
    section("Step 3: 等待 20 秒，文件1 追加 NLP 内容，增量索引")

    print("  等待 20 秒...")
    time.sleep(20)

    # 追加 NLP 相关内容到文件1末尾
    with open(file1, "a", encoding="utf-8") as f:
        f.write(CONTENT_FILE1_APPEND)
    print(f"  已追加内容到：{file1.name}（追加 {len(CONTENT_FILE1_APPEND)} 字符）")

    # 在触发索引之前记录 time_C
    time_C = int(time.time() * 1000)
    print(f"  time_C = {time_C}")

    # DeltaFileWatcher 会自动检测 append-only：
    #   1. 获取 file1 旧 chunks 和旧 file metadata
    #   2. 判断文件是否纯追加（size 增大且首 chunk 内容未变）
    #   3. 如果是 → 只索引 cutoff 之后的新内容（delete 受影响旧 chunks + upsert 新 chunks）
    #   4. 如果不是 → 退回全量重建（delete_file + upsert_file）
    S(delta_watcher._on_changes({(Change.modified, file1_abs)}))
    print(f"  增量索引完成：{file1.name}")

    # 打印当前索引状态，便于确认
    print("\n  当前索引状态：")
    all_indexed = S(file_store.list_files(MemorySource.MEMORY))
    for f_path in all_indexed:
        chunks = S(file_store.get_file_chunks(f_path, MemorySource.MEMORY))
        print(f"    - {Path(f_path).name}：{len(chunks)} 个 chunk")

    # =========================================================================
    # Step 4：Hybrid 检索（向量 + 关键词），三个场景
    # =========================================================================
    section("Step 4: Hybrid 检索")

    default_sources = [MemorySource.MEMORY]

    print(f"\n  参考时间戳：")
    print(f"    time_A = {time_A}  （file1 首次索引完成后）")
    print(f"    time_B = {time_B}  （file2/file3 索引触发前）")
    print(f"    time_C = {time_C}  （file1 追加内容索引触发前）")

    # ------------------------------------------------------------------
    # 场景 4a：检索所有主题A（无时间过滤）
    # 预期：来自 file1（首次，Python基础/Scikit-learn）
    #           + file2（深度学习/TensorFlow/PyTorch）
    #           + file1追加（NLP/BERT/词向量）
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("  [场景 4a] 检索所有主题A，无时间过滤")
    print("  query = 'Python 机器学习'")
    print("  预期：file1_初始 + file2 + file1_追加（NLP部分）")

    results_a = S(hybrid_search_with_filter(
        file_store,
        query="Python 机器学习",
        limit=10,
        sources=default_sources,
    ))
    print_results(results_a, "场景4a - 所有主题A")

    # ------------------------------------------------------------------
    # 场景 4b：检索所有主题B（无时间过滤）
    # 预期：只来自 file3（React 前端组件 / Hooks / 状态管理）
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("  [场景 4b] 检索所有主题B，无时间过滤")
    print("  query = 'React 组件'")
    print("  预期：只来自 file3")

    results_b = S(hybrid_search_with_filter(
        file_store,
        query="React 组件",
        limit=10,
        sources=default_sources,
    ))
    print_results(results_b, "场景4b - 所有主题B")

    # ------------------------------------------------------------------
    # 场景 4c：检索 time_B 之后写入的主题A（含 updated_at 过滤）
    # filters = {"updated_at": {"$gte": time_B}}
    # 预期：file2（time_B 之后全量索引）+ file1 追加的 NLP chunks（time_C 之后索引）
    # 不应包含：file1 首次写入的 chunks（那些 chunks 的 updated_at < time_A < time_B）
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("  [场景 4c] 检索 time_B 之后写入的主题A（updated_at 过滤）")
    print(f"  query = 'Python 机器学习'，filters = {{updated_at >= {time_B}}}")
    print("  预期：file2（全部）+ file1_追加（NLP部分），不含 file1 首次写入的 chunks")

    results_c = S(hybrid_search_with_filter(
        file_store,
        query="Python 机器学习",
        limit=10,
        sources=default_sources,
        filters={"updated_at": {"$gte": time_B}},
    ))
    print_results(results_c, "场景4c - time_B之后的主题A")

    # =========================================================================
    # 关闭
    # =========================================================================
    section("关闭 ReMeLight")
    S(reme.close())
    print("  ReMeLight 已关闭")


if __name__ == "__main__":
    main()
