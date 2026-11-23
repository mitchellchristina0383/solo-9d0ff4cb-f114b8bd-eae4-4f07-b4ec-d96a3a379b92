FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NATLAB_COOLDOWN_SECONDS=60

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY natlab ./natlab
COPY wsgi.py ./

RUN useradd --create-home --uid 10001 natlab \
    && chown -R natlab:natlab /app
USER natlab

EXPOSE 8000

# 演练状态保存在进程内存里，必须单 worker；线程数只影响 HTTP 并发。
# --timeout 用于在反向代理后保持长连接不误杀。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "wsgi:app"]
