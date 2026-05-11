import logging
import threading
import time
from mcp_server.logger import setup_structured_logging


def worker(logger, num_logs, times):
    start = time.time()
    for i in range(num_logs):
        logger.info("This is a test log message", extra={"my_thread": threading.get_ident(), "iteration": i})
    times.append(time.time() - start)


def run_benchmark(num_threads=10, logs_per_thread=1000):
    logger = logging.getLogger("benchmark")
    threads = []
    times = []

    start_time = time.time()
    for _ in range(num_threads):
        t = threading.Thread(target=worker, args=(logger, logs_per_thread, times))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    end_time = time.time()
    return sum(times)/len(times), end_time - start_time


if __name__ == "__main__":
    import shutil
    import os
    if os.path.exists("logs"):
        shutil.rmtree("logs")
    handler = setup_structured_logging(log_dir="logs", level=logging.DEBUG, also_stderr=False)

    total_thread_time = 0
    total_wall_time = 0
    for _ in range(3):
        t_time, w_time = run_benchmark(20, 2000)
        total_thread_time += t_time
        total_wall_time += w_time

    print(f"Average thread time (caller block time): {total_thread_time / 3:.4f} seconds")
    print(f"Average wall time (total execution time): {total_wall_time / 3:.4f} seconds")
    handler.close()
