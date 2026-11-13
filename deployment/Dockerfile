FROM python:3.11-slim

WORKDIR /app
COPY components /app/components
COPY matching /app/matching
COPY task_repr /app/task_repr
COPY resources /app/resources
COPY scheduler /app/scheduler
COPY task_executor /app/task_executor
COPY shared /app/shared
COPY deployment /app/deployment
RUN pip install --no-cache-dir 'numpy>=1.23,<3' 'jsonschema>=4.20,<5'

EXPOSE 8765
CMD ["python", "-m", "deployment.worker"]
