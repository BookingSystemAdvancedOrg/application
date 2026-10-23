# Built from the REPO ROOT, not this directory:
#   docker build -f functions/catering-document/Dockerfile -t catering-document:latest .
FROM public.ecr.aws/lambda/python:3.13

# WeasyPrint renders with Pango; DejaVu covers å, ä, ö and the kr/% signs.
# Only /tmp is writable on Lambda, so the font cache is built into the image.
RUN dnf install -y pango fontconfig dejavu-sans-fonts && dnf clean all && fc-cache -f
ENV XDG_CACHE_HOME=/tmp/.cache

COPY shared/ ${LAMBDA_TASK_ROOT}/shared/

COPY functions/catering-document/requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" --no-cache-dir

COPY functions/catering-document/app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.handler"]
