# -*- coding: utf-8 -*-
"""
Utility for overlapping CPU→GPU transfers with computation.

Two-stage prefetcher:
1. Background CPU thread fetches batches from DataLoader
2. CUDA stream asynchronously transfers to GPU

This allows true parallelism between data preparation, H2D transfer, and computation.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Iterator, Optional, Tuple, Union

import torch


class CUDAPrefetcher:
    """
    Legacy prefetcher (kept for backward compatibility).
    Prefetches batches to GPU asynchronously.

    Usage:
        prefetcher = CUDAPrefetcher(loader, device)
        batch = prefetcher.next()
        while batch is not None:
            inputs, targets = batch
            ...
            batch = prefetcher.next()
    """

    def __init__(self, loader: Iterator, device: torch.device) -> None:
        self.loader = iter(loader)
        self.device = device
        self.stream = torch.cuda.Stream(device=device) if device.type == "cuda" else None
        self._next: Optional[Tuple[torch.Tensor, torch.Tensor]] = None
        self._prefetch()

    def _to_device(self, tensor: torch.Tensor) -> torch.Tensor:
        if tensor.device.type == self.device.type:
            return tensor
        return tensor.to(self.device, non_blocking=True)

    def _prefetch(self) -> None:
        try:
            batch = next(self.loader)
        except StopIteration:
            self._next = None
            return

        if isinstance(batch, (list, tuple)):
            feats, labels = batch[:2]
        else:
            feats, labels = batch

        if self.stream is None:
            self._next = (self._to_device(feats), self._to_device(labels))
            return

        with torch.cuda.stream(self.stream):
            feats_gpu = self._to_device(feats.pin_memory() if feats.is_cuda is False else feats)
            labels_gpu = self._to_device(labels.pin_memory() if labels.is_cuda is False else labels)
        self._next = (feats_gpu, labels_gpu)

    def next(self) -> Optional[Tuple[torch.Tensor, torch.Tensor]]:
        if self._next is None:
            return None
        if self.stream is not None:
            torch.cuda.current_stream(device=self.device).wait_stream(self.stream)
        current = self._next
        self._prefetch()
        return current


class TwoStagePrefetcher:
    """
    Two-stage prefetcher for maximum CPU-GPU overlap.

    Stage 1: Background CPU thread fetches batches from DataLoader into a queue
    Stage 2: CUDA stream asynchronously transfers batches from CPU queue to GPU

    This decouples:
    - Data preparation (DuckDB → NumPy/Torch)
    - H2D transfer (pinned CPU → GPU)
    - GPU computation

    Args:
        loader: DataLoader or iterable
        device: Target device
        cpu_queue_size: Size of CPU-side queue (default: 2)
        target_dtype: Optional dtype for GPU tensors (e.g., torch.float16 for I/O half-precision)
    """

    def __init__(
        self,
        loader: Iterator,
        device: torch.device,
        *,
        cpu_queue_size: int = 2,
        target_dtype: Optional[torch.dtype] = None,
    ) -> None:
        self.device = device
        self.target_dtype = target_dtype
        self.cuda_stream = torch.cuda.Stream(device=device) if device.type == "cuda" else None

        # CPU thread prefetch queue
        self._cpu_queue: queue.Queue = queue.Queue(maxsize=max(1, int(cpu_queue_size)))
        self._stop = False

        # Start background thread to feed CPU queue
        loader_iter = iter(loader)

        def _cpu_worker():
            """Background thread that fetches batches from DataLoader."""
            try:
                for batch in loader_iter:
                    if self._stop:
                        break
                    self._cpu_queue.put(batch)
                self._cpu_queue.put(None)  # Sentinel for end of data
            except Exception as e:
                self._cpu_queue.put(e)  # Propagate exceptions to main thread

        self._cpu_thread = threading.Thread(target=_cpu_worker, daemon=True)
        self._cpu_thread.start()

        # GPU-side buffer (prefetched)
        self._next_gpu: Optional[Tuple[torch.Tensor, ...]] = None
        self._prefetch()

    def _to_device(self, x: Any) -> Any:
        """Recursively move tensors to device with non-blocking transfer."""
        if isinstance(x, torch.Tensor):
            kwargs = {"non_blocking": True}
            if self.target_dtype is not None and x.dtype.is_floating_point:
                kwargs["dtype"] = self.target_dtype
            return x.to(self.device, **kwargs)
        if isinstance(x, (list, tuple)):
            return type(x)(self._to_device(item) for item in x)
        return x

    def _prefetch(self) -> None:
        """Fetch next batch from CPU queue and start async H2D transfer."""
        try:
            cpu_batch = self._cpu_queue.get(timeout=300)
        except queue.Empty:
            self._next_gpu = None
            return

        # Handle exceptions from CPU thread
        if isinstance(cpu_batch, Exception):
            raise cpu_batch

        # Handle end of data
        if cpu_batch is None:
            self._next_gpu = None
            return

        # Extract features and labels (handle different batch formats)
        if isinstance(cpu_batch, (list, tuple)):
            feats, labels = cpu_batch[0], cpu_batch[1]
            extra = cpu_batch[2:] if len(cpu_batch) > 2 else ()
        else:
            raise ValueError(f"Unexpected batch format: {type(cpu_batch)}")

        # CPU-only fallback
        if self.cuda_stream is None:
            self._next_gpu = (self._to_device(feats), self._to_device(labels)) + tuple(
                self._to_device(x) for x in extra
            )
            return

        # Async H2D transfer on dedicated CUDA stream
        with torch.cuda.stream(self.cuda_stream):
            gpu_batch = (self._to_device(feats), self._to_device(labels)) + tuple(
                self._to_device(x) for x in extra
            )
        self._next_gpu = gpu_batch

    def next(self) -> Optional[Tuple[torch.Tensor, torch.Tensor]]:
        """
        Get next batch (already on GPU).

        Returns:
            (features, labels) tuple, or None if end of data
        """
        if self._next_gpu is None:
            return None

        # Wait for H2D transfer to complete
        if self.cuda_stream is not None:
            torch.cuda.current_stream(device=self.device).wait_stream(self.cuda_stream)

        current = self._next_gpu
        self._prefetch()  # Start next H2D transfer

        # Return only (features, labels) for backward compatibility
        return (current[0], current[1])

    def shutdown(self) -> None:
        """Stop background thread and clean up."""
        self._stop = True
        try:
            # Drain queue
            while not self._cpu_queue.empty():
                self._cpu_queue.get_nowait()
        except queue.Empty:
            pass
