# The upstream collector image is built FROM scratch: it has no shell and no HTTP
# client, so a compose healthcheck cannot run in it. This image adds one static
# busybox binary (musl build, no shared libraries) for the healthcheck's wget.
# Both base images are pinned to an exact tag and their multi-arch index digest.
FROM busybox:1.38.0-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6 AS busybox

FROM otel/opentelemetry-collector-contrib:0.162.0@sha256:39923a8e431bd1f57be82411999d389fcfe40857492e4365456d97a4c1f74be6

COPY --from=busybox /bin/busybox /busybox
