# tool_result/* 文件的生成和使用逻辑

## 1. 整体定位

`tool_result/` 目录（`{working_dir}/tool_results/`）是 ReMeLight 的**工具输出缓存目录**。  
当某条 `tool_result` 消息的 output 超出字节阈值，系统会把**完整原始内容**写入该目录下的 `{uuid}.txt`，同时把消息里的 output 截断为更短的片段，并在末尾附一条"续读提示"，让 LLM 知道完整内容在哪里、从哪行继续读。

---

## 2. 文件生成：ToolResultCompactor._truncate()

入口：`reme/memory/file_based/components/tool_result_compactor.py`

```
_truncate(content, max_bytes)
  ├── 如果 content 已含 TRUNCATION_NOTICE_MARKER  →  调 truncate_text_output() 做"二次截断"（只更新 notice 里的字节数和起始行号）
  ├── 如果 len(content) <= max_bytes + 100        →  直接返回原文，不写文件
  └── 否则（超出阈值）
        ├── 生成 uuid，写完整内容到 tool_result_dir/{uuid}.txt
        └── 调 truncate_text_output(content, 1, total_lines, max_bytes, file_path=saved_path)
              → 返回"前 max_bytes 字节 + TRUNCATION_NOTICE_MARKER + 续读提示"
```

截断后消息里的 output 末尾会是这样的格式：
```
...（前 max_bytes 字节的内容）
<<<TRUNCATED>>>
The output above was truncated.
The full content is saved to the file and contains 1200 lines in total.
This excerpt starts at line 1 and covers the next 3000 bytes.
If the current content is not enough, call `read_file` with file_path=.reme/tool_results/abc123.txt start_line=51 to read more.
```

---

## 3. 截断策略：近期消息 vs 历史消息

`ToolResultCompactor.execute()` 在处理所有消息前，先确定"近期"窗口：

```
recent_n = 从消息列表末尾往前数，连续属于 tool_result 的消息数量
split_index = max(0, len(messages) - max(recent_n, self.recent_n))
```

然后按 `is_recent = idx >= split_index` 来选阈值：

| 消息位置         | 使用阈值             | 默认值    |
|--------------|------------------|--------|
| 近期（recent）  | `recent_max_bytes` | 100 KB |
| 历史（old）     | `old_max_bytes`    | 3 KB   |

**特殊豁免**：如果某个 `tool_use` 的 `name` 是 `read_file` 且输入路径包含 `.md`，对应的 `tool_result` 始终使用 `recent_max_bytes`（不压缩到 3KB），避免截断 Markdown 知识文件。

---

## 4. 调用时机

### 4.1 compact_tool_result()（显式调用）

`reme_light.py` 的 `compact_tool_result()` 方法，直接把 messages 传给 `ToolResultCompactor.call()`，调用完后还会执行一次 `cleanup_expired_files()`。

### 4.2 pre_reasoning_hook()（推理前自动调用）

```python
# reme_light.py pre_reasoning_hook()
if enable_tool_result_compact and tool_result_compact_keep_n > 0:
    compact_msgs = messages[:-tool_result_compact_keep_n]   # 最近 N 条跳过
    await self.compact_tool_result(compact_msgs)
```

即：每次推理前，自动对"除最近 `tool_result_compact_keep_n` 条以外"的历史消息做工具结果压缩。

### 4.3 start() / close()

应用启动和关闭时，会调用 `_cleanup_tool_results()`（内部只做过期文件清理，不写新文件）。

---

## 5. 文件使用：LLM 读取完整内容

当 LLM 发现消息末尾有 `<<<TRUNCATED>>>` 续读提示，它会主动调用工具：
```
read_file(file_path=".reme/tool_results/abc123.txt", start_line=51)
```

`FileIO.read_file` 负责从该路径按行读取，返回后同样经过 `truncate_text_output` 截断处理（如果仍然太长，则再次附 notice 让 LLM 继续翻页）。

---

## 6. 文件清理：cleanup_expired_files()

`ToolResultCompactor.cleanup_expired_files()` 遍历 `tool_result_dir/*.txt`，按文件创建时间（Windows 用 `st_ctime`，macOS/Linux 用 `st_birthtime`/`st_mtime`）与 `retention_days`（默认 3 天）比较，删除过期文件。

调用时机：
- `start()` → `_cleanup_tool_results()`
- `close()` → `_cleanup_tool_results()`
- `compact_tool_result()` 执行后

---

## 7. 数据流总结

```
tool 返回超长 output
      │
      ▼
ToolResultCompactor._truncate()
      ├── 写完整内容到 tool_results/{uuid}.txt
      └── messages 中的 output 替换为"截断片段 + <<<TRUNCATED>>> + 续读提示(file_path, start_line)"
      
LLM 看到 <<<TRUNCATED>>> 提示
      │
      ▼
调用 read_file(file_path=tool_results/{uuid}.txt, start_line=N)
      │
      ▼
FileIO.read_file 返回（可能再次截断，再次附提示）

过期文件（> retention_days 天）
      │
      ▼
cleanup_expired_files() 自动删除
```
