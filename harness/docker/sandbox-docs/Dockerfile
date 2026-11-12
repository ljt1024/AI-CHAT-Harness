FROM python:3.12-slim

# The default sandbox image is a bare interpreter: no document library, no
# network to fetch one. Anything the agent is asked to produce as a report -- a
# PDF, a deck, a workbook -- could not be written at all. This image carries the
# libraries instead. Everything about how the container runs is unchanged by the
# runner: still read-only, still network_isolated, still unprivileged.
RUN pip install --no-cache-dir --disable-pip-version-check \
    reportlab==4.4.4 \
    openpyxl==3.1.5 \
    python-pptx==1.0.2 \
    python-docx==1.2.0 \
    pillow==11.3.0 \
    matplotlib==3.10.7 \
    pandas==2.3.3 \
 && rm -rf /root/.cache

USER 1000:1000
WORKDIR /workspace
