# Built from the REPO ROOT, not this directory:
#   docker build -f functions/catering-customer/Dockerfile -t catering-customer:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/catering-customer/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/catering-customer/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
