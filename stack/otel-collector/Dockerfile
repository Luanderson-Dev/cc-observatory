# The upstream collector image is built FROM scratch: it has no shell and no HTTP
# client, so a compose healthcheck cannot run in it. This image adds one static
# busybox binary (musl build, no shared libraries) for the healthcheck's wget.
# Both base images are pinned to an exact tag and their multi-arch index digest.
FROM busybox:1.38.0-musl@sha256:ea2b9914a16a4ac1981994af97b318f7c7d4db76b580c56177f08bf76f4a0be8 AS busybox

FROM otel/opentelemetry-collector-contrib:0.162.0-386@sha256:8b9211dbe1b3642eca91b237f39ede8baae9abaf97d6dcc88116f77f29545359

COPY --from=busybox /bin/busybox /busybox
