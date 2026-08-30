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
    # ModelScope 访问令牌（用于调用 ModelScope 上的 MCP 服务）
    modelscope_token: str = ""
    # 高德地图 MCP 服务地址（用于旅行助手的地理/路径查询）
    amap_mcp_url: str = ""
    # 12306 火车票 MCP 服务地址（查询跨城火车票/票价）
    train_12306_url: str = ""
    # variflight 机票 MCP 服务地址（查询跨城航班/票价）
    flight_variflight_url: str = ""
    # bing 网页搜索 MCP 服务地址（查询实时信息/攻略/价格）
    bing_mcp_url: str = ""
    # 每日新闻 MCP 服务地址（热点新闻/新闻搜索）
    news_mcp_url: str = ""
    # SQLite 数据库文件路径
    db_path: str = "app.db"
    # 允许跨域访问的前端来源（逗号分隔的多个来源）
    cors_origins: str = "http://localhost:5173"


# 全局配置实例，供各模块导入使用
settings = Settings()
