# Built from the REPO ROOT, not this directory:
#   docker build -f functions/platform-stripe-webhook/Dockerfile -t platform-stripe-webhook:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/platform-stripe-webhook/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/platform-stripe-webhook/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
