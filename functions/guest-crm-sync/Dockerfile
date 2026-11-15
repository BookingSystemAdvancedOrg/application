# Built from the REPO ROOT, not this directory:
#   docker build -f functions/guest-crm-sync/Dockerfile -t guest-crm-sync:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/guest-crm-sync/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/guest-crm-sync/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
