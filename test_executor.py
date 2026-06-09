import concurrent.futures
import time

def process(i):
    print(f"Start {i}")
    time.sleep(1)
    print(f"End {i}")
    return i

with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
    results = list(executor.map(process, range(10)))
print(results)
