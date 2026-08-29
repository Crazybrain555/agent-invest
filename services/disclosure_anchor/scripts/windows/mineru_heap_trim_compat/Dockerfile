FROM mineru@sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458

ARG COMPAT_PATCHER_SHA256
ARG COMPAT_DOCKERFILE_SHA256
ARG TASK_PROTOCOL_V2_SHA256

COPY patch_mineru_344.py /tmp/patch_mineru_344.py
COPY agent_task_protocol_v2.py /tmp/agent_task_protocol_v2.py

RUN test "${COMPAT_PATCHER_SHA256}" = "sha256:$(sha256sum /tmp/patch_mineru_344.py | cut -d ' ' -f 1)" \
    && test "${TASK_PROTOCOL_V2_SHA256}" = "sha256:$(sha256sum /tmp/agent_task_protocol_v2.py | cut -d ' ' -f 1)" \
    && install -m 0444 /tmp/agent_task_protocol_v2.py /usr/local/lib/python3.12/dist-packages/mineru/cli/agent_task_protocol_v2.py \
    && /usr/bin/python3.12 -I /tmp/patch_mineru_344.py \
    && rm -f /tmp/patch_mineru_344.py /tmp/agent_task_protocol_v2.py

ENV MINERU_MALLOC_TRIM=1
ENV MINERU_PHASE_TRACE=0
ENV MINERU_TASK_PROTOCOL_V2=0
RUN /usr/bin/python3.12 -I -c "from mineru.cli.fast_api import AsyncTaskManager; from mineru.utils.model_utils import is_heap_trim_enabled, is_phase_trace_enabled, serial_execution_profile, trim_process_heap; from mineru_vl_utils.post_process.cross_page_table import _HAS_TABLE_MERGE; from mineru_vl_utils.vlm_client.http_client import _process_async_request_limiter; assert AsyncTaskManager; assert _HAS_TABLE_MERGE; assert _process_async_request_limiter; assert serial_execution_profile(16).pipeline_mode == 'serial'; assert is_heap_trim_enabled(); assert not is_phase_trace_enabled(); assert trim_process_heap()"

LABEL io.agent-invest.mineru.base-image-digest="sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458" \
      io.agent-invest.mineru.compatibility-policy="glibc-malloc-trim-per-window.v1" \
      io.agent-invest.mineru.capacity-policy="single-owner-serial-mineru.v1" \
      io.agent-invest.mineru.compatibility-patcher-sha256="${COMPAT_PATCHER_SHA256}" \
      io.agent-invest.mineru.compatibility-dockerfile-sha256="${COMPAT_DOCKERFILE_SHA256}" \
      io.agent-invest.mineru.task-protocol-v2-sha256="${TASK_PROTOCOL_V2_SHA256}"
