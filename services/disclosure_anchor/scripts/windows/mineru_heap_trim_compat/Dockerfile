FROM mineru@sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458 AS legacy-runtime

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
RUN /usr/bin/python3.12 -I -c "import inspect; from mineru.cli.fast_api import AsyncTaskManager; from mineru.utils.model_utils import is_heap_trim_enabled, is_phase_trace_enabled, serial_execution_profile, trim_process_heap; from mineru_vl_utils.post_process.cross_page_table import _HAS_TABLE_MERGE; from mineru_vl_utils.vlm_client.http_client import _process_async_request_limiter; manager_source=inspect.getsource(AsyncTaskManager.__init__); assert 'self.task_protocol_v2 = DurableTaskRegistry(' in manager_source; assert 'task_protocol_v2_enabled' not in manager_source; assert _HAS_TABLE_MERGE; assert _process_async_request_limiter; assert serial_execution_profile(16).pipeline_mode == 'serial'; assert is_heap_trim_enabled(); assert not is_phase_trace_enabled(); assert trim_process_heap()"

LABEL io.agent-invest.mineru.base-image-digest="sha256:109016f8f7666c3a86b0a6585f5b7003d1dd63c2d318f6ecd7ab1db5aa582458" \
      io.agent-invest.mineru.compatibility-policy="glibc-malloc-trim-per-window.v1" \
      io.agent-invest.mineru.capacity-policy="single-owner-serial-mineru.v1" \
      io.agent-invest.mineru.compatibility-patcher-sha256="${COMPAT_PATCHER_SHA256}" \
      io.agent-invest.mineru.compatibility-dockerfile-sha256="${COMPAT_DOCKERFILE_SHA256}" \
      io.agent-invest.mineru.task-protocol-v2-sha256="${TASK_PROTOCOL_V2_SHA256}"

# The installer selects this target only with a complete externally pinned config.
# Helpers precede the config layer so parameter trials reuse the patched runtime.
FROM legacy-runtime AS capacity-helpers
ARG CAPACITY_CONFIG_SOURCE_SHA256
ARG CAPACITY_FILE_SOURCE_SHA256
ARG CAPACITY_BOOTSTRAP_SOURCE_SHA256
ARG CAPACITY_OBSERVATION_SOURCE_SHA256
ARG CAPACITY_SOURCES_SHA256
COPY agent_capacity_config.py agent_capacity_file.py agent_capacity_bootstrap.py agent_capacity_observation.py /tmp/
RUN test "${CAPACITY_CONFIG_SOURCE_SHA256}" = "sha256:$(sha256sum /tmp/agent_capacity_config.py | cut -d ' ' -f 1)" \
    && test "${CAPACITY_FILE_SOURCE_SHA256}" = "sha256:$(sha256sum /tmp/agent_capacity_file.py | cut -d ' ' -f 1)" \
    && test "${CAPACITY_BOOTSTRAP_SOURCE_SHA256}" = "sha256:$(sha256sum /tmp/agent_capacity_bootstrap.py | cut -d ' ' -f 1)" \
    && test "${CAPACITY_OBSERVATION_SOURCE_SHA256}" = "sha256:$(sha256sum /tmp/agent_capacity_observation.py | cut -d ' ' -f 1)" \
    && /usr/bin/python3.12 -I -c "import hashlib,json,sys; from pathlib import Path; names=('config','file','bootstrap','observation'); pins={'mineru/cli/agent_capacity_'+n+'.py':'sha256:'+hashlib.sha256(Path('/tmp/agent_capacity_'+n+'.py').read_bytes()).hexdigest() for n in names}; actual='sha256:'+hashlib.sha256(json.dumps(pins,sort_keys=True,separators=(',',':')).encode()).hexdigest(); assert actual==sys.argv[1], 'capacity source map mismatch'" "${CAPACITY_SOURCES_SHA256}" \
    && install -o root -g root -m 0444 /tmp/agent_capacity_*.py /usr/local/lib/python3.12/dist-packages/mineru/cli/ \
    && rm /tmp/agent_capacity_*.py
RUN /usr/bin/python3.12 -I -c "import json; from pathlib import Path; p=Path('/opt/agent-invest/mineru-serial-v1/compatibility.json'); m=json.loads(p.read_bytes()); assert set(m)=={'schema','policy','capacity_policy','mineru_version','mineru_vl_utils_version','base_image_digest','patcher_sha256','preimage_sha256','patched_source_sha256'}; assert m['schema']=='mineru-runtime-compatibility.v5' and m['capacity_policy']=='single-owner-serial-mineru.v1'; m['capacity_policy']='single-process-explicit-capacity.v1'; p.write_text(json.dumps(m,sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')"
LABEL io.agent-invest.mineru.capacity-policy="single-process-explicit-capacity.v1" \
      io.agent-invest.mineru.capacity-sources-sha256="${CAPACITY_SOURCES_SHA256}"

FROM capacity-helpers AS explicit-capacity
ARG CAPACITY_CONFIG_SHA256
COPY capacity-config.json /tmp/capacity-config.json
RUN test "${CAPACITY_CONFIG_SHA256}" = "sha256:$(sha256sum /tmp/capacity-config.json | cut -d ' ' -f 1)" \
    && install -d -o root -g root -m 0755 /usr/local/etc/mineru \
    && install -o root -g root -m 0444 /tmp/capacity-config.json /usr/local/etc/mineru/capacity.json \
    && /usr/bin/python3.12 -I -c "import sys; from pathlib import Path; from mineru.cli.agent_capacity_file import read_mineru_capacity_file; from mineru.cli.agent_capacity_config import decode_mineru_capacity_config; raw=read_mineru_capacity_file(Path('/usr/local/etc/mineru/capacity.json'),expected_sha256=sys.argv[1],expected_owner_uid=0); decode_mineru_capacity_config(raw)" "${CAPACITY_CONFIG_SHA256}" \
    && rm /tmp/capacity-config.json
ENV MINERU_CAPACITY_CONFIG_PATH=/usr/local/etc/mineru/capacity.json
ENV MINERU_CAPACITY_CONFIG_SHA256=${CAPACITY_CONFIG_SHA256}
LABEL io.agent-invest.mineru.capacity-config-sha256="${CAPACITY_CONFIG_SHA256}"

# Keep the default BuildKit result legacy; the installer also pins its target.
FROM legacy-runtime AS legacy
