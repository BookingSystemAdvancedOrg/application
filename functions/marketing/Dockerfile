# Built from the REPO ROOT, not this directory:
#   docker build -f functions/marketing/Dockerfile -t marketing:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/marketing/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/marketing/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
