# One image for every webshop component; the chart selects the component
# with the container args (python -m webshop <component>).
#
# Build for the EC2 nodes (x86_64), from AWS/kubeadm/k8/helm/webshop/:
#   docker buildx build --platform linux/amd64 -t <registry>/webshop:0.1.0 --push src
FROM python:3.13.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-deps -r requirements.txt

COPY webshop ./webshop

ARG WEBSHOP_VERSION=0.1.0
ENV WEBSHOP_VERSION=${WEBSHOP_VERSION}

# Unprivileged, and compatible with readOnlyRootFilesystem (nothing writes
# outside /tmp, which the chart mounts as an emptyDir).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin webshop
USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "webshop"]
CMD ["--help"]
