# MVP: single image for API and Worker (same deps, different command).
FROM python:3.12-slim

WORKDIR /app

# Ensure `import src` works (console scripts use src.*); belt-and-suspenders with setuptools below.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# 优化方案：
# 1. 先复制 pyproject.toml，从中提取并安装第三方依赖（这层会被缓存）
# 2. 再复制源码并安装项目本身

COPY pyproject.toml ./

# 升级 pip，然后安装第三方依赖（从 pyproject.toml 中提取）
RUN pip install --no-cache-dir --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        "fastapi>=0.115.0" \
        "httpx>=0.28.0" \
        "mysql-connector-python>=9.4.0" \
        "PyYAML>=6.0.1" \
        "python-multipart>=0.0.9" \
        "uvicorn>=0.31.0" \
        "python-dotenv>=1.0.1"

# 复制代码和其他资源
COPY src ./src
COPY contracts ./contracts
COPY db ./db

# 安装项目本身（不重新安装依赖）
RUN pip install --no-cache-dir --no-deps . -i https://pypi.tuna.tsinghua.edu.cn/simple

EXPOSE 18008

# Override with docker-compose: worker uses customer-profile-worker
CMD ["customer-profile-api"]
