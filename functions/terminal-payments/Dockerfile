# Built from the REPO ROOT, not this directory:
#   docker build -f functions/terminal-payments/Dockerfile -t terminal-payments:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/terminal-payments/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/terminal-payments/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
