FROM mineru@sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458

ARG COMPAT_PATCHER_SHA256
ARG COMPAT_DOCKERFILE_SHA256

COPY patch_mineru_344.py /tmp/patch_mineru_344.py

RUN test "${COMPAT_PATCHER_SHA256}" = "sha256:$(sha256sum /tmp/patch_mineru_344.py | cut -d ' ' -f 1)" \
    && /usr/bin/python3.12 -I /tmp/patch_mineru_344.py \
    && rm -f /tmp/patch_mineru_344.py

ENV MINERU_MALLOC_TRIM=1

RUN /usr/bin/python3.12 -I -c "from mineru.utils.model_utils import is_heap_trim_enabled, trim_process_heap; assert is_heap_trim_enabled(); assert trim_process_heap()"

LABEL io.agent-invest.mineru.base-image-digest="sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458" \
      io.agent-invest.mineru.compatibility-policy="glibc-malloc-trim-per-window.v1" \
      io.agent-invest.mineru.compatibility-patcher-sha256="${COMPAT_PATCHER_SHA256}" \
      io.agent-invest.mineru.compatibility-dockerfile-sha256="${COMPAT_DOCKERFILE_SHA256}"
