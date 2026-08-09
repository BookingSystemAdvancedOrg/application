# Built from the REPO ROOT, not this directory:
#   docker build -f functions/pre-signed-url/Dockerfile -t pre-signed-url:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/pre-signed-url/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/pre-signed-url/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
