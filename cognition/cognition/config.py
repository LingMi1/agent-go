"""运行配置（pydantic-settings，从环境变量读取）。

M1 范围：模型 provider + id、Anthropic/DeepSeek key、Postgres DSN、
ReAct 步数上限、gRPC 监听端口。所有字段都有默认值，因此纯逻辑测试无需任何环境变量。

环境变量约定：
- 业务配置使用 `COGNITION_` 前缀（如 COGNITION_MODEL_PROVIDER / COGNITION_GRPC_PORT）。
- 密钥沿用各家 SDK 的惯用名（ANTHROPIC_API_KEY / DEEPSEEK_API_KEY），同时接受带前缀写法。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """认知面运行配置（单一事实源）。"""

    model_config = SettingsConfigDict(
        env_prefix="COGNITION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 字段以 model_ 开头会触发 pydantic 的保护命名空间告警，这里关闭。
        protected_namespaces=(),
        populate_by_name=True,
    )

    # —— 模型路由 ——
    # M1：单一 provider 由 env 解析（默认 anthropic）；role→provider 分支保留在 router 中。
    model_provider: str = Field(default="anthropic")  # "anthropic" | "deepseek"
    anthropic_model: str = Field(default="claude-opus-4-8")
    deepseek_model: str = Field(default="deepseek-chat")

    # —— 模型分层（M2）——
    # planner / executor 各自的 provider+model；留空则回落到上面的单 provider 设置。
    # 默认 deepseek（性价比）；owner 在最终集成时把 planner 切到 opus。
    planner_provider: str | None = Field(default=None)  # "anthropic" | "deepseek"
    planner_model: str | None = Field(default=None)
    executor_provider: str | None = Field(default=None)
    executor_model: str | None = Field(default=None)

    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COGNITION_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    deepseek_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COGNITION_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(default="https://api.deepseek.com")

    # —— Checkpoint（可恢复执行状态）——
    pg_dsn: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COGNITION_PG_DSN", "DATABASE_URL"),
    )

    # —— ReAct ——
    max_steps: int = Field(default=40, ge=1)

    # —— Plan-Execute（M2）——
    # 外层 plan→execute→replan 循环上限（镜像原项目 plannerMaxSteps，默认 5）。
    planner_max_steps: int = Field(default=5, ge=1)
    # 深度研究模式（deep_research）的外层轮次上限：研究需要更多"检索-验证-推进"轮。
    research_max_steps: int = Field(default=8, ge=1)
    # 每次图执行（react 主图 / plan·research 的每个执行分支）的工具调用硬预算，0=不限。
    # 实测 DeepSeek 并行多调用×分支数可爆到单 run 961 次 web_fetch：拖满 RUN_TIMEOUT、
    # 烧穿搜索额度与模型余额——达预算后 think 以 tool_choice="none" 强制收口。
    max_tool_calls_per_branch: int = Field(default=40, ge=0)
    # plan/research 单 run 的**全局**分支预算（0=不限）。分支级预算挡不住
    # 「8 步计划×每步多子任务×replan」的乘积（实测 30+ 分支、1400+ 次调用仍超时）——
    # 累计派发分支达上限后 route 直接转 summary 基于已有结果收口。
    max_total_branches: int = Field(default=8, ge=0)

    # —— HITL 人工审批（M11）——
    # 受保护工具名列表（COGNITION_APPROVAL_TOOLS='["script_runner"]'）；默认空=关闭。
    # 仅 react 主图生效（plan 分支吞 GraphInterrupt，见 approval.py 模块注释）。
    # 注意重放语义：同批次其它副作用工具会随 resume 重执行——按需谨慎启用。
    approval_tools: list[str] = Field(default_factory=list)
    approval_reason: str = Field(default="该操作被列为高危，需人工确认后执行")
    # 并行子任务宽度上限（镜像 maxParallelTasks，默认 2）。
    max_parallel_tasks: int = Field(default=2, ge=1)
    # 单个并行分支（executor 子图）的超时秒数。默认 300：思考模型（reasoning）输出
    # 长报告时 120s 不够，实测被掐在 write_report 调用前（分支 ERROR→任务终止）。
    branch_timeout_seconds: float = Field(default=300.0, gt=0)

    # —— 产物对象存储（MinIO）——
    # 默认与 deploy/.env 的 minio 对齐；上传是惰性/可降级的（无 MinIO 也能跑单测）。
    minio_endpoint: str = Field(
        default="localhost:9000",
        validation_alias=AliasChoices("COGNITION_MINIO_ENDPOINT", "MINIO_ENDPOINT"),
    )
    minio_access_key: str = Field(
        default="minioadmin",
        validation_alias=AliasChoices(
            "COGNITION_MINIO_ACCESS_KEY", "MINIO_ACCESS_KEY", "MINIO_ROOT_USER"
        ),
    )
    minio_secret_key: str = Field(
        default="minioadmin",
        validation_alias=AliasChoices(
            "COGNITION_MINIO_SECRET_KEY", "MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD"
        ),
    )
    minio_bucket: str = Field(
        default="artifacts",
        validation_alias=AliasChoices("COGNITION_MINIO_BUCKET", "MINIO_BUCKET"),
    )
    minio_secure: bool = Field(default=False)
    # 是否真正上传到 MinIO；默认 False → 单测/无 MinIO 环境只构造 ArtifactRef，不触网。
    minio_upload_enabled: bool = Field(default=False)

    # —— 工具生态：MCP / Skill ——
    # 默认开关为真但无配置即为空操作（不接任何 MCP/Skill），故不改变既有行为。
    # 环境变量传 JSON：COGNITION_MCP_SERVERS='{"fetch":{"transport":"stdio","command":"uvx",...}}'。
    mcp_enabled: bool = Field(default=True)
    mcp_servers: dict[str, dict] = Field(default_factory=dict)
    skills_enabled: bool = Field(default=True)
    skills_dirs: list[str] = Field(default_factory=list)  # COGNITION_SKILLS_DIRS='["./skills"]'
    skill_runner: str = Field(default="local")  # "local"（dev/CI）| "docker"（生产隔离）
    skill_runner_image: str = Field(default="my-agent/skill-executor:latest")
    skill_disclosure_max_chars: int = Field(default=8000, ge=256)  # L2 正文预算
    skill_default_timeout: float = Field(default=120.0, gt=0)

    # —— Agentic RAG（Qdrant 混合检索）——
    # 默认 rag_enabled=False → knowledge_search 不注入，既有行为/测试不变。
    rag_enabled: bool = Field(default=False)
    qdrant_url: str = Field(
        default="http://localhost:6333",
        validation_alias=AliasChoices("COGNITION_QDRANT_URL", "QDRANT_URL"),
    )  # ":memory:" 或空 → 本地内存模式（离线/测试）
    qdrant_collection: str = Field(default="cognition_docs")
    # embedding：fake（测试确定性）| fastembed（本地 ONNX）| siliconflow（国产便宜 API）
    embedding_provider: str = Field(default="fake")
    embedding_model: str = Field(default="BAAI/bge-small-zh-v1.5")
    embedding_dimension: int = Field(default=64, ge=1)  # fake=64；bge-small-zh=512
    embedding_base_url: str = Field(default="https://api.siliconflow.cn/v1")
    embedding_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COGNITION_EMBEDDING_API_KEY", "SILICONFLOW_API_KEY"),
    )
    # sparse：fake（确定性 hashing）| fastembed（Qdrant/bm25）
    sparse_provider: str = Field(default="fake")
    # rerank
    rerank_enabled: bool = Field(default=False)
    rerank_provider: str = Field(default="fake")  # fake | siliconflow
    rerank_model: str = Field(default="BAAI/bge-reranker-v2-m3")
    rerank_base_url: str = Field(default="https://api.siliconflow.cn/v1")
    rerank_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COGNITION_RERANK_API_KEY", "SILICONFLOW_API_KEY"),
    )
    rerank_threshold: float = Field(default=0.0, ge=0.0)  # 真实 reranker 用 0.3；fake 用 0.0
    # 检索产物登记（M9 UX 修正）：默认关——ReAct 一轮多次检索会把产物区刷成
    # N 份同名 search-results.md（过程≠交付物）；deep_research 模式例外（唯一命名）。
    search_artifact_enabled: bool = Field(default=False)
    # 检索/反思参数
    rag_top_k: int = Field(default=10, ge=1)
    rag_rerank_top_k: int = Field(default=5, ge=1)
    rag_reflection_limit: int = Field(default=2, ge=0)
    rag_subquery_max: int = Field(default=3, ge=1)
    rag_prefetch_limit: int = Field(default=20, ge=1)

    # —— 会话短期记忆：历史投影预算（token 用字符近似）——
    # think 节点入模型前对累积 messages 做"近期优先"投影，超阈值折叠旧轮为摘要。
    history_max_messages: int = Field(default=40, ge=2)
    history_max_chars: int = Field(default=24000, ge=256)

    # —— 可选 Langfuse trace（seam，默认关，未装也能跑）——
    langfuse_enabled: bool = Field(default=False)
    langfuse_public_key: str | None = Field(
        default=None, validation_alias=AliasChoices("COGNITION_LANGFUSE_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: str | None = Field(
        default=None, validation_alias=AliasChoices("COGNITION_LANGFUSE_SECRET_KEY", "LANGFUSE_SECRET_KEY")
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("COGNITION_LANGFUSE_HOST", "LANGFUSE_HOST"),
    )

    # —— 可选 OTel 追踪（docs/18 seam，默认关，未装也能跑；opentelemetry-* 在 pyproject otel 组）——
    # 双接 OTel 官方无前缀 env（OTEL_*）与本项目前缀（COGNITION_OTEL_*）。
    otel_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("COGNITION_OTEL_ENABLED", "OTEL_ENABLED"),
    )
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317",
        validation_alias=AliasChoices(
            "COGNITION_OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"
        ),
    )
    otel_service_name: str = Field(
        default="my-agent-cognition",
        validation_alias=AliasChoices("COGNITION_OTEL_SERVICE_NAME", "OTEL_SERVICE_NAME"),
    )

    # —— M12 工具缺口 ——
    # web_fetch：抓公开网页（SSRF 服务端封禁私网）；关掉则不注册。
    web_fetch_enabled: bool = Field(default=True)
    # code_interpreter：任意 Python 执行——**默认关闭**（危险原语必须显式 opt-in）。
    # 开启前提：skill_runner=docker（强隔离）或把 "code_interpreter" 列入 approval_tools
    # （每次执行人工审批）。local 运行器 + 无审批 = 认知面主机上的无沙箱 RCE，装配期会告警。
    code_interpreter_enabled: bool = Field(default=False)

    # —— 联网搜索（web_search 工具）——
    # provider："auto"（配了 Tavily key 用 tavily，否则 ddg 免 key）| "tavily" | "ddg"
    # | "fake"（测试确定性）；空串=不注册 web_search 工具（镜像 image_gen_provider 门控）。
    search_provider: str = Field(default="auto")
    tavily_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COGNITION_TAVILY_API_KEY", "TAVILY_API_KEY"),
    )
    search_max_results: int = Field(default=6, ge=1, le=10)
    search_timeout_seconds: float = Field(default=15.0, gt=0)
    # GitHub API 认证（web_fetch 对 api.github.com / raw.githubusercontent.com 注入
    # Authorization，限流 60→5000 次/小时）。**只认 COGNITION_GITHUB_TOKEN，不接裸
    # GITHUB_TOKEN 别名**：开发机 shell 常驻 gh CLI 的 GITHUB_TOKEN，接了会被静默吸入
    # 并随抓取请求发出——凭据外送必须显式 opt-in，不能靠环境巧合。
    github_token: str | None = Field(default=None)

    # —— 图像生成（M9 线 B：provider 抽象可切换，国产便宜模型优先）——
    # 空串=不注册 image_generate 工具（镜像 rag_enabled 门控先例）；fake 供测试/离线。
    image_gen_provider: str = Field(
        default="",  # "" | "fake" | "openai"(gpt-image) | "ark"(火山方舟) | "wanx"(通义万相)
        validation_alias=AliasChoices("COGNITION_IMAGE_GEN_PROVIDER", "IMAGE_GEN_PROVIDER"),
    )
    image_gen_model: str = Field(
        default="",  # 空则用各 provider 默认（ark: doubao-seedream；wanx: wanx2.1-t2i-turbo）
        validation_alias=AliasChoices("COGNITION_IMAGE_GEN_MODEL", "IMAGE_GEN_MODEL"),
    )
    image_gen_api_key: str | None = Field(
        default=None,
        # OPENAI_API_KEY 放最后：pydantic-settings 取首个命中的别名，放前面会让既有
        # ark/wanx（ARK_API_KEY/DASHSCOPE_API_KEY）配置被同环境里的 OPENAI_API_KEY 抢走。
        validation_alias=AliasChoices(
            "COGNITION_IMAGE_GEN_API_KEY", "IMAGE_GEN_API_KEY", "ARK_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"
        ),
    )
    # openai(gpt-image) 专用：low 成本优先（用户可调 medium/high/auto）。
    image_gen_quality: str = Field(default="low")
    image_gen_base_url: str = Field(default="")  # 空则用各 provider 默认端点
    image_gen_size: str = Field(default="1024x1024")

    # —— 输出格式模板（M9，镜像原项目 applyOutputStyle）——
    # 前端格式选择器的值→追加提示词；未知值忽略。per-run 经 config.metadata 注入
    #（react=think 调用期临时前置 system；plan=planner system 拼接），绝不进 checkpoint。
    output_format_prompts: dict[str, str] = Field(
        default={
            "html": (
                "输出格式要求：最终交付一份**独立完整的 HTML 网页**。调用 ppt-generation 技能的 "
                "md_to_html.py 脚本产出可下载的 document.html 产物，正文中概述网页结构与内容要点。"
            ),
            "docs": (
                "输出格式要求：最终回答以**结构化文档**形态呈现——清晰的标题层级、小节、要点列表与"
                "结尾总结，可直接复制为正式文档；篇幅较长时调用 write_report 产出 markdown 文件。"
            ),
            "ppt": (
                "输出格式要求：最终交付一份**演示文稿**。调用 ppt-generation 技能的 build_pptx.py "
                "生成 presentation.pptx（标题页+要点页，每页 3-6 个要点），并在正文中概述每页内容。"
            ),
            "table": (
                "输出格式要求：最终回答以 **Markdown 表格**为主组织信息（配少量说明文字）；"
                "表头明确、单位统一、必要时多张表分主题呈现。"
            ),
        }
    )

    # 确定性脚本化模型开关：无需真实 LLM key 即可端到端验证（见 providers/fake.py）。
    fake_model: bool = Field(default=False)

    # —— gRPC 服务 ——
    grpc_host: str = Field(default="0.0.0.0")
    grpc_port: int = Field(default=50051, ge=1, le=65535)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置。"""
    return Settings()
