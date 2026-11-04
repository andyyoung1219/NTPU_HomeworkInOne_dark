FROM docker.io/library/python:3.12-slim
COPY supervisor.py /opt/runner.py
RUN chmod 0555 /opt/runner.py
USER 65532:65532
WORKDIR /output
ENTRYPOINT ["python", "-I", "-B", "/opt/runner.py"]
