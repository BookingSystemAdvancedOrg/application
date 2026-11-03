# Built from the REPO ROOT, not this directory:
#   docker build -f functions/meta-ads-jobs/Dockerfile -t meta-ads-jobs:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/meta-ads-jobs/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/meta-ads-jobs/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
