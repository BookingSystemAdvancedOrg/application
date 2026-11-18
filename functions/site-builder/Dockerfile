# Built from the REPO ROOT, not this directory:
#   docker build -f functions/site-builder/Dockerfile -t site-builder:latest .
# Stub for the removed site builder (see app.py): no dependencies.
FROM public.ecr.aws/lambda/python:3.13

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/site-builder/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
