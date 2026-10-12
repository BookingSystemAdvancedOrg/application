# Built from the REPO ROOT, not this directory:
#   docker build -f functions/webhook-payment-intent/Dockerfile -t webhook-payment-intent:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/webhook-payment-intent/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/webhook-payment-intent/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
