# Built from the REPO ROOT, not this directory:
#   docker build -f functions/help-assistant/Dockerfile -t help-assistant:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/help-assistant/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/help-assistant/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
