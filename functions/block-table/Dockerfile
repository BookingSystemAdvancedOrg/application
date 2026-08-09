# Built from the REPO ROOT, not this directory:
#   docker build -f functions/block-table/Dockerfile -t block-table:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/block-table/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/block-table/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
