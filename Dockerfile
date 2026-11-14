# 一阶段：装依赖（分开一层，改代码时不用重装依赖）
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先只拷依赖清单，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY src/ ./src/
COPY app/ ./app/
COPY sql/ ./sql/
COPY scripts/ ./scripts/
# 注意：Dockerfile 的 COPY 是指令不是 shell，不支持 `2>/dev/null`、`|| true`
# 这类写法（会把它们当成源路径，构建直接失败）。tests/ 一定存在，直接拷。
COPY tests/ ./tests/
COPY conftest.py ./

# 数据目录挂载点（宿主机不拷数据进镜像，靠 volume 挂进来）
RUN mkdir -p data/processed reports images

EXPOSE 8501

# 健康检查：容器里 curl 一下首页，200 才算活着（这也是运维排障的第一手证据）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).status == 200 else 1)"

CMD ["streamlit", "run", "app/app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true"]