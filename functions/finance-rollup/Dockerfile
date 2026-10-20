# Built from the REPO ROOT, not this directory:
#   docker build -f functions/finance-rollup/Dockerfile -t finance-rollup:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/finance-rollup/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/finance-rollup/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
