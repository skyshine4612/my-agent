# config.py
# 应用配置模块：使用 pydantic-settings 从环境变量 / .env 文件加载配置
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置类：集中管理后端所需的全部配置项"""

    # 从 .env 文件读取配置，忽略未定义的环境变量，避免报错
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM 服务 API Key（占位符，运行前需填入真实值）
    llm_api_key: str = ""
    # LLM 服务接口地址（默认使用阿里云百炼 DashScope 兼容模式）
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 使用的 LLM 模型名称
    llm_model: str = "qwen3.7-plus"
    # Qwen 思考模式开关：False 关闭思考（更快，延迟降 60-75%），True 开启（更强推理，但每次调用多出几十秒思考）
    enable_thinking: bool = False
    # LLM 采样温度
    llm_temperature: float = 0.5
    # Embedding 服务 API Key（同一 DashScope 账号，值通常与 llm_api_key 相同，但必须显式配在 .env）
    embedding_api_key: str = ""
    # Embedding 模型名（DashScope text-embedding-v4，输出 1024 维）
    embedding_model: str = "text-embedding-v4"
    # Embedding 向量维度：必须与建表时的 vector(N) 一致，换模型时两处要一起改
    embedding_dimensions: int = 1024
    # 多模态模型名（把攻略页插图转成文字描述用，如 qwen3.7-plus）
    vision_model: str = ""
    # 多模态 API Key（同上，值通常与 llm_api_key 相同，但必须显式配在 .env）
    vision_api_key: str = ""
    # ModelScope 访问令牌（用于调用 ModelScope 上的 MCP 服务）
    modelscope_token: str = ""
    # 12306 火车票 MCP 服务地址（查询跨城火车票/票价）
    train_12306_url: str = ""
    # variflight 官方 MCP 服务地址（直连官方，查询跨城航班/票价）
    flight_variflight_url: str = ""
    # variflight 官方 API Key（X-API-Key 认证，区别于 ModelScope 的 Bearer 令牌）
    variflight_api_key: str = ""
    # UAPIS 令牌（节假日/万年历查询，uapis.cn）
    uapis_api_key: str = ""
    # Tavily API Key（网络语义搜索 + 网页正文提取）
    tavily_api_key: str = ""
    # 高德 Web 服务 API Key（restapi.amap.com 的路线规划等，区别于 ModelScope 高德 MCP）
    amap_api_key: str = ""
    # Supabase Postgres 连接串（应用主库 postgres 的 public schema，与 auth schema 同库）
    supabase_database_url: str = ""
    # 测试库连接串（agent_test：内含最小 auth 桩，不跑 GoTrue，DDL 与主库完全一致）
    supabase_test_database_url: str = ""
    # Supabase JWT 密钥（与 supabase/docker 的 .env 中 JWT_SECRET 完全一致，本地 HS256 验签用）
    supabase_jwt_secret: str = ""
    # 工作记忆 token 预算上限：每次 LLM 调用前估算上下文，超预算淘汰最老交互 + LLM 摘要兜底
    # 应 ≤ 模型上下文上限（qwen3.7-plus 为 1M）并预留输出余量（128K）；取 128K 平衡成本与长上下文能力
    llm_context_budget: int = 128000
    # 工具结果字符串截断阈值：超过该字符数时，完整结果写临时文件，
    # 窗口只放预览 + 文件路径提示，模型用 read_file/grep 按需读回被截掉的部分
    truncate_limit: int = 4000
    # 工具结果地址索引的落盘目录（超长结果写临时文件，本轮结束即删）
    result_dir: str = "results"
    # 代码执行沙箱地址（compose 服务名；本地开发指向 150 上映射出来的端口）
    sandbox_url: str = "http://sandbox:8001"
    # 传给沙箱的执行超时秒数；后端 httpx 实际等的是这个值 + 5s（留给沙箱杀进程并回结构化响应）
    sandbox_timeout: int = 60
    # 会话文件目录（上传件与沙箱产物，跟随会话生命周期），与 result_dir 是两棵树
    file_dir: str = "files"
    # 历史答案原文占 llm_context_budget 的比例（历史会话压缩：超预算的早期答案落盘+摘要，剩余的留给工具交互）
    history_budget_ratio: float = 0.5
    # 长期记忆 hint 占 llm_context_budget 的比例（召回的用户偏好注入 system 的 token 预算上限）
    long_term_hint_ratio: float = 0.05


# 全局配置实例，供各模块导入使用
settings = Settings()
