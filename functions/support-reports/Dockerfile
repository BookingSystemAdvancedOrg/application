# Built from the REPO ROOT, not this directory:
#   docker build -f functions/support-reports/Dockerfile -t support-reports:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/support-reports/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/support-reports/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
