FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY guard.py history.py web.py host_target.py test_guard.py test_web.py test_host_target.py ./
COPY static/ ./static/
RUN chmod 0644 /app/*.py && chmod -R a+rX /app/static \
    && python -m unittest -v test_guard test_web test_host_target && rm test_guard.py test_web.py test_host_target.py
ENTRYPOINT ["python", "/app/guard.py"]
CMD ["run"]
