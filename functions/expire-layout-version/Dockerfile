# Built from the REPO ROOT, not this directory:
#   docker build -f functions/expire-layout-version/Dockerfile -t expire-layout-version:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/expire-layout-version/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/expire-layout-version/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
