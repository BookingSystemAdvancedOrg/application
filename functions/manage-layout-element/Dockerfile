# Built from the REPO ROOT, not this directory:
#   docker build -f functions/manage-layout-element/Dockerfile -t manage-layout-element:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/manage-layout-element/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/manage-layout-element/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
