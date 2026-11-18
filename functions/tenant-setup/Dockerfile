# Built from the REPO ROOT, not this directory:
#   docker build -f functions/tenant-setup/Dockerfile -t tenant-setup:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/tenant-setup/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/tenant-setup/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
