# Built from the REPO ROOT, not this directory:
#   docker build -f functions/activate-layout-version/Dockerfile -t activate-layout-version:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/activate-layout-version/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/activate-layout-version/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
