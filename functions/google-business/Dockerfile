# Built from the REPO ROOT, not this directory:
#   docker build -f functions/google-business/Dockerfile -t google-business:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/google-business/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/google-business/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
