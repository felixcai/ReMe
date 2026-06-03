"""
test_reme_subclass.py - 继承 ReMeLight，暴露自定义 prompt 路径

【演示目的】
  继承 ReMeLight，在构造函数里暴露 compactor_prompt_path 和 summarizer_prompt_path，
  使外部可以指定自定义的 prompt yaml 文件，替换默认的 compactor.yaml / summarizer.yaml。

【默认值】
  不传 prompt_path 时，自动使用原始的 yaml 文件，行为与 ReMeLight 完全一致。

所有调用均以同步方式执行（通过 S() 辅助函数 + 单一 event loop），方便断点调试。
"""

import sys
import asyncio
from pathlib import Path

# ── 确保项目根目录在 sys.path 中 ──────────────────────────────────────────────
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from agentscope.message import Msg
from agentscope.tool import Toolkit

from reme.reme_light import ReMeLight
from reme.memory.file_based.components import Compactor, Summarizer
from reme.memory.file_based.tools import FileIO

# =============================================================================
# 同步执行辅助函数
# =============================================================================
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def S(coro):
    """把 coroutine 同步跑完并返回结果，相当于 await。"""
    return _loop.run_until_complete(coro)


# =============================================================================
# 原始 yaml 文件的路径（用作默认值）
# =============================================================================
_components_dir = _project_root / "reme" / "memory" / "file_based" / "components"
_DEFAULT_COMPACTOR_PROMPT_PATH  = str(_components_dir / "compactor.yaml")
_DEFAULT_SUMMARIZER_PROMPT_PATH = str(_components_dir / "summarizer.yaml")


# =============================================================================
# MyReMeLight - 暴露 prompt 路径的子类
# =============================================================================

class MyReMeLight(ReMeLight):
    """
    继承 ReMeLight，在构造函数里暴露 compactor_prompt_path 和 summarizer_prompt_path。

    用法：
        reme = MyReMeLight(
            compactor_prompt_path="felix_test/prompts/my_compactor.yaml",
            summarizer_prompt_path="felix_test/prompts/my_summarizer.yaml",
            ...其他参数同 ReMeLight...
        )
    """

    def __init__(
        self,
        compactor_prompt_path: str = _DEFAULT_COMPACTOR_PROMPT_PATH,   # compact_memory 使用的 prompt yaml
        summarizer_prompt_path: str = _DEFAULT_SUMMARIZER_PROMPT_PATH,  # summary_memory 使用的 prompt yaml
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.compactor_prompt_path: str = compactor_prompt_path
        self.summarizer_prompt_path: str = summarizer_prompt_path

    async def compact_memory(
        self,
        messages: list[Msg],
        as_llm: str = "default",
        as_llm_formatter: str = "default",
        as_token_counter: str = "default",
        language: str = "zh",
        max_input_length: float = 128 * 1024,
        compact_ratio: float = 0.7,
        previous_summary: str = "",
        return_dict: bool = False,
        add_thinking_block: bool = True,
        extra_instruction: str = "",
    ):
        """重写 compact_memory，使用 self.compactor_prompt_path 指定的 prompt。"""
        compactor = Compactor(
            memory_compact_threshold=self.calculate_memory_compact_threshold(
                max_input_length,
                compact_ratio,
            ),
            as_llm=as_llm,
            as_llm_formatter=as_llm_formatter,
            as_token_counter=as_token_counter,
            language=language if language == "zh" else "",
            return_dict=return_dict,
            add_thinking_block=add_thinking_block,
            extra_instruction=extra_instruction,
            prompt_path=self.compactor_prompt_path,    # ← 自定义 prompt 路径
        )
        return await compactor.call(
            messages=messages,
            previous_summary=previous_summary,
            service_context=self.service_context,
        )

    async def summary_memory(
        self,
        messages: list[Msg],
        as_llm: str = "default",
        as_llm_formatter: str = "default",
        as_token_counter: str = "default",
        toolkit: Toolkit | None = None,
        language: str = "zh",
        max_input_length: float = 128 * 1024,
        compact_ratio: float = 0.7,
        timezone: str | None = None,
        add_thinking_block: bool = True,
    ):
        """重写 summary_memory，使用 self.summarizer_prompt_path 指定的 prompt。"""
        if toolkit is None:
            toolkit = Toolkit()
            file_io = FileIO(working_dir=str(self.working_path))
            toolkit.register_tool_function(file_io.read_file)
            toolkit.register_tool_function(file_io.write_file)
            toolkit.register_tool_function(file_io.edit_file)

        summarizer = Summarizer(
            working_dir=str(self.working_path),
            memory_dir=str(self.memory_path),
            memory_compact_threshold=self.calculate_memory_compact_threshold(
                max_input_length,
                compact_ratio,
            ),
            toolkit=toolkit,
            as_llm=as_llm,
            as_llm_formatter=as_llm_formatter,
            as_token_counter=as_token_counter,
            language=language if language == "zh" else "",
            timezone=timezone,
            add_thinking_block=add_thinking_block,
            prompt_path=self.summarizer_prompt_path,   # ← 自定义 prompt 路径
        )
        return await summarizer.call(
            messages=messages,
            service_context=self.service_context,
        )


# =============================================================================
# 测试用对话消息构造（与 test_reme_light.py 相同）
# =============================================================================

def make_all_messages() -> list[Msg]:
    return [
        Msg(name="user", role="user", content=(
            "你好，我正在用 Python 开发一个 AI 助手项目，"
            "使用 FastAPI 作为后端框架，前端是 React。"
            "项目名叫 SmartAssist，目标是帮助用户管理日程和待办事项。"
        )),
        Msg(name="assistant", role="assistant", content=(
            "好的！SmartAssist 听起来很有潜力。"
            "FastAPI + React 是很流行的组合。"
            "请问你目前遇到了什么具体问题，或者需要我帮你设计哪部分？"
        )),
        Msg(name="user", role="user", content=(
            "我在做用户认证模块，使用 JWT token。"
            "目前遇到的问题是：token 过期后，前端没有自动刷新，"
            "用户需要重新登录，体验很差。"
            "我希望实现 silent refresh（静默刷新）机制。"
        )),
        Msg(name="assistant", role="assistant", content=(
            "Silent refresh 标准做法：\n"
            "1. access token（15 分钟）+ refresh token（7 天）\n"
            "2. 前端在 access token 快过期前自动调用 /auth/refresh\n"
            "3. 后端验证 refresh token，颁发新 access token\n"
            "建议用 python-jose + HTTPOnly Cookie 存储 refresh token。"
        )),
        Msg(name="user", role="user", content=(
            "好的，请帮我写 FastAPI 的 JWT 认证代码，"
            "包括 login、refresh、logout 三个接口。"
            "用户信息存在 PostgreSQL，用 SQLAlchemy ORM，密码用 bcrypt 加密。"
        )),
        Msg(name="assistant", role="assistant", content=(
            "以下是完整的 FastAPI JWT 认证实现（含 login/refresh/logout）：\n"
            "SECRET_KEY = 'your-secret-key', ALGORITHM = 'HS256'\n"
            "ACCESS_TOKEN_EXPIRE_MINUTES = 15, REFRESH_TOKEN_EXPIRE_DAYS = 7\n"
            "使用 HTTPOnly Cookie 存储 refresh token，防止 XSS 攻击。"
        )),
        Msg(name="user", role="user", content=(
            "非常感谢！另外数据库连接池应该怎么配置？"
            "预计同时在线 500 人，pool_size 和 max_overflow 怎么设？"
        )),
        Msg(name="assistant", role="assistant", content=(
            "500 并发建议：pool_size=20, max_overflow=30, pool_timeout=30, "
            "pool_recycle=1800, pool_pre_ping=True。"
            "PostgreSQL 侧 max_connections=200，配合 PgBouncer 做连接池代理。"
        )),
    ]


# =============================================================================
# 分节打印辅助
# =============================================================================

def section(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


# =============================================================================
# 主测试流程
# =============================================================================

def main():
    # =========================================================================
    # 1. 初始化 MyReMeLight（暴露 prompt 路径）
    # =========================================================================
    section("Step 1: 初始化 MyReMeLight")

    reme = MyReMeLight(
        # ── 自定义 prompt 路径 ──────────────────────────────────────────────
        # 不传则使用原始 yaml，行为与 ReMeLight 完全一致
        # 传入自定义路径时，LLM 会使用你指定的 prompt
        compactor_prompt_path=_DEFAULT_COMPACTOR_PROMPT_PATH,   # compact_memory 的 prompt
        summarizer_prompt_path=_DEFAULT_SUMMARIZER_PROMPT_PATH, # summary_memory 的 prompt

        # ── 以下参数与 ReMeLight 完全相同 ──────────────────────────────────
        working_dir="felix_test/.reme_subclass_test",
        llm_api_key="sk-3fe61157d23345779df499667e5263fa",
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="sk-29a937c25bff4752af88514525c329b3",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_as_llm_config={
            "model_name": "qwen3-235b-a22b",
        },
        default_embedding_model_config={
            "model_name": "text-embedding-v4",
        },
        default_file_store_config={
            "backend": "chroma",
            "store_name": "reme",
            "fts_enabled": True,
            "vector_enabled": True,
        },
        default_file_watcher_config=None,
        vector_weight=0.7,
        candidate_multiplier=3.0,
        enable_load_env=True,
    )

    print(f"  类型：{type(reme).__name__}")
    print(f"  compactor_prompt_path  = {reme.compactor_prompt_path}")
    print(f"  summarizer_prompt_path = {reme.summarizer_prompt_path}")
    print(f"  working_path           = {reme.working_path}")
    print(f"  memory_path            = {reme.memory_path}")

    # =========================================================================
    # 2. start()
    # =========================================================================
    section("Step 2: start() - 启动记忆系统")

    S(reme.start())
    print("  记忆系统已启动")

    # =========================================================================
    # 3. 准备消息，手动划分待压缩 / 保留
    # =========================================================================
    section("Step 3: 构建测试消息，手动划分待压缩 / 保留")

    all_messages = make_all_messages()
    messages_to_compact = all_messages[:6]   # 前 6 条：待压缩历史消息
    messages_to_keep    = all_messages[6:]   # 后 2 条：保留近期消息

    print(f"  messages_to_compact 数量 = {len(messages_to_compact)}")
    print(f"  messages_to_keep    数量 = {len(messages_to_keep)}")

    # =========================================================================
    # 4. compact_memory()（使用自定义 compactor_prompt_path）
    # =========================================================================
    section("Step 4: compact_memory() - 使用自定义 prompt 路径")

    print(f"  使用的 prompt 文件：{reme.compactor_prompt_path}")

    compact_summary = S(reme.compact_memory(
        messages=messages_to_compact,
        as_llm="default",
        as_llm_formatter="default",
        as_token_counter="default",
        language="zh",
        max_input_length=128 * 1024,
        compact_ratio=0.7,
        previous_summary="",
        return_dict=False,
        add_thinking_block=True,
        extra_instruction="",
    ))

    print(f"  compact_summary 长度：{len(str(compact_summary))} 字符")
    print(f"\n  compact_summary 内容预览：\n{str(compact_summary)[:600]}")

    # =========================================================================
    # 5. summary_memory()（使用自定义 summarizer_prompt_path）
    # =========================================================================
    section("Step 5: summary_memory() - 使用自定义 prompt 路径")

    print(f"  使用的 prompt 文件：{reme.summarizer_prompt_path}")

    summary_result = S(reme.summary_memory(
        messages=messages_to_compact,
        as_llm="default",
        as_llm_formatter="default",
        as_token_counter="default",
        toolkit=None,
        language="zh",
        max_input_length=128 * 1024,
        compact_ratio=0.7,
        timezone=None,
        add_thinking_block=True,
    ))

    print(f"  summary_result 长度：{len(str(summary_result))} 字符")
    print(f"\n  summary_result 内容预览：\n{str(summary_result)[:400]}")

    memory_files = list(reme.memory_path.glob("*.md"))
    print(f"\n  memory/ 目录下的文件：{[f.name for f in memory_files]}")

    # =========================================================================
    # 6. await_summary_tasks()
    # =========================================================================
    section("Step 6: await_summary_tasks() - 等待后台 summary 任务落盘")

    pending_count = len(reme.summary_tasks)
    print(f"  当前 pending summary_tasks 数：{pending_count}")
    tasks_result = S(reme.await_summary_tasks())
    print(f"  await_summary_tasks() 返回：{tasks_result!r}")

    # =========================================================================
    # 7. memory_search()
    # =========================================================================
    section("Step 7: memory_search() - 语义检索 memory 文件")

    search_result = S(reme.memory_search(
        query="JWT token 刷新机制",
        max_results=5,
        min_score=0.1,
    ))
    print(f"  search_result 内容：")
    for block in search_result.content:
        print(f"\n{block['text'][:600]}")

    # =========================================================================
    # 8. close()
    # =========================================================================
    section("Step 8: close() - 关闭记忆系统")

    closed_ok = S(reme.close())
    print(f"  close() 返回值：{closed_ok}")
    print("  记忆系统已关闭")

    # =========================================================================
    # 完成：列出生成文件
    # =========================================================================
    section("全部测试步骤执行完毕")
    working_path = Path("felix_test/.reme_subclass_test")
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
