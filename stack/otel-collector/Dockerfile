# The upstream collector image is built FROM scratch: it has no shell and no HTTP
# client, so a compose healthcheck cannot run in it. This image adds one static
# busybox binary (musl build, no shared libraries) for the healthcheck's wget.
# Both base images are pinned to an exact tag and their multi-arch index digest.
FROM busybox:1.38.0-musl@sha256:ea2b9914a16a4ac1981994af97b318f7c7d4db76b580c56177f08bf76f4a0be8 AS busybox

FROM otel/opentelemetry-collector-contrib:0.163.1-386@sha256:cc79ec8a69421354e7dbcc524d34cbe9a88bb08ed601f5b7b2a55387392f1aba

COPY --from=busybox /bin/busybox /busybox
