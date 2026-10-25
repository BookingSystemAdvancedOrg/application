# Built from the REPO ROOT, not this directory:
#   docker build -f functions/kassa/Dockerfile -t kassa:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/kassa/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/kassa/app.py ${LAMBDA_TASK_ROOT}/
# The user manual the till shows ("Bruksanvisning", SKVFS 2014:9 4 kap. 12 §).
COPY docs/kassa/BRUKSANVISNING.md ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
