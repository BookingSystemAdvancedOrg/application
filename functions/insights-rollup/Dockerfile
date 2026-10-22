# Built from the REPO ROOT, not this directory:
#   docker build -f functions/insights-rollup/Dockerfile -t insights-rollup:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/insights-rollup/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/insights-rollup/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
