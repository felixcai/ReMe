# memory 文件的生成和使用逻辑

## 文件位置

`{working_dir}/memory/YYYY-MM-DD.md`（`working_dir` 默认为 `.reme`）

---

## 一、生成逻辑

### 触发时机

在 `pre_reasoning_hook` 中，当检测到对话上下文 token 超过阈值时：

1. `check_context()` 把消息分成 `messages_to_compact`（需压缩的旧消息）和 `messages_to_keep`（保留的新消息）
2. 对 `messages_to_compact` 同时启动两条并行处理：
   - **`compact_memory()`** → 生成精简摘要字符串，追加到 `compressed_summary`，继续参与后续 LLM 上下文（in-memory）
   - **`add_async_summary_task()`** → 后台异步调用 `summary_memory()`，**写入磁盘**

### 写入过程（`summary_memory` → `Summarizer`）

`Summarizer.execute()` 的核心步骤：

1. 把待压缩的 `messages` 格式化为字符串 `history_formatted_str`
2. 构造 prompt，内容为（见 `summarizer.yaml`）：
   - 告知 LLM 当前日期、`working_dir`、`memory_dir`
   - 要求 LLM 从会话中提取两类内容：
     - **持久化记忆**：事实、用户信息、项目状态、重要事件
     - **经验反思**：可复用的思维逻辑、成功策略、错误教训
   - 要求写入路径：`{memory_dir}/YYYY-MM-DD.md`
3. 用 `ReActAgent`（配备 `read_file` / `write_file` / `edit_file` 工具）执行这个 prompt
4. Agent 自主决策：
   - 先 `read` 当日文件，若不存在则直接 `write`
   - 若已存在，用 `edit` 智能合并，避免重复；仅在大幅重构时才 `write` 覆盖

### 文件命名规则

- 文件名 = `YYYY-MM-DD.md`，与 `summary_memory()` 调用时的**系统当前日期**对齐
- 同一天多次触发压缩，会合并追加进同一文件

---

## 二、使用逻辑（读取 / 检索）

### 向量检索（`memory_search`）

`reme_light.memory_search(query)` 调用 `MemorySearch` 工具：

1. 调用 `file_store.hybrid_search()`，搜索范围 = `MEMORY.md` + `memory/*.md`
2. 采用**混合搜索**：向量搜索（权重 0.7）+ 全文搜索（权重 0.3），可配置
3. 按 `min_score` 过滤后，返回带文件路径和行号的 top-N 片段

### 文件监听（自动索引）

`__init__` 中配置了 `file_watcher`，默认监听路径：

```python
_default_watch_paths = [str(_memory_md), str(self.memory_path)]
# 即：{working_dir}/MEMORY.md  +  {working_dir}/memory/（目录）
```

每当 `memory/*.md` 有文件变更，file_watcher 自动触发重新索引，使新写入的记忆立即可被检索。

---

## 三、整体数据流

```
对话消息过长
    │
    ▼
pre_reasoning_hook
    ├──► compact_memory()         → 生成压缩摘要字符串（in-memory，送入下轮 LLM）
    └──► add_async_summary_task() → 后台异步执行
              │
              ▼
         summary_memory() → Summarizer
              │  用 ReActAgent + file tools
              ▼
         memory/YYYY-MM-DD.md   ← 写入磁盘
              │
              ▼ (file_watcher 监听)
         file_store 自动重新索引
              │
              ▼
         memory_search(query)   ← 后续可语义检索
```


# reme_light 系统里，能仅仅对 普通的文字（或者普通的 markdown 文本）进行压缩吗


**不能直接传普通文字，但包一层 `Msg` 就可以用。** 具体分析如下：

---

## 接口层面：强依赖 `list[Msg]`

`compact_memory` 和 `summary_memory` 的入参都是 `list[Msg]`，没有接收纯字符串的重载：

```362:375:reme/reme_light.py
    async def compact_memory(
        self,
        messages: list[Msg],
        ...
    ) -> str | dict:
```

```438:450:reme/reme_light.py
    async def summary_memory(
        self,
        messages: list[Msg],
        ...
    ) -> str:
```

---

## 实际处理：`Msg` 只是个壳

进入 `Compactor` / `Summarizer` 后，第一步就是把 `list[Msg]` 转成字符串：

```60:64:reme/memory/file_based/components/compactor.py
        history_formatted_str: str = await msg_handler.format_msgs_to_str(
            messages=messages,
            memory_compact_threshold=self.memory_compact_threshold,
            include_thinking=self.add_thinking_block,
        )
```

`Msg.content` 如果是字符串，`stat_message` 会直接取出来用，不做任何特殊处理。

---

## 结论：包一层 `Msg` 即可

```python
from agentscope.message import Msg

plain_text = "# 我的文档\n这是一段很长的 markdown 内容..."

result = await app.compact_memory(
    messages=[Msg(name="user", role="user", content=plain_text)]
)
```

这样完全可行，LLM 收到的就是你的文本内容。

---

## 注意两个方法的差异

| 方法 | 适合压缩普通文本？ | 说明 |
|------|------|------|
| `compact_memory` | **适合** | 输入文本 → LLM 压缩 → 返回压缩后的字符串，不写文件 |
| `summary_memory` | **不适合** | 它的 prompt 是固定的"记忆持久化"任务，LLM 会尝试把内容写入 `memory/YYYY-MM-DD.md`，而不是返回压缩结果 |

所以如果只是想压缩一段普通文本并拿到结果，用 `compact_memory` + 包一层 `Msg` 就够了。

----- 我回复完成了 -----

# reme_light 在 compress message 的时候，也会触发去写 memory 文件

你记的是对的。准确的说法是：

**`compact_memory()` 本身不写文件，但 `pre_reasoning_hook` 触发压缩时，会同时启动两件事：**

```650:670:reme/reme_light.py
        self.add_async_summary_task(      # ① 后台异步 → 写 memory/YYYY-MM-DD.md
            messages=messages_to_compact,
            ...
        )

        compressed_summary = await self.compact_memory(  # ② 同步 → 只返回字符串，不写文件
            messages=messages_to_compact,
            ...
        )
```

两者操作的是**同一批** `messages_to_compact`，同时触发：

| | `add_async_summary_task` → `summary_memory` | `compact_memory` |
|---|---|---|
| 执行方式 | 后台异步（不阻塞） | 同步等待结果 |
| 输出 | 写入 `memory/YYYY-MM-DD.md` | 返回压缩摘要字符串 |
| 目的 | 长期持久化（详细） | 维持当前 LLM 上下文（精简） |

所以"compress message 时也会触发写 memory 文件"的说法是正确的——只要走的是 `pre_reasoning_hook` 这条路径。如果直接单独调用 `compact_memory()`，则不会写文件。

----- 我回复完成了 -----
