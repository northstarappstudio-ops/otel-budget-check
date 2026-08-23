FROM python:3.11-slim

COPY src /opt/otel-budget-check/src
COPY entrypoint.sh /opt/otel-budget-check/entrypoint.sh
RUN chmod +x /opt/otel-budget-check/entrypoint.sh

ENV PYTHONPATH=/opt/otel-budget-check/src
ENTRYPOINT ["/opt/otel-budget-check/entrypoint.sh"]