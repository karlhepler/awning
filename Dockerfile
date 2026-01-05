FROM python:3.13-slim

ARG VERSION=unknown
ENV VERSION=$VERSION

WORKDIR /app

RUN pip install --no-cache-dir \
    requests \
    python-dotenv \
    rich \
    pvlib \
    pandas \
    pytz \
    zeroconf

COPY awning_controller.py awning_automation.py ./

ENTRYPOINT ["/bin/sh", "-c", "if [ \"$1\" = 'version' ]; then echo $VERSION; else python3 awning_automation.py \"$@\"; fi", "--"]
