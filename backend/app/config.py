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
    llm_model: str = "qwen-plus"
    # Qwen 思考模式开关：False 关闭思考（更快，延迟降 60-75%），True 开启（更强推理，但每次调用多出几十秒思考）
    enable_thinking: bool = False
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
    # SQLite 数据库文件路径
    db_path: str = "app.db"
    # 工作记忆 token 预算上限：每次 LLM 调用前估算上下文，超预算淘汰最老交互 + LLM 摘要兜底
    # 应 ≤ 模型上下文上限并预留输出余量（qwen-plus 默认留一半）
    llm_context_budget: int = 32000
    # 工具结果字符串截断阈值：超过该字符数时，完整结果写临时文件，
    # 窗口只放预览 + 文件路径提示，模型用 read_file/grep 按需读回被截掉的部分
    truncate_limit: int = 4000
    # 工具结果地址索引的落盘目录（超长结果写临时文件，本轮结束即删）
    result_dir: str = "results"
    # 历史答案原文占 llm_context_budget 的比例（历史会话压缩：超预算的早期答案落盘+摘要，剩余的留给工具交互）
    history_budget_ratio: float = 0.5


# 全局配置实例，供各模块导入使用
settings = Settings()
