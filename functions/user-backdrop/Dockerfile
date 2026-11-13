# Built from the REPO ROOT, not this directory:
#   docker build -f functions/user-backdrop/Dockerfile -t user-backdrop:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/user-backdrop/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
