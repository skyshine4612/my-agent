# run.py
# 后端服务启动入口：使用 uvicorn 启动 FastAPI 应用
import uvicorn

if __name__ == "__main__":
    # 以开发模式启动，reload=True 便于代码改动后自动重载
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
