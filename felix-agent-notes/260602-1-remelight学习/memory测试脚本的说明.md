# 初始化

```python
    reme = ReMeLight(
        working_dir="felix_test/.reme_test",           # 工作目录，存放所有记忆文件
        llm_api_key="sk-3fe61157d23345779df499667e5263fa",                        # 从环境变量读取
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",         # 从环境变量读取
        embedding_api_key="sk-29a937c25bff4752af88514525c329b3",                  # 从环境变量读取（可选）
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",   # 从环境变量读取（可选）
        default_as_llm_config={
            "model_name": "qwen3.6-plus",              # 用于 compact/summary 的 LLM
        },
        default_embedding_model_config={
            "model_name": "text-embedding-v4",         # 用于向量检索的 embedding 模型
        },
        default_file_store_config={
            "backend": "chroma",                       # 显式指定后端（chroma / local 等）
            "store_name": "reme",                      # chroma collection 名称
            "fts_enabled": True,                       # 启用全文搜索（BM25）
            "vector_enabled": True,                    # 启用向量搜索
        },
        default_file_watcher_config=None,              # None = 使用内置默认监听路径
        vector_weight=0.7,                             # 向量搜索权重
        candidate_multiplier=3.0,                      # 候选结果倍数（实际返回数 × 3 取候选）
        enable_load_env=True,                          # 从 .env 文件加载环境变量
    )
```

## default_file_watcher_config配置

```python
default_file_watcher_config = {
    # ── 核心字段 ──────────────────────────────────
    "watch_paths": [                        # 监听哪些路径（文件或目录）
        "/path/to/.reme/MEMORY.md",
        "/path/to/.reme/memory/",
    ],
    # ── 过滤 & 监听行为 ────────────────────────────
    "suffix_filters": [".md"],             # 只处理哪些后缀的文件变更，None = 不过滤
    "recursive": False,                    # 是否递归监听子目录，默认 False
    # ── 性能调优 ───────────────────────────────────
    "debounce": 2000,                      # 防抖时间（ms），文件变更后等多久才触发索引
    "poll_delay_ms": 2000,                 # 轮询间隔（ms）
    # ── 索引行为 ───────────────────────────────────
    "chunk_tokens": 400,                   # 文件分块大小（tokens）
    "chunk_overlap": 80,                   # 相邻分块的重叠 tokens 数
    "rebuild_index_on_start": True,        # True = 启动时清空旧索引并全量重扫
}
```

关键逻辑：有没有写 watch_paths 决定了行为不同：
  - 写了 watch_paths → 完全用你的配置，框架默认的 MEMORY.md + memory/ 不会自动加进来
  - 没写 watch_paths → 你的其他字段和默认路径合并，默认路径自动补上


# 压缩消息：压缩对话为摘要字符串

```python
    compact_summary = S(reme.compact_memory(
        messages=messages_to_compact,      # 待压缩的历史消息
        as_llm="default",                  # 使用默认 LLM（初始化时配置的 model_name）
        as_llm_formatter="default",        # 使用默认 formatter
        as_token_counter="default",        # 使用默认 token 计数器
        language="zh",                     # 摘要语言：zh = 中文
        max_input_length=128 * 1024,       # 模型上下文窗口（128K tokens）
        compact_ratio=0.7,                 # # 送给 LLM 的消息上限 = max_input_length × 0.7 × 0.95，超出部分（更老的消息）直接丢弃
        previous_summary="",              # 上轮摘要（首次为空字符串）
        return_dict=False,                 # False = 返回字符串；True = 返回 dict
        add_thinking_block=True,           # 生成摘要前加入思考步骤，提升摘要质量
        extra_instruction="",              # 附加指令（空 = 使用默认行为）
    ))
```

## as_llm_formatter

把 list[Msg] 转换成 LLM API 能接收的请求格式（即 messages 数组的结构）。不同的 LLM 厂商对消息格式有细微差异，formatter 负责抹平这些差异。

从注册表看，目前支持两个 backend：
  - R.as_llm_formatters.register("openai")(ReMeOpenAIChatFormatter)
  - R.as_llm_formatters.register("dashscope")(DashScopeChatFormatter)
  - 默认是 openai

## max_input_length 和 compact_ratio

作用是：限制送给 LLM 的消息内容最多有多少 token
计算公式：max_input_length × compact_ratio × 0.95 = 128K × 0.7 × 0.95 ≈ 85K tokens
format_msgs_to_str() 从最新消息往前累加，累加到超过 85K 就停，更老的消息直接丢弃，不送给 LLM

## previous_summary

**传什么内容：**

传上一次 `compact_memory()` 返回的字符串。典型用法：

```python
# 第一次调用，没有历史摘要
summary_v1 = S(reme.compact_memory(
    messages=messages_batch_1,
    previous_summary="",
))

# 第二次调用，把上次的摘要传进去
summary_v2 = S(reme.compact_memory(
    messages=messages_batch_2,
    previous_summary=summary_v1,   # ← 传上一轮的结果
))
```

**传入后的效果：**

看 `compactor.yaml` 里的 prompt 处理逻辑（源码第 82-88 行）：

```python
if previous_summary:
    user_message = (
        f"# conversation\n{history_formatted_str}\n\n"
        f"# previous-summary\n{previous_summary}\n\n"
        + self.get_prompt("update_user_message")   # ← 用"更新"模式的 prompt
    )
else:
    user_message = (
        f"# conversation\n{history_formatted_str}\n\n"
        + self.get_prompt("initial_user_message")  # ← 用"初始"模式的 prompt
    )
```

- **空字符串**：LLM 从零开始生成摘要（initial 模式）
- **有内容**：LLM 在旧摘要基础上做**增量更新**（update 模式），把新对话里的信息合并进去，而不是重新写一遍

**实际使用场景：**

在长对话里，每次触发压缩时把上一轮的摘要传入，摘要就会滚动积累，始终保持对整个历史对话的完整概括，而不是只反映最近一批消息。

## extra_instruction

任何你想让 LLM 在压缩时额外注意的内容，比如：

```
# 聚焦保留的内容
extra_instruction="重点保留所有涉及 API 接口、数据库 schema 的内容，忽略闲聊部分。"

# 排除不需要的内容
extra_instruction="不要保留调试日志和错误堆栈，只保留最终结论和决策。"

# 指定摘要风格
extra_instruction="摘要要包含具体的文件路径、函数名、变量名，不要用模糊描述。"

# 强调某类信息
extra_instruction="如果对话中有用户明确的偏好或约束条件，必须单独列出，不能合并到其他章节。"
```

## prompt

现在全部清楚了，整理如下：

---

**`compact_memory()` 用的 prompt（`compactor.yaml`）：**

分三段，`language="zh"` 时自动选 `_zh` 后缀版本：

| 段 | 作用 | 何时使用 |
|---|---|---|
| `system_prompt_zh` | 角色定义：你是上下文压缩助手 | 每次都用 |
| `initial_user_message_zh` | 让 LLM 从零生成结构化摘要 | `previous_summary=""` 时 |
| `update_user_message_zh` | 让 LLM 在旧摘要基础上增量更新 | `previous_summary` 非空时 |

输出格式固定为 `## 目标 / ## 约束和偏好 / ## 进展 / ## 关键决策 / ## 下一步 / ## 关键上下文` 六个章节。

---

**如何使用自定义 prompt：**

有两种方式：

**方式一：`extra_instruction` 微调（最简单）**

直接在调用时传入，内容追加在 prompt 末尾：

```python
compact_summary = S(reme.compact_memory(
    messages=messages_to_compact,
    extra_instruction="重点保留 API 接口设计决策，忽略调试过程。",
    ...
))
```

**方式二：替换整个 yaml 文件（完全自定义）**

继承 `Compactor`，指定自己的 yaml 路径：

```python
from reme.memory.file_based.components import Compactor

class MyCompactor(Compactor):
    pass

# 在同目录下放一个 my_compactor.yaml，格式和 compactor.yaml 一样
# BaseOp 会自动根据类名找同名 yaml 文件
```

或者在 `Compactor.__init__` 里传 `prompt_path` 参数直接指定 yaml 路径：

```python
compactor = Compactor(
    ...
    prompt_path="/path/to/my_custom_prompt.yaml",
)
```

实际项目里**方式一（`extra_instruction`）通常就够用**，只有需要完全改变输出格式时才用方式二。

---------- 怎么设置Compactor对象 ----------

没有办法直接把自定义的 `Compactor` 实例传给 `reme.compact_memory()`。

看源码，`compact_memory()` 内部是**硬编码**创建 `Compactor` 的：

```413:429:reme/reme_light.py
        try:
            compactor = Compactor(
                memory_compact_threshold=self.calculate_memory_compact_threshold(max_input_length, compact_ratio),
                as_llm=as_llm,
                as_llm_formatter=as_llm_formatter,
                as_token_counter=as_token_counter,
                language=language if language == "zh" else "",
                return_dict=return_dict,
                add_thinking_block=add_thinking_block,
                extra_instruction=extra_instruction,
            )

            return await compactor.call(
                messages=messages,
                previous_summary=previous_summary,
                service_context=self.service_context,
            )
```

没有暴露 `compactor` 参数，所以无法从外部注入。

**如果要完全自定义 prompt，实际可行的方式只有：**

**方式一：直接修改 `compactor.yaml`**（最直接）

**方式二：绕过 `compact_memory()`，直接自己调用 `Compactor`**

```python
from reme.memory.file_based.components import Compactor

my_compactor = Compactor(
    memory_compact_threshold=int(128 * 1024 * 0.7 * 0.95),
    as_llm="default",
    as_llm_formatter="default",
    as_token_counter="default",
    language="zh",
    return_dict=False,
    add_thinking_block=True,
    extra_instruction="",
    prompt_path="/path/to/my_custom_prompt.yaml",   # ← 指定自定义 yaml
)

result = S(my_compactor.call(
    messages=messages_to_compact,
    previous_summary="",
    service_context=reme.service_context,   # ← 复用 reme 的 service_context
))
```

**方式三：继承 `ReMeLight`，重写 `compact_memory()`**

```python
class MyReMeLight(ReMeLight):
    async def compact_memory(self, messages, **kwargs):
        compactor = MyCompactor(...)   # 用自定义 Compactor
        return await compactor.call(...)
```

实际上**方式二**最简单，直接用 `reme.service_context` 复用已有的 LLM/embedding 配置。

# 将对话摘要写入 memory/

```python
    summary_result = S(reme.summary_memory(
        messages=messages_to_compact,      # 待摘要的历史消息（同 compact_memory 的输入）
        as_llm="default",                  # 使用默认 LLM
        as_llm_formatter="default",        # 使用默认 formatter
        as_token_counter="default",        # 使用默认 token 计数器
        toolkit=None,                      # None = 自动创建含 read/write/edit 的 FileIO toolkit
        language="zh",                     # 摘要语言
        max_input_length=128 * 1024,       # 模型上下文窗口
        compact_ratio=0.7,                 # 送给 LLM 的消息上限 = max_input_length × 0.7 × 0.95，超出部分（更老的消息）直接丢弃
        timezone=None,                     # None = 使用系统本地时区确定文件日期
        add_thinking_block=True,           # 加入思考步骤
    ))
```

## add_thinking_block

`add_thinking_block` 在 `compact_memory` 和 `summary_memory` 里的含义**完全相同**，都是作为 `include_thinking` 传给 `format_msgs_to_str()`：

```python
history_formatted_str = await msg_handler.format_msgs_to_str(
    messages=messages,
    memory_compact_threshold=...,
    include_thinking=self.add_thinking_block,   # ← 两者都一样
)
```

**实际含义：**

输入的 `messages` 里，如果有些消息包含模型的 thinking block（即 Claude 等模型的 `<thinking>...</thinking>` 推理过程内容），：

- `add_thinking_block=True`：把 thinking block 内容也包含进格式化字符串，送给 LLM 一起处理
- `add_thinking_block=False`：过滤掉 thinking block，只送正文内容给 LLM

对我们测试脚本里的消息（普通文字对话，没有 thinking block）来说，这个参数没有实际影响，设 `True` 或 `False` 效果一样。

## toolkit

`toolkit` 参数的类型是 `agentscope.tool.Toolkit`，本质是**给 ReAct Agent 配备一组工具函数**。

默认 `None` 时框架自动创建包含 `read_file` / `write_file` / `edit_file` 三个工具的 toolkit。

**能设置什么不同的值：**

`FileIO` 实际上还有第四个工具 `append_file`（追加写文件），默认没有被注册进去。如果你想让 LLM 能用追加写而不是覆盖写，可以自己构建 toolkit：

```python
from agentscope.tool import Toolkit
from reme.memory.file_based.tools import FileIO

file_io = FileIO(working_dir=str(reme.working_path))
toolkit = Toolkit()
toolkit.register_tool_function(file_io.read_file)
toolkit.register_tool_function(file_io.write_file)
toolkit.register_tool_function(file_io.edit_file)
toolkit.register_tool_function(file_io.append_file)   # ← 追加注册 append_file

summary_result = S(reme.summary_memory(
    messages=messages_to_compact,
    toolkit=toolkit,   # ← 传入自定义 toolkit
    ...
))
```

也可以把完全自定义的工具函数注册进去，让 ReAct Agent 在写 memory 文件时能调用你自己的逻辑（比如写到数据库、发送通知等）。

## prompt

`summary_memory()` 的 prompt 固定来自 `summarizer.yaml`，和 `compact_memory()` 一样，**没有 `extra_instruction` 参数**，所以微调能力比 `compact_memory` 弱。

自定义方式也是同样的两种：

**方式一：直接修改 `summarizer.yaml`**（最直接）

文件在：`reme/memory/file_based/components/summarizer.yaml`，改完即生效。

**方式二：绕过 `summary_memory()`，直接调用 `Summarizer`**

```python
from agentscope.tool import Toolkit
from reme.memory.file_based.components import Summarizer
from reme.memory.file_based.tools import FileIO

file_io = FileIO(working_dir=str(reme.working_path))
toolkit = Toolkit()
toolkit.register_tool_function(file_io.read_file)
toolkit.register_tool_function(file_io.write_file)
toolkit.register_tool_function(file_io.edit_file)

my_summarizer = Summarizer(
    working_dir=str(reme.working_path),
    memory_dir=str(reme.memory_path),
    memory_compact_threshold=int(128 * 1024 * 0.7 * 0.95),
    toolkit=toolkit,
    as_llm="default",
    as_llm_formatter="default",
    as_token_counter="default",
    language="zh",
    timezone=None,
    add_thinking_block=True,
    prompt_path="/path/to/my_summarizer.yaml",   # ← 指定自定义 yaml
)

result = S(my_summarizer.call(
    messages=messages_to_compact,
    service_context=reme.service_context,
))
```

自定义 yaml 的格式参考 `summarizer.yaml`，只需要包含 `user_message` 或 `user_message_zh` 字段即可。


# 等待后台 summary 任务全部落盘

```python
    pending_count = len(reme.summary_tasks)
    print(f"  当前 pending summary_tasks 数：{pending_count}（同步调用 summary_memory 不产生后台任务）")

    tasks_result = S(reme.await_summary_tasks())
    print(f"  await_summary_tasks() 返回：{tasks_result!r}")
```

只有通过 `add_async_summary_task()` 启动的后台任务才会被记录到 `summary_tasks`。

对比一下两者：

| | 记录到 `summary_tasks`？ | 被 `await_summary_tasks()` 等待？ |
|---|---|---|
| 直接调用 `compact_memory()` | 否 | 否 |
| 直接调用 `summary_memory()` | 否 | 否 |
| `add_async_summary_task()` 启动的后台任务 | **是** | **是** |

`add_async_summary_task()` 内部是这样：

```552:553:reme/reme_light.py
        task = asyncio.create_task(self.summary_memory(messages=messages, **kwargs))
        self.summary_tasks.append(task)
```

只有 `summary_memory()` 被包成 `asyncio.Task` 后台运行时，才会进入 `summary_tasks`。`compact_memory()` 从来不会进 `summary_tasks`。

在我们的测试脚本里，直接 `S(reme.compact_memory(...))` 和 `S(reme.summary_memory(...))` 都是同步阻塞调用，不产生后台任务，所以 `reme.summary_tasks` 始终是空列表，`await_summary_tasks()` 立即返回。


# 混合搜索（切词）

```
    search_result = S(reme.memory_search(
        query="JWT token 刷新机制",
        max_results=5,
        min_score=0.1,
    ))
```

整个流程中，**没有做任何中文分词**（如 jieba 等）。以下是 query 的实际处理路径：

**`hybrid_search` = 向量搜索 + 关键词搜索 (FTS) 混合**

---

**1. 向量搜索部分**

query 原样送入 embedding 模型，由模型内部的 tokenizer 处理，Python 层不做任何预处理。

---

**2. 关键词搜索部分（FTS）**

对 query `"JWT token 刷新机制"` 的处理步骤如下：

**Step 1 - Sanitize**：去除 FTS5 特殊字符（`*/?:+-` 等），归一化空白

```609:681:reme/core/file_store/sqlite_file_store.py
    def _sanitize_fts_query(query: str) -> str:
        ...
        cleaned = " ".join(cleaned.split())
        return cleaned
```

**Step 2 - 按空格切分**：`cleaned.split()` → `["JWT", "token", "刷新机制"]`

**Step 3 - 选择搜索策略**：所有词 `len >= 3` → 走 FTS5 trigram 路径

```692:714:reme/core/file_store/sqlite_file_store.py
        # FTS5 trigram (fast path): used when ALL terms >= 3 chars (trigram minimum).
        # LIKE (universal fallback): used when any term < 3 chars, covering CJK
        #   short words, single/double-char queries, and mixed-length queries.
```

**Step 4 - FTS5 Trigram 搜索**：构造 `JWT OR token OR 刷新机制` 作为 FTS 查询语句，数据库层面 trigram tokenizer 将文本按每 3 个字符一组建立索引（字符级三元组，对中文字符也是字符计数）。

---

**结论**

- 不用 jieba 等分词器
- 中文 query 靠**空格**切分词（所以 `"刷新机制"` 作为一个整体词去搜，而不是 `["刷新", "机制"]`）
- FTS5 trigram 是字符级 n-gram，和语言无关
- 向量侧依赖 embedding 模型内置 tokenizer

