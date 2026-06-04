# `base_file_watcher.py`

这个文件定义的是所有 file watcher 的公共骨架。  
它本身不负责“怎么切 chunk、怎么做全量更新、怎么做增量更新”，而是负责：

- 管理 watcher 的生命周期
- 统一处理启动、关闭、初始扫描、持续监听
- 把文件变化事件转发给子类或外部 callback

可以把它理解成一个“调度框架层”。

## 1. 文件头和依赖

```python
import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Callable

from loguru import logger
from watchfiles import awatch, Change

from ..enumeration import MemorySource
from ..file_store import BaseFileStore
```

这里用到的几个关键依赖：

- `awatch` / `Change`
  - 来自 `watchfiles`
  - 用来异步监听文件变化，并给出变化类型，例如 `added`、`modified`、`deleted`

- `BaseFileStore`
  - watcher 不直接做存储，而是把结果交给 `file_store`

- `MemorySource`
  - 在扫描后回查已索引文件时，会用 `MemorySource.MEMORY`

## 2. `BaseFileWatcher` 类的定位

```python
class BaseFileWatcher:
```

这个类是所有具体 watcher 的父类。  
它的设计意图很明确：

- 公共部分放这里
- 具体策略放到子类的 `_on_changes()` 里

也就是说：

- `BaseFileWatcher`
  - 决定“什么时候监听、什么时候扫描、什么时候派发变化”
- `FullFileWatcher` / `DeltaFileWatcher`
  - 决定“变化来了以后，具体怎么处理”

## 3. `__init__`：初始化 watcher 的运行参数

```python
def __init__(
    self,
    watch_paths: list[str] | str,
    suffix_filters: list[str] | None = None,
    recursive: bool = False,
    debounce: int = 2000,
    chunk_tokens: int = 400,
    chunk_overlap: int = 80,
    file_store: BaseFileStore | None = None,
    callback: Callable[[set[tuple[Change, str]]], None | Coroutine[Any, Any, None]] | None = None,
    rebuild_index_on_start: bool = True,
    poll_delay_ms: int = 2000,
    **kwargs,
):
```

这一段定义了 watcher 的核心配置项。

### `watch_paths`

表示要监听哪些路径，可以传：

- 一个文件
- 一个目录
- 多个文件/目录

下面这一句会把单个字符串统一包装成列表，方便后面统一处理：

```python
self.watch_paths: list[str] = [watch_paths] if isinstance(watch_paths, str) else watch_paths
```

### `suffix_filters`

表示只监听哪些后缀的文件。  
例如 `[".md"]` 就只处理 markdown 文件。

### `recursive`

如果监听的是目录：

- `False`：只看第一层文件
- `True`：递归扫描子目录

### `debounce`

传给 `watchfiles` 的去抖时间。  
用来避免一次保存操作触发太多零碎事件。

### `chunk_tokens` / `chunk_overlap`

这两个参数虽然是在基类里定义，但基类自己并不直接切 chunk。  
它只是把这些参数保存下来，供子类（尤其是 `FullFileWatcher` / `DeltaFileWatcher`）在 `chunk_markdown()` 时使用。

### `file_store`

这是 watcher 和底层存储层衔接的关键。  
watcher 自己不存数据，而是把处理后的结果交给 `file_store`。

### `callback`

允许外部注入一个变化处理函数。  
如果传了 `callback`，后面收到文件变化时优先调用它；否则走子类的 `_on_changes()`。

### `rebuild_index_on_start`

这个参数决定 watcher 启动时是否做一次“清空索引 + 全量扫描已有文件”。

- `True`
  - 启动即重建索引
- `False`
  - 只监听启动之后的新变化

### `poll_delay_ms`

控制轮询间隔。  
因为这里的 `awatch(...)` 固定传了 `force_polling=True`，所以实际是强制轮询模式。

### 内部状态字段

```python
self._stop_event = asyncio.Event()
self._watch_task: asyncio.Task | None = None
self._running = False
```

这几个字段分别表示：

- `_stop_event`：通知后台监听循环停止
- `_watch_task`：后台异步监听任务
- `_running`：当前 watcher 是否处于运行中

## 4. `start()`：启动 watcher

```python
async def start(self):
    if self._running:
        return
```

如果已经在运行，就直接返回，避免重复启动。

然后重新初始化停止事件，并把状态设成运行中：

```python
self._stop_event = asyncio.Event()
self._running = True
```

### 内部的 `_initialize_and_watch()`

```python
async def _initialize_and_watch():
    if self.rebuild_index_on_start:
        if self.file_store is not None:
            await self.file_store.clear_all()
            logger.info("Cleared all indexed data on start")
        await self._scan_existing_files()
    await self._watch_loop()
```

这是整个 watcher 启动后的总流程，分两步：

1. 如果要求启动即重建索引：
   - 先调用 `file_store.clear_all()`
   - 再扫描当前已有文件
2. 然后进入持续监听循环

这也是为什么之前在分析 `ReMeLight -> ChromaFileStore` 链路时，会看到：

- watcher 启动时会触发 `clear_all()`
- 然后触发初始建索引

最后，这个总流程不是阻塞当前协程执行，而是被放进后台 task：

```python
self._watch_task = asyncio.create_task(_initialize_and_watch())
```

所以 `start()` 的语义是“启动后台监听任务”，而不是“阻塞直到监听结束”。

## 5. `close()`：关闭 watcher

```python
async def close(self):
    if not self._running:
        return
```

如果没有运行，就直接返回。

真正关闭时，它会：

1. 设置停止事件
2. 等待后台监听任务退出
3. 把 `_running` 设回 `False`

```python
self._stop_event.set()
if self._watch_task:
    await self._watch_task
self._running = False
```

这说明 watcher 的关闭是“协作式”的：

- 不是强杀后台任务
- 而是通知 `_watch_loop()` 自己停下来

## 6. `watch_filter()`：过滤哪些文件应该被监听

```python
def watch_filter(self, _change: Change, path: str) -> bool:
```

这个函数是传给 `watchfiles.awatch()` 的过滤器，也在扫描已有文件时复用。

逻辑很简单：

- 如果没有配置 `suffix_filters`，全部放行
- 否则只放行后缀匹配的文件

匹配逻辑在这里：

```python
for suffix in self.suffix_filters:
    if path.endswith("." + suffix.strip(".")):
        return True
```

这里做了一个小兼容：

- 既支持传 `".md"`
- 也支持传 `"md"`

## 7. `_scan_existing_files()`：启动时扫描已有文件

```python
async def _scan_existing_files(self):
    existing_files: set[tuple[Change, str]] = set()
```

这个方法的作用是：

**把“启动前已经存在的文件”也当成一次 `Change.added` 事件来处理。**

这很重要，因为 watcher 不只是想监听未来变化，还想在启动时把当前文件系统状态同步进索引。

### 遍历监听路径

```python
for watch_path_str in self.watch_paths:
    watch_path = Path(watch_path_str)
```

这里会逐个处理每个监听路径。

### 如果路径不存在

```python
if not watch_path.exists():
    logger.warning(f"Watch path does not exist: {watch_path}")
    continue
```

直接跳过，不报错退出。

### 如果是单个文件

```python
if watch_path.is_file():
    if self.watch_filter(Change.added, str(watch_path)):
        existing_files.add((Change.added, str(watch_path)))
```

如果单个文件满足后缀过滤条件，就把它加入待处理集合。

### 如果是目录

目录分两种扫描方式：

- `recursive=True` 时用 `rglob("*")`
- `recursive=False` 时用 `iterdir()`

也就是说，是否递归完全由 watcher 配置决定。

### 扫描完成后触发统一处理

```python
if existing_files:
    await self.on_changes(existing_files)
```

这一步非常关键。  
它说明“启动时扫描已有文件”和“运行中收到文件变化”最终走的是同一套处理接口：`on_changes()`。

也就是说，子类并不需要区分：

- 这是启动扫描来的事件
- 还是运行中监听来的事件

两者都会被包装成变化事件集来处理。

### 扫描后回查 file_store 中已有文件

```python
if self.file_store is not None:
    files: list[str] = await self.file_store.list_files(MemorySource.MEMORY)
    for file_path in files:
        chunks = await self.file_store.get_file_chunks(file_path, MemorySource.MEMORY)
        logger.info(f"Found existing file: {file_path}, {len(chunks)} chunks")
```

这一段更偏调试/校验用途。  
它会从 `file_store` 再读一遍当前已索引文件和 chunk 数量，打印日志确认结果。

## 8. `_interruptible_sleep()`：可中断睡眠

```python
async def _interruptible_sleep(self, seconds: float):
```

这个函数的作用是：  
在 watcher 出错后等待重试时，不是简单 `sleep(seconds)`，而是允许在等待期间被 `_stop_event` 打断。

实现方式是：

```python
await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
```

如果在 timeout 之前收到了 stop event，就提前返回；否则正常超时继续执行。

这个设计能保证：

- watcher 出错后会等待再重试
- 但如果这时用户要求关闭 watcher，不用傻等完整的 10 秒

## 9. `_watch_loop()`：持续监听文件变化

```python
async def _watch_loop(self):
```

这是基类最核心的运行循环。

### 没有监听路径就直接退出

```python
if not self.watch_paths:
    logger.warning("No watch paths specified")
    return
```

### 外层 `while`：保证出错后自动重启

```python
while not self._stop_event.is_set():
```

整个监听逻辑放在一个大循环里，这意味着：

- 正常运行时会一直监听
- 如果 `awatch()` 抛异常，不会让 watcher 整体死掉
- 而是等待一段时间后再重启监听

### 每轮先过滤掉不存在的路径

```python
valid_paths = [p for p in self.watch_paths if Path(p).exists()]
```

这是个很实用的容错逻辑。  
因为有些路径可能被用户删掉了，或者临时不可用。

如果一个都不存在：

```python
if not valid_paths:
    logger.warning("No valid watch paths exist, waiting 10 seconds before retry...")
    await self._interruptible_sleep(10)
    continue
```

### 调用 `awatch()` 进入监听流

```python
async for changes in awatch(
    *valid_paths,
    force_polling=True,
    watch_filter=self.watch_filter,
    recursive=self.recursive,
    debounce=self.debounce,
    poll_delay_ms=self.poll_delay_ms,
    stop_event=self._stop_event,
):
```

这里的关键参数：

- `force_polling=True`
  - 强制轮询方式监听
- `watch_filter=self.watch_filter`
  - 套用前面定义的后缀过滤
- `recursive=self.recursive`
  - 是否递归
- `debounce=self.debounce`
  - 去抖
- `stop_event=self._stop_event`
  - 允许外部通知停止

### 每次收到变化，统一交给 `on_changes()`

```python
await self.on_changes(changes)
```

这说明基类不关心“怎么处理变化”，只负责把变化事件转发出去。

### 两类异常处理

#### `FileNotFoundError`

```python
except FileNotFoundError as e:
```

说明监听的某个路径在运行中消失了。  
这里不会直接崩，而是记日志，等待 10 秒后重试。

#### 其他异常

```python
except Exception as e:
```

任何其他异常也一样：

- 记日志
- 等待 10 秒
- 再重启监听

这就是基类“自动恢复”的核心实现。

## 10. `_on_changes()`：留给子类实现的真正处理逻辑

```python
async def _on_changes(self, changes: set[tuple[Change, str]]):
    """Callback method to handle file changes"""
```

这个函数在基类里没有实现，相当于一个抽象钩子。  
子类必须重写它，决定：

- 收到文件变化后，怎么读取文件
- 怎么切 chunk
- 怎么调用 `file_store`

## 11. `on_changes()`：统一分发变化处理

```python
async def on_changes(self, changes: set[tuple[Change, str]]):
```

这是基类暴露出来的统一入口。

逻辑是：

- 如果定义了外部 `callback`，优先执行 callback
- 否则走子类的 `_on_changes()`

```python
if self.callback:
    result = self.callback(changes)
    if asyncio.iscoroutine(result):
        await result
else:
    await self._on_changes(changes)
```

这里的好处是：

- 默认情况下，子类靠重写 `_on_changes()` 工作
- 需要特殊行为时，也可以在外部直接注入 callback，替换子类逻辑

最后统一打日志：

```python
logger.info(f"[{self.__class__.__name__}] on_changes: {changes}")
```

## 12. `is_running()`：查询运行状态

```python
def is_running(self) -> bool:
    return self._running
```

这是一个简单的状态查询方法。

## 13. `add_path()` / `remove_path()`：动态修改监听路径

### `add_path()`

```python
async def add_path(self, path: str):
```

如果新路径不在列表里，就加入。

如果当前 watcher 正在运行，还会：

1. 先 `close()`
2. 再 `start()`

也就是说，这里的策略不是“热更新内部监听器”，而是**重启 watcher** 来应用新的监听路径。

### `remove_path()`

```python
async def remove_path(self, path: str):
```

逻辑和 `add_path()` 对称：

- 从 `watch_paths` 里移除
- 如果正在运行，就重启 watcher

## 14. 对 `base_file_watcher.py` 的整体理解

这个文件最重要的价值不是“做了具体索引逻辑”，而是建立了统一流程：

1. 启动 watcher
2. 如有需要，先清空旧索引
3. 扫描当前已有文件
4. 持续监听未来变化
5. 收到变化后统一派发
6. 允许异常恢复
7. 允许动态调整监听路径

也就是说，它是一个**稳定的 watcher 调度骨架**。

# `full_file_watcher.py`

这个文件实现的是“**全量同步策略**”。

它的思路很直接：

- 只要文件新增或修改
- 就把整个文件重新读取一遍
- 重新切 chunk
- 删除该文件在 `file_store` 中的旧 chunk
- 再把新的全部写回去

这个策略最简单、最稳定，但成本也更高，因为每次修改都可能重算整份文件的 embedding。

## 1. 文件头和依赖

```python
import asyncio
from pathlib import Path

from loguru import logger
from watchfiles import Change

from .base_file_watcher import BaseFileWatcher
from ..enumeration import MemorySource
from ..schema import FileMetadata
from ..utils import chunk_markdown, hash_text
```

这里的几个关键点：

- 继承自 `BaseFileWatcher`
- 使用 `chunk_markdown()` 做分块
- 使用 `hash_text()` 计算文件 hash
- 最后会把结果写到注入进来的 `file_store`

## 2. 类定义和 `__init__`

```python
class FullFileWatcher(BaseFileWatcher):
```

这个类就是具体的“全量同步 watcher”。

构造函数里除了调用父类，没有额外复杂逻辑：

```python
def __init__(self, **kwargs):
    super().__init__(**kwargs)
    self.dirty = False
```

这里的 `dirty` 表示当前 watcher 是否正在处理变更。  
它更像一个运行标记，方便日志或外部调试。

## 3. `_build_file_metadata()`：读取文件并构造 `FileMetadata`

```python
@staticmethod
async def _build_file_metadata(path: str) -> FileMetadata:
```

这个辅助函数专门负责把磁盘上的文件读出来，并包装成 `FileMetadata`。

### 为什么内部再包一层 `_read_file_sync()`

```python
def _read_file_sync():
    return file_path.stat(), file_path.read_text(encoding="utf-8")
```

因为：

- `stat()`
- `read_text()`

都是同步 IO。  
这里用 `asyncio.to_thread(...)` 把同步文件读取丢到线程池里执行，避免阻塞当前事件循环。

### 返回的 `FileMetadata`

```python
return FileMetadata(
    hash=hash_text(content),
    mtime_ms=stat.st_mtime * 1000,
    size=stat.st_size,
    path=str(file_path.absolute()),
    content=content,
)
```

这里记录了：

- 文件内容 hash
- 修改时间
- 文件大小
- 文件绝对路径
- 文件内容本身

后面 `_on_changes()` 会基于这些信息做 chunking 和存储。

## 4. `_on_changes()`：全量同步的核心

```python
async def _on_changes(self, changes: set[tuple[Change, str]]):
```

这是 `FullFileWatcher` 的核心方法，也是它和基类衔接的地方。

一开始先把 `dirty` 设为 `True`，表示进入处理状态：

```python
self.dirty = True
```

然后逐个处理变化事件：

```python
for change_type, path in changes:
```

### 4.1 `added` / `modified`：新增或修改时全量重建

```python
if change_type in [Change.added, Change.modified]:
```

这两种情况的策略是一样的：  
不做细粒度差分，直接重建该文件对应的索引。

#### 第一步：读取文件元信息和内容

```python
file_meta = await self._build_file_metadata(path)
```

#### 第二步：整文件切 chunk

```python
chunks = (
    chunk_markdown(
        file_meta.content,
        file_meta.path,
        MemorySource.MEMORY,
        self.chunk_tokens,
        self.chunk_overlap,
    )
    or []
)
```

这里可以看到，切块时用到了父类保存下来的：

- `chunk_tokens`
- `chunk_overlap`

同时把 source 固定为 `MemorySource.MEMORY`。

#### 第三步：为 chunk 生成 embedding

```python
if chunks:
    chunks = await self.file_store.get_chunk_embeddings(chunks)
```

注意：

- watcher 不自己调用 embedding model
- 而是通过 `file_store.get_chunk_embeddings(...)`

也就是说，watcher 负责调度，embedding 相关能力还是通过存储层/其依赖来完成。

#### 第四步：记录 chunk 数量

```python
file_meta.chunk_count = len(chunks)
```

这是文件级 metadata，用来表示当前文件被切成了多少 chunk。

#### 第五步：先删旧数据

```python
await self.file_store.delete_file(file_meta.path, MemorySource.MEMORY)
```

为什么先删？

因为这是全量策略：  
与其做复杂比较，不如直接把这个文件过去的所有 chunk 清掉，再写入新的。

#### 第六步：写入新数据

```python
await self.file_store.upsert_file(file_meta, MemorySource.MEMORY, chunks)
```

这一句就是“全量同步”最终落库的动作。

可以理解成：

- `FullFileWatcher`
  - 负责重建这份文件的 chunk 结果
- `file_store`
  - 负责把它保存到底层存储中

### 4.2 `deleted`：文件删除时清理索引

```python
elif change_type == Change.deleted:
    await self.file_store.delete_file(path, MemorySource.MEMORY)
```

删除逻辑就更直接了：

- 文件已经没了
- 对应索引也应该删掉

### 4.3 其他变化类型

```python
else:
    logger.warning(f"Unknown change type: {change_type}")
```

这里只做告警，没有额外处理。

### 4.4 收尾

循环结束后：

```python
self.dirty = False
```

表示这批变更已经处理完成。

## 5. 对 `full_file_watcher.py` 的整体理解

这个文件的关键特征是：

- 简单
- 稳妥
- 每次修改都整文件重建

它适合：

- 文件规模不是特别大
- 修改频率不极端高
- 希望逻辑简单、结果稳定

一句话概括：

**`FullFileWatcher` 不尝试分析“改了哪一部分”，它只关心“这个文件变了，那就整份重建”。**

# `delta_file_watcher.py`

这个文件实现的是“**增量同步策略**”。  
它和 `FullFileWatcher` 的核心区别在于：

- `FullFileWatcher`：文件一改就整份重建
- `DeltaFileWatcher`：优先判断是不是“尾部追加”，如果是，就只更新新增部分及受 overlap 影响的部分

所以它的目标是：

**尽量少重算 embedding，尤其适合日志、append-only 文件这类场景。**

## 1. 文件头和依赖

```python
import asyncio
import os

from loguru import logger
from watchfiles import Change

from .base_file_watcher import BaseFileWatcher
from ..enumeration import MemorySource
from ..schema import FileMetadata, MemoryChunk
from ..utils import chunk_markdown, hash_text
```

和 `FullFileWatcher` 相比，这里多用了：

- `os.stat`
- `MemoryChunk`

因为它需要读取旧 chunk，并做增量计算。

## 2. 类定义和总体策略说明

```python
class DeltaFileWatcher(BaseFileWatcher):
```

类注释里已经把设计意图说得很清楚：

- 检测文件是否是 append-only
- 找到安全的 cutoff line
- 只重建 cutoff 到文件末尾这部分
- 删除受影响的旧 chunks
- 插入新的 chunks

也就是说，它的核心不是“做精确 diff”，而是做一种**启发式增量更新**。

## 3. `__init__()`：新增 `overlap_lines`

```python
def __init__(self, overlap_lines: int = 2, **kwargs):
    super().__init__(**kwargs)
    self.overlap_lines = overlap_lines
    self.dirty = False
```

这里新增了一个比 `FullFileWatcher` 多出来的参数：

- `overlap_lines`

它的作用是：  
在判断尾部追加时，不是只从最后一个 chunk 的结尾开始重算，而是向前多回退几行，给 chunk overlap 留出安全边界。

这是一种“宁可多算一点，也别算漏”的策略。

## 4. `_build_file_metadata()`：读取新文件状态

```python
@staticmethod
async def _build_file_metadata(path: str) -> FileMetadata:
```

这个函数和 `FullFileWatcher` 的作用类似，也是：

- 读取文件 stat
- 读取文件全文
- 构造 `FileMetadata`

区别主要在实现细节：

```python
stat_t = os.stat(path)
with open(path, "r", encoding="utf-8") as f:
    content_t = f.read()
```

最后也通过 `asyncio.to_thread(...)` 放到线程里执行。

## 5. `_find_cutoff_line()`：判断能不能做增量更新

```python
def _find_cutoff_line(
    self,
    old_chunks: list[MemoryChunk],
    old_file_meta: FileMetadata,
    new_file_meta: FileMetadata,
) -> int | None:
```

这是 `DeltaFileWatcher` 最关键的判断函数。

它的目标是回答一个问题：

**这个修改是否足够像“尾部追加”？如果是，从哪一行开始重新计算最安全？**

返回值：

- `int`
  - 表示可以增量更新，返回 cutoff line
- `None`
  - 表示不适合增量更新，应退回全量重建

### 5.1 没有旧 chunk，无法做增量

```python
if not old_chunks:
    return None
```

因为增量更新必须建立在“已有历史索引”的基础上。

### 5.2 文件变小，直接判定不是 append-only

```python
if new_file_meta.size < old_file_meta.size:
    return None
```

文件都缩小了，显然不是尾部追加。

### 5.3 增长太小，也保守地认为不是 append-only

```python
size_growth = new_file_meta.size - old_file_meta.size
if size_growth < 10:
    return None
```

这里是一个启发式规则：  
如果只增长了很少字节，可能是微小编辑，不一定真的是追加内容，所以保守地走全量更新。

### 5.4 用“旧内容是否还是新内容前缀”做近似验证

它没有真的重建完整旧文件内容再做精确比较，而是用了一个轻量 heuristic：

1. 拿旧的第一个 chunk
2. 从新文件中取出对应行区间
3. 比较两者内容是否大部分仍然匹配

对应代码：

```python
first_chunk = old_chunks_sorted[0]
first_chunk_lines = first_chunk.text.split("\n")
new_first_lines = new_lines[first_chunk.start_line - 1 : first_chunk.end_line]
```

然后计算匹配率：

```python
matches = sum(1 for old, new in zip(first_chunk_lines, new_first_lines) if old == new)
if matches < len(first_chunk_lines) * 0.8:
    return None
```

也就是说：

- 如果前面内容变化太大
- 就不把它当 append-only

### 5.5 计算 cutoff line

如果通过了前面判断，就认为文件“看起来像尾部追加”。

接着取旧 chunk 中结束最靠后的一个 chunk：

```python
last_chunk = max(old_chunks_sorted, key=lambda c: c.end_line)
cutoff_line = max(1, last_chunk.end_line - self.overlap_lines)
```

这个 cutoff line 不是最后一行，而是会向前回退 `overlap_lines` 行。  
目的是让 chunk overlap 影响到的边界部分重新切块。

## 6. `_extract_content_from_line()`：截取 cutoff 之后的新内容

```python
@staticmethod
def _extract_content_from_line(content: str, start_line: int) -> str:
```

这个函数很直接：

- 输入整份文件内容
- 输入起始行号
- 返回从该行开始到文件末尾的文本

它服务于增量更新场景：  
既然只想重算 cutoff 之后的内容，就先把这一段截出来。

## 7. `_on_changes()`：增量同步主流程

```python
async def _on_changes(self, changes: set[tuple[Change, str]]):
```

这是 `DeltaFileWatcher` 的核心逻辑。

和 `FullFileWatcher` 一样，先：

```python
self.dirty = True
```

然后逐条处理文件变化。

### 7.1 `Change.added`：新文件仍然走全量处理

```python
if change_type == Change.added:
```

新文件没有旧索引可参考，所以没法做增量。  
这里逻辑基本等同于 `FullFileWatcher` 的新增处理：

1. 读取整文件
2. `chunk_markdown()`
3. `get_chunk_embeddings()`
4. `upsert_file()`

也就是说：

- `DeltaFileWatcher` 只在“修改已有文件”时尝试增量
- 对新增文件仍然是整份建索引

### 7.2 `Change.modified`：修改文件时尝试增量

这一段是整个文件最重要的部分。

#### 第一步：取旧索引状态

```python
old_chunks = await self.file_store.get_file_chunks(path, MemorySource.MEMORY)
old_file_meta = await self.file_store.get_file_metadata(path, MemorySource.MEMORY)
```

这里能看出 `DeltaFileWatcher` 和 `FullFileWatcher` 的本质区别：

- `FullFileWatcher` 不关心旧状态
- `DeltaFileWatcher` 必须先拿旧 chunk 和旧 file metadata，才能判断能不能增量更新

#### 第二步：读取新文件

```python
file_meta = await self._build_file_metadata(path)
```

#### 第三步：如果旧索引不存在，退回全量更新

```python
if not old_chunks or not old_file_meta:
```

说明当前存储里没有这个文件的历史索引，这时无法增量更新，只能：

- 删除旧数据
- 整份重新 chunk
- 整份重新 upsert

#### 第四步：尝试找 cutoff line

```python
old_chunks_sorted = sorted(old_chunks, key=lambda c: c.start_line)
cutoff_line = self._find_cutoff_line(old_chunks_sorted, old_file_meta, file_meta)
```

这是增量更新与否的判断分叉点。

### 7.3 `cutoff_line is None`：不能增量，退回全量更新

```python
if cutoff_line is None:
```

说明当前修改不适合启发式增量处理，于是直接走整份重建。

这段逻辑和 `FullFileWatcher` 很像：

- 整文件 `chunk_markdown`
- 生成 embedding
- `delete_file`
- `upsert_file`

也就是说，`DeltaFileWatcher` 并不是完全替代全量策略，而是：

**能增量就增量，不能增量就回退到 full update。**

### 7.4 `cutoff_line` 存在：执行真正的增量更新

```python
else:
    new_content_part = self._extract_content_from_line(file_meta.content, cutoff_line)
```

#### 第一步：只截取 cutoff 之后的内容

这一段就是“只重算变化尾部”的核心。

#### 第二步：只对这一段重新切 chunk

```python
new_chunks = (
    chunk_markdown(
        new_content_part,
        file_meta.path,
        MemorySource.MEMORY,
        self.chunk_tokens,
        self.chunk_overlap,
    )
    or []
)
```

注意，这里切出来的 chunk 行号是相对于“截取后的局部文本”的，不是原文件真实行号。

#### 第三步：修正 chunk 的行号和 ID

```python
for idx, chunk in enumerate(new_chunks):
    chunk.start_line += cutoff_line - 1
    chunk.end_line += cutoff_line - 1
    chunk.id = hash_text(
        f"{chunk.source}:{chunk.path}:{chunk.start_line}:"
        f"{chunk.end_line}:{chunk.hash}:{idx}",
    )
```

这是增量方案里非常关键的一步。

因为：

- 重新切块的文本只是“局部内容”
- 必须把 chunk 的 `start_line` / `end_line` 映射回原文件真实行号
- 同时重新生成 chunk ID，避免和旧 chunk 混淆

#### 第四步：生成新 chunk 的 embedding

```python
new_chunks = await self.file_store.get_chunk_embeddings(new_chunks)
```

#### 第五步：找出哪些旧 chunk 需要删掉

```python
chunks_to_delete = [c.id for c in old_chunks_sorted if c.start_line >= cutoff_line]
```

含义是：

- cutoff 之前的 chunk 认为仍然有效
- cutoff 及之后的旧 chunk 都认为被影响了，要删掉

#### 第六步：应用增量更新

```python
if chunks_to_delete:
    await self.file_store.delete_file_chunks(path, chunks_to_delete)

if new_chunks:
    await self.file_store.upsert_chunks(new_chunks, MemorySource.MEMORY)
```

这里和全量更新最大的区别是：

- 不再删除整份文件
- 只删除受影响的旧 chunks
- 只写入新的尾部 chunks

#### 第七步：更新文件级 metadata

```python
new_chunk_count = len(old_chunks) - len(chunks_to_delete) + len(new_chunks)
file_meta.chunk_count = new_chunk_count
await self.file_store.update_file_metadata(file_meta, MemorySource.MEMORY)
```

因为现在不是整份 `upsert_file()`，所以文件级 metadata 也要单独更新。

这里尤其更新的是：

- 最新文件 hash
- mtime
- size
- `chunk_count`

### 7.5 `Change.deleted`：删除文件

```python
elif change_type == Change.deleted:
    await self.file_store.delete_file(path, MemorySource.MEMORY)
```

和 `FullFileWatcher` 一样，文件被删除时，直接把对应索引删掉。

### 7.6 收尾

最后统一：

```python
self.dirty = False
```

表示这批变化已经处理完成。

## 8. 对 `delta_file_watcher.py` 的整体理解

这个文件的核心思想是：

- 对新增文件：全量建索引
- 对修改文件：
  - 先判断是不是 append-only
  - 如果不是，退回全量重建
  - 如果是，只重算尾部受影响区域

所以它不是“精确 diff 引擎”，而是一个**面向 append-only 场景的启发式增量同步器**。

它的价值主要在于：

- 减少无意义的 embedding 重算
- 避免日志类文件每次增长都整份重建

但代价是逻辑明显比 `FullFileWatcher` 更复杂，也更依赖启发式判断。

## 9. 三个文件放在一起怎么理解

如果把这三个文件合起来看，关系可以总结成：

- `BaseFileWatcher`
  - 定义 watcher 生命周期和事件分发骨架

- `FullFileWatcher`
  - 提供“整文件重建索引”的具体策略

- `DeltaFileWatcher`
  - 提供“尽量做尾部增量更新，必要时退回全量重建”的具体策略

也就是说：

- 基类负责“框架”
- 子类负责“策略”

这是一个很典型的模板方法模式风格设计。

# awatch的changes是什么

这里的 `changes`，拿到的是 **一批文件变化事件的集合**。

从类型注解和后面的用法可以直接看出来，它的结构是：

```python
set[tuple[Change, str]]
```

也就是：

- 一个 `set`
- 里面每个元素都是一个二元组：`(变化类型, 文件路径)`

在这个文件里，后续处理函数也是按这个结构写的：

```212:215:reme/core/file_watcher/base_file_watcher.py
async def _on_changes(self, changes: set[tuple[Change, str]]):
    """Callback method to handle file changes"""

async def on_changes(self, changes: set[tuple[Change, str]]):
```

而子类里也是这么消费的：

```48:49:reme/core/file_watcher/full_file_watcher.py
for change_type, path in changes:
    if change_type in [Change.added, Change.modified]:
```

所以 `changes` 的实际内容会长得像这样：

```python
{
    (Change.added, "C:/xxx/MEMORY.md"),
    (Change.modified, "C:/xxx/memory/foo.md"),
    (Change.deleted, "C:/xxx/memory/bar.md"),
}
```

含义分别是：

- `Change.added`
  - 新增了一个文件
- `Change.modified`
  - 文件内容被修改了
- `Change.deleted`
  - 文件被删除了

再结合 `debounce=self.debounce` 来看，这个 `changes` 一般不是“单个变化”，而是 **在一个去抖时间窗口内聚合出来的一批变化**。  
也就是说，一次 `async for` 迭代里，可能同时拿到多个文件的变化。

例如，用户一次保存操作或批量修改后，可能得到：

```python
{
    (Change.modified, "C:/proj/.reme/MEMORY.md"),
    (Change.modified, "C:/proj/.reme/memory/task1.md"),
}
```

还有一点要注意：

这里传给 `awatch()` 的是：

```python
watch_filter=self.watch_filter
```

所以 `changes` 里通常只会包含**通过过滤器的文件**。  
比如当前配置只监听 `.md`，那大概率不会看到 `.json`、`.py` 之类文件出现在这个集合里。

一句话总结：

**这里的 `changes` 是 `watchfiles.awatch()` 在一个轮询/去抖周期内产出的一组文件变化事件，每个元素都是 `(Change.xxx, path)`。**

# chunk_markdown

这个函数的核心目标可以先一句话概括：

**把一段 markdown 文本按“近似 token 大小”切成多个 `MemoryChunk`，并保留行号、重叠区间、chunk id 和 hash。**

我按代码顺序给你拆。

## 1. 函数签名：输入什么，输出什么

```8:14:reme/core/utils/chunking_utils.py
def chunk_markdown(
    text: str,
    path: str,
    source: MemorySource,
    chunk_tokens: int,
    overlap: int,
) -> list[MemoryChunk]:
```

这几个参数的含义是：

- `text`
  - 要切分的全文内容
- `path`
  - 这段内容来自哪个文件
- `source`
  - 这批 chunk 的来源标签，比如 `MemorySource.MEMORY`
- `chunk_tokens`
  - 每个 chunk 允许的最大大小，单位是“近似 token”
- `overlap`
  - 相邻 chunk 之间要保留多少“重叠内容”，也是近似 token

返回值是：

- `list[MemoryChunk]`
  - 一组切好的 chunk 对象

## 2. 第一段：先把全文按行拆开

```28:30:reme/core/utils/chunking_utils.py
lines = text.split("\n")
if not lines:
    return []
```

这里做的事情很基础：

- 先按换行符把全文拆成 `lines`
- 如果没有内容，就直接返回空列表

说明这个函数的切分单位，底层是**按行推进**的，不是按句子或 markdown heading 语义切分。

## 3. 第二段：把 token 数近似换算成字符数

```32:34:reme/core/utils/chunking_utils.py
# Convert tokens to characters (~1 token = 4 chars)
max_chars = max(32, chunk_tokens * 4)
overlap_chars = max(0, overlap * 4)
```

这是这个函数很关键的一点：

它**没有真的做 tokenizer 级别切分**，而是用了一个近似规则：

- `1 token ≈ 4 chars`

所以：

- `chunk_tokens` 被换算成 `max_chars`
- `overlap` 被换算成 `overlap_chars`

比如：

- `chunk_tokens=400`
- 那 `max_chars ≈ 1600`

这里还有两个保护：

- `max_chars` 至少是 32
- `overlap_chars` 至少是 0

也就是说，这个函数的大小控制本质上是**基于字符数的近似 token 控制**。

## 4. 第三段：初始化结果容器和“当前正在构造的 chunk”

```36:40:reme/core/utils/chunking_utils.py
chunks: list[MemoryChunk] = []

# Currently building chunk
current: list[dict] = []  # [{'line': str, 'line_no': int}]
current_chars = 0
```

这里有两个层次：

- `chunks`
  - 已经完成的 chunk 列表
- `current`
  - 当前还在拼装中的 chunk 内容

`current` 里不是直接放字符串，而是放这种结构：

```python
{"line": 某一段文本, "line_no": 原始行号}
```

这么做的目的是为了后面生成 chunk 时能保留：

- chunk 文本
- 起始行号
- 结束行号

`current_chars` 则记录当前 chunk 已经累积了多少字符。

## 5. 第四段：`flush()`，把当前缓存正式输出成一个 `MemoryChunk`

```python
42:69:reme/core/utils/chunking_utils.py
def flush():
    """Add current chunk to results list"""
    if not current:
        return

    first_entry = current[0]
    last_entry = current[-1]

    if not first_entry or not last_entry:
        return

    chunk_text = "\n".join([entry["line"] for entry in current])
    start_line = first_entry["line_no"]
    end_line = last_entry["line_no"]

    chunk_hash = hash_text(chunk_text)

    chunks.append(
        MemoryChunk(
            id=hash_text(f"{source}:{path}:{start_line}:{end_line}:{chunk_hash}:{len(chunks)}"),
            path=path,
            source=source,
            start_line=start_line,
            end_line=end_line,
            text=chunk_text,
            hash=chunk_hash,
        ),
    )
```

这个内部函数的作用是：

**把 `current` 里暂存的内容，封装成一个正式的 `MemoryChunk`，追加到 `chunks` 里。**

逐步看：

### 5.1 空 chunk 直接跳过

```python
if not current:
    return
```

### 5.2 取首尾元素，确定 chunk 的行号范围

```python
first_entry = current[0]
last_entry = current[-1]
start_line = first_entry["line_no"]
end_line = last_entry["line_no"]
```

因为 `current` 里每段都带原始行号，所以这里能直接得到：

- `start_line`
- `end_line`

### 5.3 把当前内容重新拼成文本

```python
chunk_text = "\n".join([entry["line"] for entry in current])
```

也就是把当前积累的多行/多段重新拼成最终 chunk 文本。

### 5.4 计算 chunk 内容 hash

```python
chunk_hash = hash_text(chunk_text)
```

这个 hash 反映的是 chunk 文本内容本身。

### 5.5 构造 `MemoryChunk`

这里最关键的是 `id`：

```python
id=hash_text(f"{source}:{path}:{start_line}:{end_line}:{chunk_hash}:{len(chunks)}")
```

这个 ID 不是只看文本内容，而是综合了：

- `source`
- `path`
- `start_line`
- `end_line`
- `chunk_hash`
- 当前 chunk 序号 `len(chunks)`

这样做的目的，是让 chunk ID 更稳定，也更不容易和别的来源/别的文件/别的位置冲突。

## 6. 第五段：`carry_overlap()`，保留 chunk 重叠区

```71:96:reme/core/utils/chunking_utils.py
def carry_overlap():
    """Keep overlapping part and clear the rest"""
    nonlocal current, current_chars

    if overlap_chars <= 0 or not current:
        current = []
        current_chars = 0
        return
```

这个函数的作用是：

**在一个 chunk 输出完以后，不是把 `current` 完全清空，而是保留末尾一部分内容，作为下一个 chunk 的开头。**

这就是 chunk overlap 的实现。

### 6.1 如果不需要 overlap，就直接清空

```python
if overlap_chars <= 0 or not current:
    current = []
    current_chars = 0
    return
```

### 6.2 从后往前收集末尾内容

```80:93:reme/core/utils/chunking_utils.py
acc = 0
kept = []

for j in range(len(current) - 1, -1, -1):
    entry = current[j]
    if not entry:
        continue

    acc += len(entry["line"]) + 1
    kept.insert(0, entry)

    if acc >= overlap_chars:
        break
```

这里的逻辑是：

- 从当前 chunk 的最后一段开始往前倒着拿
- 一直拿到累计字符数达到 `overlap_chars`
- 再把这些保留下来

注意它用了：

```python
kept.insert(0, entry)
```

这是为了虽然是倒序遍历，但最后保留下来的顺序仍然是正序。

### 6.3 用 overlap 内容替换当前缓存

```python
current = kept
current_chars = sum(len(entry["line"]) + 1 for entry in kept)
```

这样下一个 chunk 开始时，前面就自动带上上一块的尾部内容。

## 7. 第六段：主循环，逐行处理原文

```98:119:reme/core/utils/chunking_utils.py
for i, line in enumerate(lines):
    line_no = i + 1
```

这是整个函数的主循环，按原文一行一行往下走。

这里特意把行号记成 `i + 1`，说明函数内部使用的是**1-based 行号**。

## 8. 第七段：如果某一行太长，先把这一行拆成多个 segment

```101:108:reme/core/utils/chunking_utils.py
# Split long lines into multiple segments
segments = []
if not line:  # Empty line
    segments.append("")
else:
    # If line is too long, split by maximum character count
    for start in range(0, len(line), max_chars):
        segments.append(line[start : start + max_chars])
```

这一段很关键，因为它处理了“单行超长”的情况。

正常情况下，一行就会变成一个 segment。  
但如果某一行本身长度就超过 `max_chars`，那即使单独放一块也超限，所以必须把这一行再切碎。

所以这里的策略是：

- 空行：保留成一个空字符串 segment
- 普通行：如果不长，就是一个 segment
- 超长行：按 `max_chars` 切成多个 segment

注意这里切 segment 时：

- 行号 `line_no` 还是原来的行号
- 所以一个很长的原始行，可能会被拆成多个 segment，但它们都对应同一个 `line_no`

## 9. 第八段：把 segment 填进当前 chunk，满了就 flush

```110:119:reme/core/utils/chunking_utils.py
for segment in segments:
    line_size = len(segment) + 1  # +1 for newline

    # If adding current segment would exceed the limit, flush current chunk
    if current_chars + line_size > max_chars and current:
        flush()
        carry_overlap()

    current.append({"line": segment, "line_no": line_no})
    current_chars += line_size
```

这是实际的“装箱”逻辑。

### 9.1 先计算这一段加入后会占多少字符

```python
line_size = len(segment) + 1
```

这里加 1 是把换行也算进去。

### 9.2 如果加进去会超限，就先把当前 chunk 输出掉

```python
if current_chars + line_size > max_chars and current:
    flush()
    carry_overlap()
```

这里顺序很重要：

1. `flush()`
   - 先把当前已有内容生成一个正式 chunk
2. `carry_overlap()`
   - 再把末尾 overlap 部分保留下来，作为下一个 chunk 的起点

### 9.3 然后再把当前 segment 加进去

```python
current.append({"line": segment, "line_no": line_no})
current_chars += line_size
```

所以整体逻辑就是典型的“流式装箱”：

- 不断往当前 chunk 塞内容
- 一旦超限，就输出当前块
- 保留 overlap
- 开始装下一块

## 10. 第九段：循环结束后，把最后一个 chunk 也输出

```121:122:reme/core/utils/chunking_utils.py
# Process the final chunk
flush()
```

因为主循环里只有“快超限时才 flush”，所以最后一个 chunk 如果还没满，是不会自动输出的。  
因此这里必须手动再 `flush()` 一次。

## 11. 第十段：过滤掉纯空白 chunk

```124:124:reme/core/utils/chunking_utils.py
return [c for c in chunks if c.text.strip()]
```

最后返回时，会把只包含空白字符的 chunk 去掉。

这说明：

- 函数允许中间处理空行
- 但不希望最终产出一个“完全空白”的 chunk

## 12. 整体流程串起来

把这个函数压缩成流程图式理解，就是：

1. 把全文按行拆开
2. 把 token 限制近似换算成字符限制
3. 逐行遍历文本
4. 如果某行太长，先拆成多个 segment
5. 按字符上限把 segment 依次装进当前 chunk
6. 快超限时：
   - 先输出当前 chunk
   - 保留尾部 overlap
   - 再继续装下一个 chunk
7. 所有内容处理完后，输出最后一个 chunk
8. 返回非空 chunk 列表

## 13. 这个函数的几个关键特点

最后我帮你提炼几个最重要的设计点：

- **按行驱动**
  - 不是按段落语义、也不是按 tokenizer 精确切
- **token 只是近似**
  - 用 `token * 4 ≈ chars` 来控制 chunk 大小
- **支持 overlap**
  - 相邻 chunk 会保留一小段重复内容
- **保留原始行号**
  - 方便后续检索结果映射回源文件
- **超长单行会被继续切 segment**
  - 避免单行把 chunk 上限打爆
- **chunk ID 里带 source/path/line/hash**
  - 便于唯一标识

如果你愿意，我下一条可以继续只回答一个问题：**我把 `chunk_markdown()` 用一个具体 markdown 文本样例，手工演示一遍它是怎么切出多个 chunk 的。**

## 样例说明

可以。下面我用一个**非常小的手工例子**演示 `chunk_markdown()` 是怎么切的。

为了便于演示，我假设：

- `chunk_tokens = 5`
- 所以 `max_chars = 5 * 4 = 20`
- `overlap = 2`
- 所以 `overlap_chars = 2 * 4 = 8`

假设输入文本是这个 markdown：

```text
# Title
1234567890
abcdef
XYZ
last line
```

按行拆开后是：

1. `# Title`
2. `1234567890`
3. `abcdef`
4. `XYZ`
5. `last line`

下面开始手工跑。

## 1. 初始状态

开始时：

- `chunks = []`
- `current = []`
- `current_chars = 0`

## 2. 处理第 1 行 `# Title`

这行长度是 7，算上换行就是 8。

加入后：

- `current = [("# Title", line 1)]`
- `current_chars = 8`

还没超过 `max_chars=20`，继续。

## 3. 处理第 2 行 `1234567890`

这行长度是 10，算上换行是 11。

如果继续加入：

- 原来 `current_chars = 8`
- 新增 `line_size = 11`
- 合计 `19`

还没超过 20，所以可以直接加进去。

现在：

- `current = [("# Title", 1), ("1234567890", 2)]`
- `current_chars = 19`

## 4. 处理第 3 行 `abcdef`

这行长度 6，算上换行是 7。

如果加入：

- 原来 `19`
- 新增 `7`
- 合计 `26`

超过 `max_chars=20`，所以这时会先触发：

```python
flush()
carry_overlap()
```

### 4.1 先 flush 第一个 chunk

当前 `current` 里的内容是：

- 第 1 行 `# Title`
- 第 2 行 `1234567890`

所以第一个 chunk 变成：

```text
# Title
1234567890
```

它的属性大致是：

- `start_line = 1`
- `end_line = 2`

于是：

- `chunks = [chunk1]`

### 4.2 再 carry_overlap

现在要保留末尾约 `8 chars` 的 overlap。

当前 chunk 的末尾往前看：

- 第 2 行 `1234567890`，长度 10，加换行约 11
- 已经 >= 8，所以只保留这一行就够了

于是 `carry_overlap()` 之后：

- `current = [("1234567890", 2)]`
- `current_chars = 11`

### 4.3 再把第 3 行加进去

把 `abcdef` 加进去：

- `11 + 7 = 18`
- 没超限

现在：

- `current = [("1234567890", 2), ("abcdef", 3)]`
- `current_chars = 18`

## 5. 处理第 4 行 `XYZ`

这行长度 3，算上换行是 4。

加入后：

- `18 + 4 = 22`

超限，所以 снова先：

```python
flush()
carry_overlap()
```

### 5.1 flush 第二个 chunk

当前内容是：

```text
1234567890
abcdef
```

所以第二个 chunk 是：

- `start_line = 2`
- `end_line = 3`

现在：

- `chunks = [chunk1, chunk2]`

### 5.2 carry_overlap

需要保留末尾约 8 chars。

从后往前取：

- `abcdef` 长度 6，加换行约 7，还不够
- 再往前加上 `1234567890`，就超过 8

所以这次会保留两行：

- `1234567890`
- `abcdef`

于是：

- `current = [("1234567890", 2), ("abcdef", 3)]`
- `current_chars = 18`

注意这里会出现一个现象：  
因为 overlap 是按“字符累计”粗略回退，不是按“正好切一行”，所以有时候会多保留一整行。

### 5.3 再加入 `XYZ`

- `18 + 4 = 22`  
又超了。

这时因为 `current` 非空，代码逻辑还是会先 flush 再 carry overlap，然后再把 `XYZ` 放进去。

#### 再 flush 一次

这次 flush 出来的内容其实还是：

```text
1234567890
abcdef
```

也就是说，在某些边界情况下，**可能会产生高度重复的 chunk**。这是这个近似切块逻辑的一个自然结果。

然后 carry overlap 后，再把 `XYZ` 放进去。

最终这一轮之后，可把当前状态理解成：

- `current` 最终会以 overlap 保留的尾部内容加上 `XYZ` 继续向前推进

这个例子说明：  
这个算法很简单直接，但不是“最优最去重”的，它追求的是**稳定、朴素、保留 overlap**。

## 6. 继续处理第 5 行 `last line`

同理，`last line` 加入时会看：

- 当前 `current_chars`
- 加上这一行会不会超限

如果超限，就先 flush，再 carry overlap，再加入。

## 7. 最后 flush 收尾

所有行都处理完后，代码还会执行一次：

```python
flush()
```

把最后一个还没输出的 `current` 收尾成最终 chunk。

## 8. 这个例子里最重要的观察点

其实你不用太纠结每一步字符精确值，更应该抓住这个函数的 4 个核心行为：

1. **按行推进**
   每次处理的基本单位是“行”，不是段落语义。

2. **快超限时先输出当前 chunk**
   不是等真的塞爆再处理，而是“加下一个 segment 会超限时”，先 flush。

3. **输出后保留末尾 overlap**
   下一个 chunk 会继承上一个 chunk 的尾巴。

4. **overlap 是按字符数粗略保留，不是精确按行或语义切**
   所以有时会保留多一整行，甚至在边界情况下造成较强重复。

## 9. 再给你一个更直观的“理想化结果”

如果忽略刚才那个边界重复问题，只看这个函数想实现的目标，它希望切出类似这种 chunk：

### chunk 1

```text
# Title
1234567890
```

### chunk 2

```text
1234567890
abcdef
```

### chunk 3

```text
abcdef
XYZ
last line
```

也就是：

- 前后 chunk 有重叠
- 每个 chunk 控制在近似大小内
- 能保留上下文连续性

## 10. 你真正应该怎么理解它

这个函数不是一个“特别聪明的 markdown 语义切分器”，它更像一个：

**按行滚动、带 overlap、按字符上限近似控制大小的 chunk builder。**

如果你愿意，我下一条可以继续只回答一个问题：**我用你仓库里真实的 `chunk_markdown()` 参数（比如 `chunk_tokens=400, overlap=80`）再举一个更贴近真实运行的例子。**

