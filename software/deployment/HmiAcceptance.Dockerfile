FROM ubuntu:24.04
ARG HMI_VERSION
ENV DEBIAN_FRONTEND=noninteractive
ENV HMI_EXPECTED_VERSION=${HMI_VERSION}
COPY host/dist/rp1-test-hmi_${HMI_VERSION}_amd64.deb /tmp/rp1-test-hmi.deb
RUN apt-get update && apt-get install -y --no-install-recommends /tmp/rp1-test-hmi.deb xvfb xauth curl \
    && rm -rf /var/lib/apt/lists/* /tmp/rp1-test-hmi.deb
COPY deployment/hmi-acceptance.sh /acceptance.sh
ENTRYPOINT ["bash", "/acceptance.sh"]
