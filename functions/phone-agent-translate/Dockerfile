# Built from the REPO ROOT, not this directory:
#   docker build -f functions/phone-agent-translate/Dockerfile -t phone-agent-translate:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/phone-agent-translate/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/phone-agent-translate/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
