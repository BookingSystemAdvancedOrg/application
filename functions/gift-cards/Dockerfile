# Built from the REPO ROOT, not this directory:
#   docker build -f functions/gift-cards/Dockerfile -t gift-cards:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/gift-cards/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/gift-cards/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
