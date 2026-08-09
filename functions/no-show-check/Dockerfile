# Built from the REPO ROOT, not this directory:
#   docker build -f functions/no-show-check/Dockerfile -t no-show-check:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/no-show-check/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/no-show-check/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
