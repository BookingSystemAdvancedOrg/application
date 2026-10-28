# Built from the REPO ROOT, not this directory:
#   docker build -f functions/site-repo/Dockerfile -t site-repo:latest .
FROM public.ecr.aws/lambda/python:3.13

COPY functions/site-repo/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/site-repo/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
