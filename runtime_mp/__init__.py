"""
runtime_mp — multiprocessing-based CJanus runtime.

Provides GIL-free parallel execution of `call` instructions by
replacing threading.Thread with multiprocessing.Process.

Modules
-------
runtime_mp   RuntimeMP class (inherits Runtime, overrides execute_call)
benchmark_nogil  GIL detection and thread-scaling benchmark
"""
from .runtime_mp import RuntimeMP

__all__ = ["RuntimeMP"]
