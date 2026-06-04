# ReMeLight 使用 ChromaFileStore 的逻辑梳理

本文只梳理 `ReMeLight` 这条链路里，`ChromaFileStore` 是如何被初始化、如何被调用、以及最终如何落到真正的 `chromadb` API 的。

先给结论：

- `ReMeLight` 自己不直接调用 `chromadb`
- `ReMeLight` 通过 `Application` 初始化默认 `file_store`
- 在 `light` 配置下，这个默认 `file_store` 后端是 `chroma`
- 因此实际对象是 `ChromaFileStore`
- 后续 `ReMeLight` 对记忆文件的建索引、更新、搜索，都会通过 `ChromaFileStore` 间接调用 `chromadb`

## 1. 总体调用链

最核心的总链路可以概括成下面几条：

- 初始化链路  
  `ReMeLight.__init__` -> `Application.__init__` -> 读取 `light` 配置 -> 默认 `file_store.backend=chroma`

- 启动链路  
  `await reme.start()` -> `Application.start()` -> 创建并启动 `ChromaFileStore`

- 建索引/更新链路  
  `Application.start()` -> 启动默认 `file_watcher` -> `BaseFileWatcher.start()` -> 扫描已有文件/监听文件变化 -> `FullFileWatcher._on_changes()` -> `ChromaFileStore.delete_file()` / `ChromaFileStore.upsert_file()`

- 搜索链路  
  `ReMeLight.memory_search()` -> `MemorySearch.call()` -> `self.file_store.hybrid_search()` -> `ChromaFileStore.hybrid_search()` -> `vector_search()` + `keyword_search()`

- 关闭链路  
  `await reme.close()` -> `Application.close()` -> `ChromaFileStore.close()`

## 2. ReMeLight 为什么会用到 ChromaFileStore

`ReMeLight` 初始化父类 `Application` 时，指定的是 `config_path="light"`：

```80:89:reme/reme_light.py
            enable_load_env=enable_load_env,
            parser=ReMeConfigParser,
            default_as_llm_config=default_as_llm_config,
            default_embedding_model_config=default_embedding_model_config,
            default_file_store_config=default_file_store_config,
            default_file_watcher_config=_merged_file_watcher_config,
        )
```

而 `light` 配置里默认把 `file_store` 后端设为了 `chroma`：

```27:39:reme/config/light.yaml
file_stores:
  default:
    backend: chroma
    embedding_model: default
    store_name: "reme"

file_watchers:
  default:
    backend: full
    file_store: default
    suffix_filters: [ ".md" ]
    recursive: false
```

所以，只要没有被 `default_file_store_config` 覆盖，`ReMeLight` 默认用到的就是 `ChromaFileStore`。

## 3. ReMeLight 触发 ChromaFileStore 的所有主要调用点

下面按“从启动到运行再到关闭”的顺序，整理所有被 `ReMeLight` 触发到的 `ChromaFileStore` 调用点。

### 3.1 启动时初始化 ChromaFileStore：`ChromaFileStore.start()`

`ReMeLight.start()` 本身只是调用父类启动逻辑：

```209:226:reme/reme_light.py
    async def start(self):
        """
        Start the application lifecycle.
        ...
        """
        result = await super().start()
        # Perform initial cleanup of any expired tool result files
        self._cleanup_tool_results()
        return result
```

真正初始化 `file_store` 的地方在 `Application.start()`。它会根据 `file_stores.default.backend` 选择后端，并调用对应实例的 `start()`：

```255:267:reme/core/application.py
        for name, config in self.service_config.file_stores.items():
            if config.backend not in R.file_stores:
                logger.warning(f"File store backend {config.backend} is not supported.")
            else:
                config_dict = config.model_dump(exclude={"backend", "embedding_model"})
                config_dict.update(
                    {
                        "embedding_model": self.service_context.embedding_models[config.embedding_model],
                        "db_path": working_path / "file_store",
                    },
                )
                self.service_context.file_stores[name] = R.file_stores[config.backend](**config_dict)
                await self.service_context.file_stores[name].start()
```

在 `light` 配置下，这里的 `R.file_stores[config.backend]` 就是 `ChromaFileStore`。  
所以这里实际触发的是：

- `ChromaFileStore.start()`

`ChromaFileStore.start()` 内部又会真正初始化 `chromadb.PersistentClient` 和 collection：

```110:129:reme/core/file_store/chroma_file_store.py
    async def start(self) -> None:
        """Initialize ChromaDB client and collection."""
        if self.client is not None:
            return

        # Initialize persistent ChromaDB client
        self.client = chromadb.PersistentClient(
            path=str(self.db_path),
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True,
            ),
        )

        # Get or create the chunks collection
        self.chunks_collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
```

这里是 `ReMeLight` 触达 `chromadb` 的第一个入口。

### 3.2 启动 watcher 时清空旧索引：`ChromaFileStore.clear_all()`

`Application.start()` 在启动完 `file_store` 后，还会继续启动默认 `file_watcher`：

```269:276:reme/core/application.py
        for name, config in self.service_config.file_watchers.items():
            if config.backend not in R.file_watchers:
                logger.warning(f"File watcher backend {config.backend} is not supported.")
            else:
                config_dict = config.model_dump(exclude={"backend", "file_store"})
                config_dict["file_store"] = self.service_context.file_stores[config.file_store]
                self.service_context.file_watchers[name] = R.file_watchers[config.backend](**config_dict)
                await self.service_context.file_watchers[name].start()
```

`light` 默认的 watcher 是 `full`，也就是 `FullFileWatcher`。  
而 `BaseFileWatcher.start()` 在默认参数 `rebuild_index_on_start=True` 下，会先清空旧索引：

```python
        82:88:reme/core/file_watcher/base_file_watcher.py
        async def _initialize_and_watch():
            if self.rebuild_index_on_start:
                if self.file_store is not None:
                    await self.file_store.clear_all()
                    logger.info("Cleared all indexed data on start")
                await self._scan_existing_files()
            await self._watch_loop()
```

因为这里的 `self.file_store` 就是默认 `ChromaFileStore`，所以启动时会触发：

- `ChromaFileStore.clear_all()`

它内部会删除并重建 Chroma collection：

```python
    607:620:reme/core/file_store/chroma_file_store.py
    async def clear_all(self) -> None:
        """Clear all indexed data."""
        # Delete and recreate the collection
        self.client.delete_collection(
            name=self.collection_name,
        )
        self.chunks_collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

        # Clear file metadata cache and disk
        self._metadata_cache = {}
        await self._save_metadata({})
```

### 3.3 启动时扫描已有文件并建索引：`delete_file()` / `upsert_file()`

清空旧索引之后，watcher 会扫描当前监听路径下已有的文件：

```117:148:reme/core/file_watcher/base_file_watcher.py
    async def _scan_existing_files(self):
        """Scan existing files matching watch criteria and trigger on_changes with Change.added"""
        existing_files: set[tuple[Change, str]] = set()
        ...
        if existing_files:
            logger.info(f"[SCAN_ON_START] Found {len(existing_files)} existing files matching watch criteria")
            await self.on_changes(existing_files)
            logger.info(f"[SCAN_ON_START] Added {len(existing_files)} files to memory store")
```

`ReMeLight` 在构造时默认监听的是：

- `MEMORY.md` 或 `memory.md`
- `memory/` 目录

对应代码：

```142:152:reme/reme_light.py
        _memory_md = self.working_path / "MEMORY.md"
        if not _memory_md.exists() and (self.working_path / "memory.md").exists():
            _memory_md = self.working_path / "memory.md"
        _default_watch_paths = [str(_memory_md), str(self.memory_path)]
        if default_file_watcher_config and default_file_watcher_config.get("watch_paths"):
            _merged_file_watcher_config = default_file_watcher_config
        else:
            _merged_file_watcher_config = {
                **(default_file_watcher_config or {}),
                "watch_paths": _default_watch_paths,
            }
```

扫描到文件后，`FullFileWatcher._on_changes()` 会做切块和写入：

```48:69:reme/core/file_watcher/full_file_watcher.py
        for change_type, path in changes:
            if change_type in [Change.added, Change.modified]:
                file_meta = await self._build_file_metadata(path)
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
                if chunks:
                    chunks = await self.file_store.get_chunk_embeddings(chunks)
                file_meta.chunk_count = len(chunks)

                await self.file_store.delete_file(file_meta.path, MemorySource.MEMORY)
                logger.info(f"delete_file {file_meta.path}")

                await self.file_store.upsert_file(file_meta, MemorySource.MEMORY, chunks)
                logger.info(f"Upserted {file_meta.chunk_count} chunks for {file_meta.path}")
```

这里会触发两个 `ChromaFileStore` 方法：

- `ChromaFileStore.delete_file()`
- `ChromaFileStore.upsert_file()`

其中 `upsert_file()` 会真正把 chunk 写入 Chroma：

```176:182:reme/core/file_store/chroma_file_store.py
        self.chunks_collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
```

也就是说，`ReMeLight` 在启动后会立刻把当前 `MEMORY.md` / `memory/*.md` 的内容切成 chunk，并交给 `ChromaFileStore` 写入 Chroma。

### 3.4 启动扫描完成后读取已有索引：`list_files()` / `get_file_chunks()`

`BaseFileWatcher._scan_existing_files()` 在完成初始写入后，还会回头检查当前索引里有哪些文件，以及每个文件对应多少 chunk：

```152:156:reme/core/file_watcher/base_file_watcher.py
        if self.file_store is not None:
            files: list[str] = await self.file_store.list_files(MemorySource.MEMORY)
            for file_path in files:
                chunks = await self.file_store.get_file_chunks(file_path, MemorySource.MEMORY)
                logger.info(f"Found existing file: {file_path}, {len(chunks)} chunks")
```

所以这里又会触发两个 `ChromaFileStore` 方法：

- `ChromaFileStore.list_files()`
- `ChromaFileStore.get_file_chunks()`

这一步更偏“校验/恢复当前索引状态”，不是用户主动搜索，但它确实是 `ReMeLight` 启动链路里对 `ChromaFileStore` 的真实调用点。

### 3.5 运行中监控文件变化并更新索引：`delete_file()` / `upsert_file()`

除了启动时全量扫描，watcher 进入 `_watch_loop()` 之后，还会持续监控文件变化：

```186:199:reme/core/file_watcher/base_file_watcher.py
                async for changes in awatch(
                    *valid_paths,
                    force_polling=True,
                    watch_filter=self.watch_filter,
                    recursive=self.recursive,
                    debounce=self.debounce,
                    poll_delay_ms=self.poll_delay_ms,
                    stop_event=self._stop_event,
                ):
                    if self._stop_event.is_set():
                        break

                    await self.on_changes(changes)
```

而 `FullFileWatcher._on_changes()` 在处理新增/修改/删除时，会继续调用：

- 新增或修改文件  
  `ChromaFileStore.delete_file()` -> `ChromaFileStore.upsert_file()`

- 删除文件  
  `ChromaFileStore.delete_file()`

对应代码：

```71:73:reme/core/file_watcher/full_file_watcher.py
            elif change_type == Change.deleted:
                await self.file_store.delete_file(path, MemorySource.MEMORY)
                logger.info(f"Deleted {path}")
```

所以在运行期，`ReMeLight` 对 Chroma 的持续使用，主要体现在“文件变化即重建对应文件的 chunk 索引”。

### 3.6 执行语义搜索：`hybrid_search()` -> `vector_search()` / `keyword_search()`

`ReMeLight` 对 Chroma 的另一条主链路是 `memory_search()`。

入口在 `ReMeLight.memory_search()`：

```784:796:reme/reme_light.py
        search_tool = MemorySearch(
            vector_weight=self.vector_weight,
            candidate_multiplier=self.candidate_multiplier,
        )

        # Execute the search with validated parameters
        search_result = await search_tool.call(
            query=query,
            max_results=max_results,
            min_score=min_score,
            service_context=self.service_context,
        )
```

`MemorySearch` 内部直接调用默认 `file_store` 的 `hybrid_search()`：

```82:89:reme/memory/file_based/tools/memory_search.py
        results = await self.file_store.hybrid_search(
            query=query,
            limit=max_results,
            sources=self.sources,
            vector_weight=self.vector_weight,
            candidate_multiplier=self.candidate_multiplier,
        )
```

因为这里的默认 `file_store` 就是 `ChromaFileStore`，所以会触发：

- `ChromaFileStore.hybrid_search()`

而 `hybrid_search()` 在向量检索和关键词检索都启用时，会继续调用：

- `ChromaFileStore.keyword_search()`
- `ChromaFileStore.vector_search()`

对应代码：

```533:568:reme/core/file_store/chroma_file_store.py
        if self.vector_enabled and self.fts_enabled:
            keyword_results = await self.keyword_search(query, candidates, sources)
            vector_results = await self.vector_search(query, candidates, sources)
            ...
            else:
                merged = self._merge_hybrid_results(
                    vector=vector_results,
                    keyword=keyword_results,
                    vector_weight=vector_weight,
                    text_weight=text_weight,
                )
                ...
                return merged[:limit]
```

其中：

- `vector_search()` 最终调用的是 Chroma 的 `collection.query(...)`
- `keyword_search()` 最终调用的是 Chroma 的 `collection.get(..., where_document=...)`

对应代码如下。

向量检索：

```357:364:reme/core/file_store/chroma_file_store.py
            results = self.chunks_collection.query(
                query_embeddings=[query_embedding],
                n_results=limit,
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
```

关键词检索：

```466:470:reme/core/file_store/chroma_file_store.py
        results = self.chunks_collection.get(
            where=where_filter,
            where_document=where_document,
            include=["documents", "metadatas"],
        )
```

所以，对 `ReMeLight` 来说，真正的“搜索 Chroma”不是直接调 `chromadb`，而是：

`ReMeLight.memory_search()` -> `MemorySearch` -> `ChromaFileStore.hybrid_search()` -> `chromadb`

### 3.7 关闭时释放/持久化：`ChromaFileStore.close()`

`ReMeLight.close()` 还是走父类关闭逻辑：

```228:244:reme/reme_light.py
    async def close(self) -> bool:
        """
        Close the application and perform cleanup.
        ...
        """
        # Final cleanup of expired tool result files before shutdown
        self._cleanup_tool_results()
        return await super().close()
```

而 `Application.close()` 会关闭所有 `file_store`：

```521:523:reme/core/application.py
        for name, file_store in self.service_context.file_stores.items():
            logger.info(f"Closing file store: {name}")
            await file_store.close()
```

在 `light` 配置下，这里实际调用的是：

- `ChromaFileStore.close()`

它会先保存本地文件级 metadata 缓存，然后释放客户端引用：

```624:633:reme/core/file_store/chroma_file_store.py
    async def close(self) -> None:
        """Close ChromaDB client and release resources."""
        # Persist metadata cache to disk before closing
        if self._metadata_cache:
            await self._save_metadata(self._metadata_cache)

        # ChromaDB PersistentClient handles persistence automatically
        self.client = None
        self.chunks_collection = None
        await super().close()
```

## 4. ReMeLight 真实会触发到的 ChromaFileStore 方法清单

按当前默认 `light` 配置，`ReMeLight` 会直接或间接触发到这些 `ChromaFileStore` 方法：

- `start()`
  
  - 在 `Application.start()` 中初始化 Chroma client 和 collection

- `clear_all()`
  
  - 在 `BaseFileWatcher.start()` 的初始重建索引阶段触发

- `delete_file()`
  
  - 在 `FullFileWatcher._on_changes()` 里，处理新增/修改/删除文件时触发

- `upsert_file()`
  
  - 在 `FullFileWatcher._on_changes()` 里，处理新增/修改文件时触发

- `list_files()`
  
  - 在 `BaseFileWatcher._scan_existing_files()` 里，扫描完成后读取当前索引文件列表

- `get_file_chunks()`
  
  - 在 `BaseFileWatcher._scan_existing_files()` 里，读取某个文件已索引的 chunk

- `hybrid_search()`
  
  - 在 `ReMeLight.memory_search()` 中通过 `MemorySearch` 触发

- `vector_search()`
  
  - 由 `hybrid_search()` 内部调用

- `keyword_search()`
  
  - 由 `hybrid_search()` 内部调用

- `close()`
  
  - 在 `Application.close()` 中触发

## 5. ReMeLight 没有直接调用，但 ChromaFileStore 内部会进一步调用的 chromadb API

虽然 `ReMeLight` 只面向 `ChromaFileStore`，但在 `ChromaFileStore` 内部，最终会落到这些真正的 `chromadb` API：

- `chromadb.PersistentClient(...)`
  
  - 初始化本地持久化 Chroma 客户端

- `client.get_or_create_collection(...)`
  
  - 创建或获取 collection

- `client.delete_collection(...)`
  
  - 重建索引时删除旧 collection

- `chunks_collection.upsert(...)`
  
  - 写入/更新 chunk

- `chunks_collection.get(...)`
  
  - 读取 chunk、关键词搜索

- `chunks_collection.query(...)`
  
  - 执行向量搜索

- `chunks_collection.delete(...)`
  
  - 删除某个文件的 chunk

## 6. 这份调用图可以怎么理解

如果只保留最关键的一句话，可以这样记：

`ReMeLight` 不直接碰 `chromadb`；它通过默认 `file_store=ChromaFileStore` 把 `MEMORY.md` 和 `memory/*.md` 建索引到 Chroma，并通过 `memory_search()` 再从 Chroma 做混合检索。

再压缩成最核心的两条线：

- 写入线  
  `ReMeLight.start()` -> `file_watcher` -> `ChromaFileStore.upsert_file()` -> `chromadb.upsert()`

- 搜索线  
  `ReMeLight.memory_search()` -> `ChromaFileStore.hybrid_search()` -> `chromadb.query()/get()`

# FullFileWatcher 和 BaseFileWatcher

可以这样理解，但要加一个限定，不然会稍微说过头。

**在“文件建索引 / 文件更新”这条链路里，你的理解基本对：**

- `ChromaFileStore` 更像一个**底层存储能力层**
  - 负责 `start / clear_all / delete_file / upsert_file / list_files / get_file_chunks / search`
  - 本身不负责“什么时候扫描文件、什么时候重建、什么时候同步”
- `BaseFileWatcher` / `FullFileWatcher` 更像**索引同步调度层**
  - `BaseFileWatcher` 负责生命周期和总流程：启动、初始扫描、持续监听
  - `FullFileWatcher` 负责具体同步策略：文件变了以后怎么读、怎么切 chunk、怎么调用 `file_store`

所以如果只讨论“**memory 文件怎么被同步进 Chroma**”，可以这么说：

**`ChromaFileStore` 负责提供存储能力，`BaseFileWatcher` 和 `FullFileWatcher` 负责决定何时、以什么方式调用这些能力。**

但如果把范围放大到整个 `ReMeLight` 使用 Chroma 的所有功能，这句话还要再修正一下：

**`FullFileWatcher` / `BaseFileWatcher` 不是“真正提供功能的唯一一层”，它们主要负责“索引构建与更新”；而“检索功能”是由 `ReMeLight.memory_search()` + `MemorySearch` + `ChromaFileStore.hybrid_search()` 这条链路共同完成的。**

也就是说可以拆成两类：

1. **建索引/更新索引**
   - 谁决定怎么执行：`BaseFileWatcher` + `FullFileWatcher`
   - 谁负责落库：`ChromaFileStore`

2. **搜索**
   - 谁决定要不要搜、搜什么：`ReMeLight.memory_search()` / `MemorySearch`
   - 谁负责真正执行检索：`ChromaFileStore`

所以最准确的说法是：

**`ChromaFileStore` 是底层能力层，不主导文件同步时机；`BaseFileWatcher` 和 `FullFileWatcher` 主导的是“索引同步”这件事；但在“搜索”这件事上，执行主体仍然是 `ChromaFileStore`。**
