# Built from the REPO ROOT, not this directory:
#   docker build -f functions/dlq-replay/Dockerfile -t dlq-replay:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY functions/dlq-replay/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/dlq-replay/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
