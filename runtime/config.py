"""CJanus runtime configuration flags."""
from dataclasses import dataclass, field


@dataclass
class Config:
    # Debug flags
    slow_debug: float = 0.0       # sleep duration after every instruction (seconds)
    seq_dag: bool = False          # group same process together in DAG display
    dag_debug: bool = False        # display DAG update information
    var_debug: bool = False        # display variable read/write information
    block_debug: bool = False      # display block updates
    exec_debug: bool = False       # store and compare fwd/bwd execution
    sem_debug: bool = False        # display semaphore V/P operations
    lock_debug: bool = False       # display lock/unlock of addresses
    dag_lock_debug: bool = False   # display lock/unlock of DAG nodes
    timing_debug: bool = False     # time the execution

    # Execution modes
    sequential: bool = False       # disable threads and locks
    suppress_output: bool = False  # stop printing executed blocks
    slow_parse: bool = False       # simulate slower parsing with artificial delay
    strict: bool = False           # strictly follow basic-block-wise atomicity
    no_dag: bool = False           # do not record annotation DAG
    ext_expr: bool = False         # allow expressions of arbitrary length

    # Runtime
    max_procs: int = 0             # max thread count (0 = no limit)

    # Auto mode
    auto: bool = False
    auto_progress: int = 0         # 0=forward, 1=backward, 2=replay

    # Metrics
    metrics: bool = False          # collect and display runtime metrics


# Global config instance
cfg = Config()
