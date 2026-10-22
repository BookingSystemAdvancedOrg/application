# Built from the REPO ROOT, not this directory:
#   docker build -f functions/guest-privacy/Dockerfile -t guest-privacy:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/guest-privacy/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/guest-privacy/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
