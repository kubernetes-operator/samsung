# k8s-traffic-operator 본체 이미지. operator/ 디렉토리를 빌드 컨텍스트로 사용:
#   docker build -t <registry>/k8s-traffic-operator:latest .
#
# HUBBLE_CLI_VERSION: GATEWAY_IMPLEMENTATION=cilium-hubble로 실행할 때 metrics/hubble_collector.py가
# 쓰는 hubble CLI 버전. **클러스터의 실제 Cilium 버전과 맞춰야 한다** — 버전이 다르면
# hubble-relay와의 gRPC 스키마 불일치("invalid fieldmask")로 조회가 실패한다(실측 확인됨,
# Dockerfile.dashboard와 동일한 제약).
FROM python:3.12-slim

ARG HUBBLE_CLI_VERSION=v1.18.6
ARG TARGETARCH=amd64

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -sL "https://github.com/cilium/hubble/releases/download/${HUBBLE_CLI_VERSION}/hubble-linux-${TARGETARCH}.tar.gz" \
       -o /tmp/hubble.tar.gz \
    && tar -C /usr/local/bin -xzf /tmp/hubble.tar.gz hubble \
    && chmod +x /usr/local/bin/hubble \
    && rm /tmp/hubble.tar.gz \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir kopf kubernetes requests

COPY k8s_traffic_operator k8s_traffic_operator

# kopf CLI(-m 모듈 로더)가 cwd를 자동으로 sys.path에 넣지 않아 직접 지정한다(로컬 테스트에서
# 동일 증상 실측: "ModuleNotFoundError: No module named 'k8s_traffic_operator'").
ENV PYTHONPATH=/app

# 오퍼레이터는 클러스터 상태를 변경하므로(scale/reroute) root로 실행하지 않는다.
# "operator" 그룹이 base 이미지에 이미 존재해 useradd -U가 충돌하므로 이름을 바꾼다.
RUN useradd -r -u 1002 traffic-operator-runtime
USER 1002

CMD ["kopf", "run", "-A", "--standalone", "-m", "k8s_traffic_operator.main", "--verbose"]
