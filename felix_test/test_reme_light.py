"""
test_reme_light.py - ReMeLight memory 文件操作相关接口测试脚本

【覆盖的接口】
  - ReMeLight.__init__() + start()    初始化 & 启动
  - compact_memory()                  压缩历史对话为摘要字符串（in-memory，不写文件）
  - summary_memory()                  将摘要写入 memory/YYYY-MM-DD.md（写文件）
  - await_summary_tasks()             等待后台 summary 任务全部落盘（确保 memory 文件写完）
  - memory_search()                   语义检索 MEMORY.md + memory/*.md
  - close()                           关闭并清理资源

【await_summary_tasks() 说明】
  pre_reasoning_hook() 内部会把 summary_memory() 丢到后台异步执行（不阻塞主流程）。
  await_summary_tasks() 的作用是：等待所有后台 summary 任务全部写完 memory 文件。
  本测试脚本直接同步调用 summary_memory()，不产生后台任务，
  所以 await_summary_tasks() 会立即返回（演示接口调用方式）。

所有调用均以同步方式执行（通过 S() 辅助函数 + 单一 event loop），方便断点调试。
"""

import asyncio
from pathlib import Path

from agentscope.message import Msg
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


# =============================================================================
# 测试用对话消息构造（纯文字对话，无工具调用）
# =============================================================================

def make_all_messages() -> list[Msg]:
    """构造一段模拟对话历史。"""
    return [
        Msg(
            name="user",
            role="user",
            content=(
                "你好，我正在用 Python 开发一个 AI 助手项目，"
                "使用 FastAPI 作为后端框架，前端是 React。"
                "项目名叫 SmartAssist，目标是帮助用户管理日程和待办事项。"
            ),
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=(
                "好的！SmartAssist 听起来很有潜力。"
                "FastAPI + React 是很流行的组合。"
                "请问你目前遇到了什么具体问题，或者需要我帮你设计哪部分？"
            ),
        ),
        Msg(
            name="user",
            role="user",
            content=(
                "我在做用户认证模块，使用 JWT token。"
                "目前遇到的问题是：token 过期后，前端没有自动刷新，"
                "用户需要重新登录，体验很差。"
                "我希望实现 silent refresh（静默刷新）机制。"
            ),
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=(
                "Silent refresh 是个常见需求。标准做法是：\n"
                "1. 使用 access token（短有效期，如 15 分钟）+ refresh token（长有效期，如 7 天）\n"
                "2. 前端在 access token 快过期前（比如剩余 1 分钟时）自动调用 /auth/refresh 接口\n"
                "3. 后端验证 refresh token，颁发新的 access token\n"
                "4. refresh token 本身也支持滚动刷新（rotating refresh token）\n\n"
                "FastAPI 里我建议用 python-jose 做 JWT，配合 HTTPOnly Cookie 存储 refresh token "
                "来防止 XSS 攻击。需要我给你写具体的代码示例吗？"
            ),
        ),
        Msg(
            name="user",
            role="user",
            content=(
                "好的，请帮我写 FastAPI 的 JWT 认证代码，"
                "包括 login、refresh、logout 三个接口。"
                "用户信息存在 PostgreSQL 里，用 SQLAlchemy ORM。"
                "密码用 bcrypt 加密。"
            ),
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=(
                "以下是完整的 FastAPI JWT 认证实现：\n\n"
                "```python\n"
                "# auth/router.py\n"
                "from datetime import datetime, timedelta\n"
                "from fastapi import APIRouter, Depends, HTTPException, Response, Cookie\n"
                "from jose import jwt, JWTError\n"
                "from passlib.context import CryptContext\n"
                "from sqlalchemy.orm import Session\n\n"
                "SECRET_KEY = 'your-secret-key'\n"
                "ALGORITHM = 'HS256'\n"
                "ACCESS_TOKEN_EXPIRE_MINUTES = 15\n"
                "REFRESH_TOKEN_EXPIRE_DAYS = 7\n\n"
                "pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')\n"
                "router = APIRouter(prefix='/auth', tags=['auth'])\n\n"
                "def create_access_token(data: dict) -> str:\n"
                "    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)\n"
                "    return jwt.encode({**data, 'exp': expire}, SECRET_KEY, algorithm=ALGORITHM)\n\n"
                "# ... 完整代码含 login / refresh / logout ...\n"
                "```\n\n"
                "使用 HTTPOnly Cookie 存储 refresh token，防止 XSS 攻击。"
            ),
        ),
        Msg(
            name="user",
            role="user",
            content=(
                "非常感谢！另外我想问一下数据库连接池的配置，"
                "我们预计同时在线用户 500 人，应该怎么设置 pool_size 和 max_overflow？"
            ),
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=(
                "对于 500 并发用户，建议 SQLAlchemy 连接池配置如下：\n\n"
                "```python\n"
                "engine = create_engine(\n"
                "    DATABASE_URL,\n"
                "    pool_size=20,        # 核心连接数\n"
                "    max_overflow=30,     # 超出 pool_size 后最多额外开多少\n"
                "    pool_timeout=30,     # 等待连接超时秒数\n"
                "    pool_recycle=1800,   # 连接存活 30 分钟后回收，避免 MySQL 8 小时断开\n"
                "    pool_pre_ping=True,  # 使用前检查连接是否存活\n"
                ")\n"
                "```\n\n"
                "同时建议在 PostgreSQL 侧设置 max_connections=200，"
                "并配合 PgBouncer 做连接池代理，进一步提升并发能力。"
            ),
        ),
    ]


# =============================================================================
# 分节打印辅助
# =============================================================================

def section(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


# =============================================================================
# 主测试流程（同步风格）
# =============================================================================

def main():
    # =========================================================================
    # 1. 初始化 ReMeLight
    # =========================================================================
    section("Step 1: 初始化 ReMeLight")

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

    print(f"  working_path     = {reme.working_path}")
    print(f"  memory_path      = {reme.memory_path}")
    print(f"  tool_result_path = {reme.tool_result_path}")
    print(f"  dialog_path      = {reme.dialog_path}")
    print(f"  vector_weight    = {reme.vector_weight}")
    print(f"  candidate_multiplier = {reme.candidate_multiplier}")

    # =========================================================================
    # 2. start() - 启动文件存储、文件监控、embedding 缓存；清理过期 tool_result 文件
    # =========================================================================
    section("Step 2: start() - 启动记忆系统")

    S(reme.start())
    print("  记忆系统已启动")

    # =========================================================================
    # 3. 准备测试消息，直接分成"待压缩"和"保留"两组
    #    不调用 check_context()，手动指定哪些消息需要压缩
    # =========================================================================
    section("Step 3: 构建测试消息，手动划分待压缩 / 保留")

    all_messages = make_all_messages()
    print(f"  共 {len(all_messages)} 条消息")
    for i, msg in enumerate(all_messages):
        preview = str(msg.content)[:60].replace("\n", " ")
        print(f"  [{i}] role={msg.role:9s}  {preview}...")

    # 手动指定：前 6 条是历史消息（待压缩），后 2 条是近期消息（保留）
    messages_to_compact = all_messages[:6]   # 需要压缩的历史消息
    messages_to_keep    = all_messages[6:]   # 保留在上下文中的近期消息

    print(f"\n  messages_to_compact 数量 = {len(messages_to_compact)}")
    print(f"  messages_to_keep    数量 = {len(messages_to_keep)}")

    # =========================================================================
    # 4. compact_memory() - 压缩历史对话为结构化摘要字符串（in-memory，不写文件）
    # =========================================================================
    section("Step 4: compact_memory() - 压缩对话为摘要字符串（不写文件）")

    compact_summary = S(reme.compact_memory(
        messages=messages_to_compact,      # 待压缩的历史消息
        as_llm="default",                  # 使用默认 LLM（初始化时配置的 model_name）
        as_llm_formatter="default",        # 使用默认 formatter
        as_token_counter="default",        # 使用默认 token 计数器
        language="zh",                     # 摘要语言：zh = 中文
        max_input_length=128 * 1024,       # 模型上下文窗口（128K tokens）
        compact_ratio=0.7,                 # 送给 LLM 的消息上限 = max_input_length × 0.7 × 0.95，超出部分（更老的消息）直接丢弃
        previous_summary="",              # 上轮摘要（首次为空字符串）
        return_dict=False,                 # False = 返回字符串；True = 返回 dict
        add_thinking_block=True,           # 生成摘要前加入思考步骤，提升摘要质量
        extra_instruction="",              # 附加指令（空 = 使用默认行为）
    ))

    print(f"  compact_summary 类型：{type(compact_summary).__name__}")
    print(f"  compact_summary 长度：{len(str(compact_summary))} 字符")
    print(f"\n  compact_summary 内容预览：\n{str(compact_summary)[:600]}")

    # =========================================================================
    # 5. summary_memory() - 将对话摘要写入 memory/YYYY-MM-DD.md
    # =========================================================================
    section("Step 5: summary_memory() - 将摘要写入 memory/YYYY-MM-DD.md")

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

    print(f"  summary_result 长度：{len(str(summary_result))} 字符")
    print(f"\n  summary_result 内容预览：\n{str(summary_result)[:400]}")

    # 验证 memory 文件是否已写入磁盘
    memory_files = list(reme.memory_path.glob("*.md"))
    print(f"\n  memory/ 目录下的文件：{[f.name for f in memory_files]}")

    # =========================================================================
    # 6. await_summary_tasks() - 等待后台 summary 任务全部落盘
    #    说明：本脚本直接同步调用 summary_memory()，不产生后台任务。
    #    此处演示接口调用方式（会立即返回，pending_tasks = 0）。
    #    实际使用场景：pre_reasoning_hook() 会启动后台任务，
    #    在 agent 关闭前调用此接口确保 memory 文件全部写完。
    # =========================================================================
    section("Step 6: await_summary_tasks() - 等待后台 summary 任务落盘")

    pending_count = len(reme.summary_tasks)
    print(f"  当前 pending summary_tasks 数：{pending_count}（同步调用 summary_memory 不产生后台任务）")

    tasks_result = S(reme.await_summary_tasks())
    print(f"  await_summary_tasks() 返回：{tasks_result!r}")

    # =========================================================================
    # 7. memory_search() - 语义检索 MEMORY.md + memory/*.md
    # =========================================================================
    section("Step 7: memory_search() - 语义检索 memory 文件")

    search_result = S(reme.memory_search(
        query="JWT token 刷新机制",    # 搜索查询语句
        max_results=5,                 # 最多返回 5 条结果
        min_score=0.1,                 # 最低相似度阈值（低于此分数的结果被过滤）
    ))

    print(f"  search_result 类型：{type(search_result).__name__}")
    print(f"  search_result 内容：")
    for block in search_result.content:
        print(f"\n{block['text'][:600]}")

    # 换一个查询，验证不同主题的检索
    search_result2 = S(reme.memory_search(
        query="数据库连接池 pool_size 配置",
        max_results=5,
        min_score=0.1,
    ))
    print(f"\n  第二次搜索（数据库连接池）结果：")
    for block in search_result2.content:
        print(f"\n{block['text'][:600]}")

    # =========================================================================
    # 8. close() - 关闭记忆系统，清理 tool_result 文件，停止文件监控，保存 embedding 缓存
    # =========================================================================
    section("Step 8: close() - 关闭记忆系统")

    closed_ok = S(reme.close())
    print(f"  close() 返回值：{closed_ok}")
    print("  记忆系统已关闭")

    # =========================================================================
    # 完成：列出 working_dir 下所有生成的文件
    # =========================================================================
    section("全部测试步骤执行完毕")
    working_path = Path("felix_test/.reme_test")
    print(f"  working_dir 内容（{working_path}）：")
    for f in sorted(working_path.rglob("*")):
        if f.is_file():
            size = f.stat().st_size
            print(f"    {f.relative_to(working_path)}  ({size} bytes)")


# =============================================================================
# 入口
# =============================================================================
if __name__ == "__main__":
    main()
