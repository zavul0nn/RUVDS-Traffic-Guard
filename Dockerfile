FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY guard.py history.py web.py test_guard.py test_web.py ./
COPY static/ ./static/
RUN chmod 0644 /app/*.py && chmod -R a+rX /app/static \
    && python -m unittest -v test_guard test_web && rm test_guard.py test_web.py
ENTRYPOINT ["python", "/app/guard.py"]
CMD ["run"]
