"""Independent fake serving instances: metadata only, every compute/copy is forbidden."""
from __future__ import annotations
from math import prod
from types import SimpleNamespace

class MetadataTensor:
    def __init__(self, device: str, dtype: str, shape=(2, 3)):
        self.device = device
        self.dtype = dtype
        self.shape = shape
    def numel(self):
        return prod(self.shape)
    def is_floating_point(self):
        return 'float' in self.dtype
    def _forbidden(self, *args, **kwargs):
        raise AssertionError('identity observation attempted tensor value/compute/transfer')
    to = cpu = cuda = numpy = item = detach = copy_ = data_ptr = _forbidden
    def __array__(self, *args, **kwargs):
        return self._forbidden()

class ServingNet:
    def __init__(self, device='cuda:0', dtype='torch.float16', path='/weights/snapshots/revision/mfr', buffers=()):
        self._parameters = [MetadataTensor(device, dtype)]
        self._buffers = list(buffers)
        self.config = SimpleNamespace(_name_or_path=path)
        self.training = False
        self.parameters_calls = 0
        self.buffers_calls = 0
    def parameters(self, recurse=True):
        self.parameters_calls += 1
        return iter(self._parameters)
    def buffers(self, recurse=True):
        self.buffers_calls += 1
        return iter(self._buffers)
    def named_parameters(self, *args, **kwargs):
        self.parameters_calls += 1
        return iter((str(n), p) for n, p in enumerate(self._parameters))
    def named_buffers(self, *args, **kwargs):
        self.buffers_calls += 1
        return iter((str(n), b) for n, b in enumerate(self._buffers))
    def _forbidden(self, *args, **kwargs):
        raise AssertionError('identity observation attempted network execution/loading/transfer')
    __call__ = forward = state_dict = load_state_dict = to = half = cuda = cpu = _forbidden

class NeverInspect:
    def __getattribute__(self, name):
        raise AssertionError('disabled identity observation touched a model')


def ocr(device='cuda:0', dtype='torch.float16', prefix='/weights/snapshots/revision/ocr'):
    return SimpleNamespace(
        lang='ch',
        text_detector=SimpleNamespace(net=ServingNet(device, dtype), weights_path=prefix+'/det.safetensors', device='misleading-wrapper-device'),
        text_recognizer=SimpleNamespace(net=ServingNet(device, dtype), weights_path=prefix+'/rec.safetensors', device='misleading-wrapper-device'),
    )

def hybrid(device='cuda:0'):
    float_dtype='torch.float16' if device.startswith('cuda') else 'torch.float32'
    return SimpleNamespace(
        device='misleading-wrapper-device',
        lang=None,
        layout_model=SimpleNamespace(model=ServingNet(device, 'torch.float32'), model_dir='/weights/snapshots/revision/layout', device='misleading-wrapper-device'),
        mfr_model=SimpleNamespace(model=ServingNet(device, float_dtype), device='misleading-wrapper-device'),
        ocr_model=ocr(device, float_dtype),
    )

def orientation(device='cuda:0'):
    return SimpleNamespace(ocr_engine=ocr(device, prefix='/weights/snapshots/revision/orientation'))
